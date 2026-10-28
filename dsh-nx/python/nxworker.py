# -*- coding: utf-8 -*-
"""dsh-nx worker: a resident NXOpen journal driven by request/response files.

Started once by the plugin (or by hand) as

    D:\\software\\NX\\NXBIN\\run_journal.exe nxworker.py

with NXBRIDGE_ROOT pointing at a directory holding ``req/`` and ``resp/``.
The loop waits for ``req/<id>.json`` ({"id","cmd","args"}) and answers with
``resp/<id>.json`` ({"id","ok","result","error","trace","log","ms"}).
Options come from :class:`NXOpen.SessionOptions`, never from argv, because
``run_journal.exe`` reserves its own command line.
"""
import os
import re
import sys
import json
import math
import time
import glob
import subprocess
import traceback

ROOT = os.environ.get("NXBRIDGE_ROOT") or os.path.dirname(os.path.abspath(__file__))
REQ = os.path.join(ROOT, "req")
RESP = os.path.join(ROOT, "resp")
LOG = os.path.join(ROOT, "bridge.log")
READY = os.path.join(ROOT, "ready.json")
FATAL = os.path.join(ROOT, "fatal.log")
MAXSECONDS = float(os.environ.get("NXBRIDGE_MAX_SECONDS") or 21600)

for _d in (ROOT, REQ, RESP):
    try:
        os.makedirs(_d)
    except Exception:
        pass


def log(*parts):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + " ".join(str(p) for p in parts)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def fix_text(value):
    """NX hands back GBK bytes mis-decoded as UTF-8 for Chinese names."""
    if not isinstance(value, str):
        return value
    try:
        return value.encode("gbk").decode("utf-8")
    except Exception:
        return value


def main():
    import NXOpen
    import NXOpen.Drawings
    import NXOpen.Annotations

    NX = NXOpen
    s = NXOpen.Session.GetSession()
    state = {"part": None}
    started = time.time()

    def out(*parts):
        log(*parts)
        return " ".join(str(p) for p in parts)

    def body_list(part):
        bodies = []
        for b in list(part.Bodies):
            bodies.append({
                "name": fix_text(getattr(b, "Name", "") or ""),
                "journalId": str(getattr(b, "JournalIdentifier", "")),
                "type": type(b).__name__,
            })
        return bodies

    UNIT_NAMES = {"Millimeters": "mm", "Centimeters": "cm", "Meters": "m", "Inches": "inch", "Feet": "ft"}

    def unit_name(part):
        """`part.PartUnits` reads back as a bare enum value; name it."""
        try:
            raw = part.PartUnits
            value = getattr(raw, "value", raw)
            for name, short in UNIT_NAMES.items():
                member = getattr(NX.PartUnits, name, None)
                if member is not None and getattr(member, "value", None) == value:
                    return short
            return str(value)
        except Exception:
            return ""

    def part_info(part):
        units = unit_name(part)
        info = {
            "leaf": fix_text(part.Leaf),
            "fullPath": fix_text(part.FullPath),
            "bodies": body_list(part),
        }
        if units:
            info["units"] = units
        return info

    def display_part():
        """The part every command works on: what NX has in front.

        The displayed part is the truth -- opening a part (a tool, or the panel preview)
        changes it, and a cached state["part"] would then point at a part nobody is
        looking at any more (that once made nx_save write the previewed part).
        """
        part = s.Parts.Display
        if part is not None:
            state["part"] = part
            return part
        part = state.get("part")
        if part is None:
            raise RuntimeError("no part is open in this NX session")
        return part

    def fully_loaded(component, budget=None):
        """Return the component's prototype part with its geometry actually in memory.

        A large assembly opens its components minimally loaded: Part.Bodies is empty until
        the prototype is pulled in, which made a 35-component fixture preview as six loose
        screws. Loading is per component and the counter keeps a giant assembly from
        stalling the whole export.
        """
        if budget is None:
            budget = fully_loaded.budget
        prototype = component.Prototype
        if prototype is None or fully_loaded.seen >= budget:
            return prototype
        fully_loaded.seen += 1
        try:
            if not prototype.IsFullyLoaded:
                prototype.LoadFully()
        except Exception:
            pass
        return prototype
    fully_loaded.seen = 0
    fully_loaded.budget = 40

    # Paper size in mm, for laying views out when the wizard refuses (A-series landscape).
    SHEET_SIZES = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0),
                   "A1": (841.0, 594.0), "A0": (1189.0, 841.0)}

    def bodies_of(part, names=None):
        bodies = list(part.Bodies)
        if not names:
            return bodies
        wanted = {str(n) for n in names}
        picked = [b for b in bodies if fix_text(getattr(b, "Name", "")) in wanted]
        if not picked:
            raise RuntimeError("no body matches %r" % (names,))
        return picked

    def measure(part, names=None):
        bodies = bodies_of(part, names)
        if not bodies:
            names = []
            try:
                root = part.ComponentAssembly.RootComponent
                for child in root.GetChildren():
                    names.append(fix_text(getattr(child, "Name", "") or ""))
            except Exception:
                pass
            return {
                "assembly": True,
                "bodies": 0,
                "components": len(names),
                "componentNames": names[:60],
                "note": "assembly has no bodies of its own; measure each component part instead",
            }
        try:
            box = s.Measurement.GetBoundingBoxProperties(bodies, 0, NX.Point3d(0.0, 0.0, 0.0), False)
        except Exception as error:
            raise RuntimeError("bounding box failed: %s" % error)
        result = {
            "bodies": len(bodies),
            "bboxMin": [box[0][0].X, box[0][0].Y, box[0][0].Z],
            "bboxMax": [box[0][6].X, box[0][6].Y, box[0][6].Z],
            "bboxSize": list(box[2]),
            "bboxVolume": float(box[5]),
        }
        props = s.Measurement.GetBodyProperties(bodies, 0.999, False)
        result.update({
            # mass_props[0] is surface area, [1] volume, [2] mass in kg.
            "volume": float(props[0][1]),
            "area": float(props[0][0]),
            "mass": float(props[0][2]),
            "centroid": [props[2].X, props[2].Y, props[2].Z],
            "density": float(props[4]),
        })
        return result

    def sheet_info(sheet):
        views = []
        try:
            for v in sheet.GetDraftingViews():
                views.append({
                    "name": fix_text(v.Name),
                    "kind": type(v).__name__,
                    "scale": float(v.Scale),
                })
        except Exception as e:
            out("sheet views failed:", repr(e))
        scale = (1.0, 1.0)
        try:
            scale = sheet.GetScale()
        except Exception:
            pass
        return {
            "name": fix_text(sheet.Name),
            "width": float(sheet.Length),
            "height": float(sheet.Height),
            "scale": [float(scale[0]), float(scale[1])],
            "projection": str(sheet.ProjectionAngle),
            "views": views,
        }

    def sheet_list(part):
        sheets = []
        try:
            current = part.DraftingDrawingSheets.CurrentDrawingSheet
        except Exception:
            current = None
        for sh in list(part.DraftingDrawingSheets):
            info = sheet_info(sh)
            info["current"] = current is not None and sh.Tag == current.Tag
            sheets.append(info)
        return sheets

    def listed_parts():
        parts = s.Parts
        for name in ("GetDisplayedParts", "GetDisplayParts"):
            fn = getattr(parts, name, None)
            if fn is not None:
                try:
                    return list(fn())
                except Exception:
                    continue
        return []

    def cmd_status(args):
        return {
            "release": s.ReleaseNumber,
            "fullRelease": s.FullReleaseNumber,
            "isBatch": bool(s.IsBatch),
            "application": str(s.ApplicationName),
            "baseDir": s.GetEnvironmentVariableValue("UGII_BASE_DIR"),
            "uptimeSeconds": round(time.time() - started, 1),
            "bridgeRoot": ROOT,
            "displayParts": [fix_text(getattr(p, "Leaf", "") or str(p)) for p in listed_parts()],
            "hasPart": state["part"] is not None,
        }

    def cmd_open(args):
        path = args["path"]
        if not os.path.exists(path):
            raise RuntimeError("file not found: " + path)
        wanted = os.path.normcase(os.path.abspath(path))
        part = None
        for loaded in list(s.Parts):
            try:
                if os.path.normcase(os.path.abspath(fix_text(loaded.FullPath))) == wanted:
                    part = loaded
                    break
            except Exception:
                continue
        reused = part is not None
        if part is None:
            res = s.Parts.OpenDisplay(path)
            part = res[0] if isinstance(res, (tuple, list)) else res
        s.Parts.SetDisplay(part, True, True)
        state["part"] = part
        info = part_info(part)
        info["reused"] = reused
        info["sheets"] = sheet_list(part)
        return info

    def cmd_info(args):
        part = display_part()
        info = part_info(part)
        info["sheets"] = sheet_list(part)
        info["modelingViews"] = [str(v.Name) for v in part.ModelingViews]
        return info

    def cmd_measure(args):
        return measure(display_part(), args.get("bodies"))

    def cmd_export(args):
        part = display_part()
        fmt = (args.get("format") or "step").lower()
        target = args["path"]
        parent = os.path.dirname(os.path.abspath(target))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        if os.path.exists(target):
            os.remove(target)
        dm = s.DexManager
        if fmt in ("step", "stp"):
            creator = dm.CreateStepCreator()
        elif fmt in ("iges", "igs"):
            creator = dm.CreateIgesCreator()
        elif fmt == "stl":
            creator = dm.CreateStlCreator()
        else:
            raise RuntimeError("unsupported export format: %s (use step, iges or stl)" % fmt)
        creator.OutputFile = target
        if fmt == "stl":
            # Preview meshes live and die by these two numbers: NX defaults to 0.058 mm chordal
            # and 18 degrees, which turns a real thread into a handful of flat rings. A finer
            # mesh costs a couple of times the triangles and is what makes tooth flanks, fillets
            # and bores readable in the panel.
            try:
                if args.get("tolerance"):
                    creator.ChordalTol = float(args["tolerance"])
                if args.get("angle"):
                    creator.AngularTol = float(args["angle"])
            except Exception:
                pass
        # Two switches decide whether a solid ever reaches the translator: an empty
        # layer mask selects nothing, and every object type flag defaults to False.
        try:
            creator.LayerMask = args.get("layers") or "1-256"
        except Exception:
            pass
        types = getattr(creator, "ObjectTypes", None)
        if types is not None:
            for flag in ("Solids", "Surfaces", "Curves", "Attributes"):
                try:
                    setattr(types, flag, True)
                except Exception:
                    pass
        try:
            creator.ExportFrom = NX.StepCreatorExportFromOption.DisplayPart if fmt in ("step", "stp") \
                else NX.IgesCreatorExportFromOption.DisplayPart
        except Exception:
            pass
        picked = pick_bodies(part, args.get("bodies"), allow_empty=True)
        component_bodies = []
        if not picked:
            # An assembly part owns no bodies: its geometry lives in the components.
            # Take the OCCURRENCE of each prototype body, never the prototype itself —
            # the prototype sits at its own origin, so a plain shape export scatters the
            # components instead of showing them where the assembly puts them.
            component_bodies = component_bodies_in(part)
            if not component_bodies and getattr(part, "ComponentAssembly", None) is not None:
                # Right after opening, an assembly is only partly loaded: no component has
                # its bodies yet, the selection stays empty and the translator writes an
                # empty mesh (an 84 byte STL with zero triangles). Load it, then look again.
                try:
                    part.LoadFully()
                except Exception:
                    pass
                component_bodies = component_bodies_in(part)
        is_assembly = not picked and bool(component_bodies)
        if is_assembly:
            # STL and IGES take the component bodies through the selector; only STEP reads
            # the assembly from disk. ("Existing part" mode turns an assembly into a single
            # Transformation entity for IGES - a 656 byte file with no geometry in it.)
            if fmt in ("stl", "iges", "igs"):
                picked = component_bodies
            else:
                full = getattr(part, "FullPath", "") or ""
                if not full or not os.path.exists(full):
                    raise RuntimeError("save the assembly before exporting STEP/IGES: "
                                       "the translator reads it from disk")
                creator.ExportFrom = NX.StepCreatorExportFromOption.ExistingPart \
                    if fmt in ("step", "stp") else NX.IgesCreatorExportFromOption.ExistingPart
                creator.InputFile = full
        added = 0
        selection = getattr(creator, "ExportSelectionBlock", None) if picked else None
        for body in picked:
            # STEP/IGES wrap the list in an ObjectSelector, STL exposes it directly.
            try:
                selection.SelectionComp.Add(body)
                added += 1
            except Exception:
                try:
                    selection.Add(body)
                    added += 1
                except Exception:
                    break
        creator.Commit()
        # The translators run asynchronously: the file shows up a few seconds later.
        deadline = time.time() + float(args.get("waitSeconds") or 120)
        while time.time() < deadline and not os.path.exists(target):
            time.sleep(0.4)
        ok = os.path.exists(target)
        if ok and fmt == "stl":
            # An empty tessellation is not an export: report it instead of handing the panel a
            # file the viewer cannot draw.
            try:
                with open(target, "rb") as handle:
                    head = handle.read(84)
                if len(head) >= 84 and int.from_bytes(head[80:84], "little") == 0:
                    os.remove(target)
                    raise RuntimeError(
                        "the STL translator produced an empty mesh (0 triangles): no solid was "
                        "offered - open the part or assembly first, then export again")
            except RuntimeError:
                raise
            except Exception:
                pass
        log = os.path.splitext(target)[0] + ".log"
        counts = []
        if os.path.exists(log):
            try:
                with open(log, "r", errors="replace") as handle:
                    for line in handle:
                        if "Number of solids" in line:
                            counts.append(line.split(":")[-1].strip())
            except Exception:
                pass
        if not ok:
            raise RuntimeError("%s export produced no file at %s (bodies offered: %d, accepted: %d)"
                               % (fmt, target, len(picked), added))
        return {
            "format": fmt,
            "path": target,
            "exists": ok,
            "bytes": os.path.getsize(target),
            "bodies": added,
            "assembly": is_assembly,
            "solidCounts": counts,
        }

    def cmd_drawing(args):
        part = display_part()
        # Assemblies open lightweight: without loading them the fallback layout has no bounding
        # box to fit the views to (and the views themselves come out half empty).
        try:
            part.LoadFully()
        except Exception:
            pass
        size = str(args.get("size") or "A3").upper()
        projection = str(args.get("projection") or "first").lower()
        auto_scale = args.get("autoScale", True)
        use_template = args.get("template", True)
        # "Give me a drawing" must not stack a second empty sheet on a part that already has
        # one: the panel button would otherwise leave a blank current sheet behind and the next
        # PDF print would hand back a white page.
        reuse = args.get("reuse", True)
        scale = args.get("scale") or [1.0, 1.0]

        if reuse:
            paper = SHEET_SIZES.get(size)
            for existing in list(part.DraftingDrawingSheets):
                try:
                    populated = [v for v in existing.GetDraftingViews()
                                 if view_summary(v)["visible"] > 0]
                except Exception:
                    populated = []
                if not populated:
                    continue
                # Only reuse a sheet of the size that was asked for: picking A2 after an A3
                # sheet exists otherwise silently keeps printing on the small sheet.
                if paper is not None:
                    try:
                        same = (abs(float(existing.Length) - paper[0]) < 1.0
                                and abs(float(existing.Height) - paper[1]) < 1.0)
                    except Exception:
                        same = True
                    if not same:
                        continue
                out("reusing sheet:", fix_text(existing.Name), len(populated), "view(s)")
                return {"sheet": sheet_info(existing), "sheets": sheet_list(part),
                        "reused": True,
                        "views": [view_summary(v) for v in populated]}

        angle = NX.Drawings.DrawingSheetProjectionAngleType.ThirdAngle if projection.startswith("t") \
            else NX.Drawings.DrawingSheetProjectionAngleType.FirstAngle
        sheet = None
        if use_template:
            tpl = os.path.join(s.GetEnvironmentVariableValue("UGII_BASE_DIR"),
                               "DRAFTING", "templates", "Drawing-%s-Size-template.prt" % size)
            if os.path.exists(tpl):
                known = {sh.Tag for sh in list(part.DraftingDrawingSheets)}
                # Create() is a void method: the new sheet has to be found afterwards.
                part.DrawingSheets.SheetTemplates.Create(tpl)
                fresh = [sh for sh in list(part.DraftingDrawingSheets) if sh.Tag not in known]
                if fresh:
                    sheet = fresh[0]
                    out("template sheet:", fix_text(sheet.Name))
                else:
                    out("template produced no sheet, falling back to InsertSheet")
        if sheet is None:
            sheet_size = getattr(NX.Drawings.DrawingSheetStandardSheetSize, size)
            name = args.get("sheetName") or "SH1"
            sheet = part.DraftingDrawingSheets.InsertSheet(
                name, sheet_size, float(scale[0]), float(scale[1]), angle)
            out("inserted sheet:", repr(sheet))

        # The wizard's "auto scale" is not trustworthy on big parts: a 500 mm fixture comes out at
        # 0.83 on an A3 and the outer views hang off the paper, and views cannot be deleted again.
        # So the default is our own layout, computed from the part's bounding box; the wizard is
        # only used when the caller asks for it (layout: "wizard").
        layout = str(args.get("layout") or "fitted").lower()
        created = []
        plan_ok = False
        if layout == "wizard":
            wiz = part.DraftingViews.CreateViewCreationWizardBuilder()
            # The drawing layout part must be named explicitly, otherwise Commit
            # raises "Drawing part file and drawing layout part file differ."
            wiz.Part = part
            wiz.BaseView = str(args.get("baseView") or "TOP")
            flags = args.get("views") or {"front": True, "right": True, "isometric": True}
            for key, prop in (("top", "TopView"), ("front", "FrontView"), ("right", "RightView"),
                              ("left", "LeftView"), ("back", "BackView"), ("bottom", "BottomView"),
                              ("isometric", "IsometricView"), ("trimetric", "TrimetricView")):
                if key in flags:
                    setattr(wiz, prop, bool(flags[key]))
            wiz.AutoScale = bool(auto_scale)
            plan_ok = True
            try:
                wiz.Commit()
            except Exception as e:
                # A wizard that refuses (no model views, assembly quirks) must not leave behind a
                # bare sheet: fall through to explicit base views below.
                out("view wizard failed:", repr(e))
                plan_ok = False
            try:
                created = [v for v in sheet.GetDraftingViews() if view_summary(v)["visible"] > 0]
            except Exception:
                created = []
        if not created or not plan_ok:
            # Explicit fallback: one base view per orientation, laid out in first-angle order.
            # The view scale has to be derived from the part, otherwise a 500 mm fixture is drawn
            # at 1:1 and runs off an A3 sheet (the panel's "print PDF" then hands back a page that
            # is mostly missing).
            out("falling back to explicit base views")
            width = height = 0.0
            try:
                extent = []
                if len(part.Bodies) > 0:
                    extent = measure(part).get("bboxSize") or []
                elif getattr(part, "ComponentAssembly", None) is not None:
                    shown = component_bodies_in(part)
                    if not shown:
                        # Lightweight components carry no bodies until the part is loaded.
                        try:
                            part.LoadFully()
                        except Exception:
                            pass
                        shown = component_bodies_in(part)
                    if shown:
                        box = s.Measurement.GetBoundingBoxProperties(
                            shown, 0, NX.Point3d(0.0, 0.0, 0.0), False)
                        extent = list(box[2])
                if len(extent) >= 2:
                    width = float(extent[0])
                    height = float(extent[1])
            except Exception as e:
                out("fallback fit: no bounding box", repr(e))
            paper = SHEET_SIZES.get(str(size).upper(), (420.0, 297.0))
            if width > 0 and height > 0:
                fit = min((paper[0] * 0.78) / width, (paper[1] * 0.46) / height)
            else:
                # Extent unknown (assembly whose components never loaded): pick a scale that is
                # small enough for the usual fixture-sized part rather than filling the sheet.
                fit = 0.4
            ratio = 1.0
            for candidate in (1.0, 0.5, 0.4, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01):
                if candidate <= fit:
                    ratio = candidate
                    break
            else:
                ratio = 0.01
            out("fallback scale: 1:%g (part %gx%g mm on %s)" % (
                1.0 / ratio if ratio else 1, width, height, size))
            model_views = {}
            try:
                for mv in list(part.ModelingViews):
                    model_views[fix_text(mv.Name).upper()] = mv
            except Exception:
                model_views = {}
            grid = {"FRONT": (0.30, 0.66), "TOP": (0.30, 0.30),
                    "RIGHT": (0.71, 0.68), "ISOMETRIC": (0.74, 0.29)}
            wanted = [("FRONT", 0), ("TOP", 1), ("RIGHT", 2), ("ISOMETRIC", 3)]
            placed = []
            for index, (name, slot) in enumerate(wanted):
                mv = model_views.get(name)
                if mv is None:
                    continue
                builder = part.DraftingViews.CreateBaseViewBuilder(None)
                try:
                    builder.SelectModelView.SelectedView = mv
                    builder.Scale.ScaleType = NX.Drawings.ViewScaleBuilderType.Ratio
                    # An isometric view looks bigger than its plan on paper; keep it inside its
                    # quadrant instead of letting it run over the plan view.
                    builder.Scale.Numerator = float(ratio * (0.78 if name == "ISOMETRIC" else 1.0))
                    builder.Scale.Denominator = 1.0
                    spot = grid.get(name, (0.30, 0.66))
                    builder.Placement.Placement.Value = part.Points.CreatePoint(
                        NX.Point3d(paper[0] * spot[0], paper[1] * spot[1], 0.0))
                    builder.Validate()
                    builder.Commit()
                    placed.append(name)
                except Exception as e:
                    out("base view failed:", name, repr(e))
            created = [v for v in sheet.GetDraftingViews() if view_summary(v)["visible"] > 0]
            out("explicit base views:", ", ".join(placed))

        info = {"sheet": sheet_info(part.DraftingDrawingSheets.CurrentDrawingSheet),
                "sheets": sheet_list(part),
                "reused": False,
                "views": [view_summary(v) for v in created]}
        return info

    def cmd_pdf(args):
        part = display_part()
        target = args["path"]
        parent = os.path.dirname(os.path.abspath(target))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        sheets = []
        wanted = args.get("sheets")
        if wanted:
            names = {str(n) for n in wanted}
            for sh in list(part.DraftingDrawingSheets):
                if fix_text(sh.Name) in names:
                    sheets.append(sh)
        if not sheets:
            # Print something that has geometry on it: the current sheet may well be a bare one
            # left over from an earlier "new drawing" click, and a PDF of it is a white page.
            best = None
            best_count = 0
            try:
                current = part.DraftingDrawingSheets.CurrentDrawingSheet
            except Exception:
                current = None
            for sh in list(part.DraftingDrawingSheets):
                try:
                    count = len([v for v in sh.GetDraftingViews() if view_summary(v)["visible"] > 0])
                except Exception:
                    count = 0
                if current is not None and sh.Tag == current.Tag and count > 0:
                    best, best_count = sh, count
                    break
                if count > best_count:
                    best, best_count = sh, count
            if best is not None:
                sheets = [best]
                out("printing sheet:", fix_text(best.Name), best_count, "view(s)")
        if not sheets:
            # Nothing with geometry anywhere: build a sheet here, so "打印 PDF" works right
            # after opening a part, then insist on a real sheet before touching its Name.
            try:
                cmd_drawing({"size": args.get("size") or "A3", "projection": "first",
                             "views": {"top": True, "front": True, "isometric": True}})
            except Exception:
                pass
            sheets = [sh for sh in list(part.DraftingDrawingSheets)
                      if len([v for v in sh.GetDraftingViews()
                              if view_summary(v)["visible"] > 0]) > 0]
        if not sheets:
            try:
                current = part.DraftingDrawingSheets.CurrentDrawingSheet
            except Exception:
                current = None
            if current is None:
                raise RuntimeError("this part has no drawing sheet yet - build one first "
                                   "(nx_drawing, or the panel's 建图纸 button)")
            sheets = [current]
        for candidate in sheets:
            # A sheet that is not on screen does not get its CGM exported: open it first.
            try:
                candidate.Open()
            except Exception:
                pass
        builder = part.PlotManager.CreatePrintPdfbuilder()
        # Printing nothing produces an empty file: the source sheets must be set.
        builder.SourceBuilder.SetSheets(sheets)
        builder.Filename = target
        builder.Action = NX.PrintPDFBuilderActionOption.New
        try:
            builder.OutputText = NX.PrintPDFBuilderOutputTextOption.Text
        except Exception:
            pass
        builder.Commit()
        ok = os.path.exists(target)
        return {
            "path": target,
            "exists": ok,
            "bytes": os.path.getsize(target) if ok else 0,
            "sheets": [fix_text(sh.Name) for sh in sheets],
            "sheet": fix_text(sheets[0].Name) if sheets else None,
            "views": len([v for v in sheets[0].GetDraftingViews() if view_summary(v)["visible"] > 0])
            if sheets else 0,
        }

    def cmd_annotate(args):
        part = display_part()
        views = list(part.DraftingViews)
        if not views:
            raise RuntimeError("the part has no drafting views to annotate")
        builder = part.Drafting.AutomationManager.CreateAnnotateViewsBuilder()
        for v in views:
            builder.Views.Add(v)
        builder.Commit()
        return {"annotated": len(views), "views": [fix_text(v.Name) for v in views]}

    # ---------------------------------------------------------------- drafting extras
    def drafting_views(part):
        try:
            return list(part.DraftingDrawingSheets.CurrentDrawingSheet.GetDraftingViews())
        except Exception:
            return list(part.DraftingViews)

    def pick_view(part, wanted):
        views = drafting_views(part)
        if not views:
            raise RuntimeError("the part has no drafting views yet — run nx_drawing first")
        names = ", ".join(fix_text(v.Name) for v in views)
        if wanted:
            key = str(wanted).strip().lower()
            for v in views:
                if fix_text(v.Name).lower() == key:
                    return v
            for v in views:
                if key in fix_text(v.Name).lower():
                    return v
            raise RuntimeError("no view matches %r (views: %s)" % (wanted, names))
        for v in views:
            name = fix_text(v.Name).lower()
            if type(v).__name__ == "BaseView" and ("top" in name or "front" in name):
                return v
        return views[0]

    def view_frame(view):
        # GetDrawingReferencePoint is the view centre on the sheet, Origin is the model
        # point that sits there mirrored, so model -> sheet is ref + (p + Origin) * scale.
        ref = view.GetDrawingReferencePoint()
        try:
            ox, oy = float(view.Origin.X), float(view.Origin.Y)
        except Exception:
            ox = oy = 0.0
        return float(ref.X), float(ref.Y), float(view.Scale), ox, oy

    def sheet_of(view, x, y):
        rx, ry, scale, ox, oy = view_frame(view)
        return rx + (float(x) + ox) * scale, ry + (float(y) + oy) * scale

    def free_spot(part):
        """A sheet point as far as possible from every view centre (crude auto placement)."""
        sheet = part.DraftingDrawingSheets.CurrentDrawingSheet
        width, height = float(sheet.Length), float(sheet.Height)
        taken = []
        for v in drafting_views(part):
            try:
                r = v.GetDrawingReferencePoint()
                taken.append((float(r.X), float(r.Y)))
            except Exception:
                pass
        best, best_distance = (width - 70.0, 45.0), -1.0
        y = 30.0
        while y < height - 25.0:
            x = 30.0
            while x < width - 25.0:
                distance = min([((x - tx) ** 2 + (y - ty) ** 2) ** 0.5 for tx, ty in taken] or [9.9e3])
                if distance > best_distance:
                    best, best_distance = (x, y), distance
                x += 15.0
            y += 15.0
        return best

    def view_summary(view):
        return {
            "name": fix_text(view.Name),
            "kind": type(view).__name__,
            "scale": float(view.Scale),
            "sheetPoint": [round(float(view.GetDrawingReferencePoint().X), 2),
                           round(float(view.GetDrawingReferencePoint().Y), 2)],
            "visible": len(list(view.AskVisibleObjects())),
        }

    def cmd_section_view(args):
        part = display_part()
        parent = pick_view(part, args.get("parent"))
        start = args.get("start") or args.get("from")
        end = args.get("end") or args.get("to")
        if not start or not end:
            raise RuntimeError("start and end are the two ends of the cut line in the parent view's "
                               "model space, e.g. start [-10,30,10] and end [110,30,10]")
        kind = str(args.get("type") or "pointtopoint").replace("-", "").replace("_", "").lower()
        types = NX.Drawings.SectionViewBuilderSectionLineType
        mapping = {"pointtopoint": types.PointToPoint, "half": types.Half,
                   "pointandangle": types.PointAndAngle, "revolved": types.Revolved,
                   "stepped": types.SimpleStepped}
        if kind not in mapping:
            raise RuntimeError("type must be one of %s" % ", ".join(sorted(mapping)))

        at = args.get("at") or free_spot(part)

        def build(candidate):
            known = {v.Tag for v in drafting_views(part)}
            builder = part.DraftingViews.CreateSectionViewBuilder(None)
            # The parent has to be set before the cut segments, otherwise NX rejects both.
            builder.ParentView.View.Value = parent
            builder.SectionViewType = mapping[candidate]
            segments = builder.SectionLineSegments.SegmentLocation
            segments.AddCutSegment(part.Points.CreatePoint(pnt(*start)))
            segments.AddCutSegment(part.Points.CreatePoint(pnt(*end)))
            builder.ViewPlacement.Placement.Value = part.Points.CreatePoint(pnt(at[0], at[1], 0.0))
            builder.Validate()
            builder.Commit()
            fresh = [v for v in drafting_views(part) if v.Tag not in known]
            if not fresh:
                raise RuntimeError("the section view was committed but no new view appeared")
            return fresh[-1]

        # NX refuses a point-to-point section for some cut directions ("Invalid hinge line")
        # while the same two points commit as a stepped section, so retry instead of failing.
        order = [kind]
        if not args.get("strict"):
            order += [c for c in ("stepped", "half") if c != kind]
        attempts = []
        last = None
        for candidate in order:
            try:
                view = build(candidate)
            except Exception as exc:
                last = exc
                text = "%s" % exc
                attempts.append("%s: %s" % (candidate, text))
                if "hinge line" not in text and "associativity" not in text:
                    raise
                continue
            result = {"view": view_summary(view), "parent": fix_text(parent.Name),
                      "type": candidate, "at": [round(float(at[0]), 2), round(float(at[1]), 2)],
                      "requested": kind}
            if attempts:
                result["fellBack"] = True
                result["note"] = ("NX rejected %s for this cut line (%s); the same cut went in "
                                  "as %s" % (kind, attempts[0].split(": ", 1)[-1], candidate))
            return result
        raise RuntimeError("no section type would take this cut line -- %s" % "; ".join(attempts))

    def cmd_detail_view(args):
        part = display_part()
        parent = pick_view(part, args.get("parent"))
        center = args.get("center")
        radius = args.get("radius")
        if not center or radius is None:
            raise RuntimeError("center [x,y] and radius (both in the parent view's model space) are required")
        shape = str(args.get("shape") or "circular").strip().lower()
        ratio = float(args.get("scale") or 2.0)
        cx, cy = sheet_of(parent, center[0], center[1])
        r_sheet = float(radius) * float(parent.Scale)

        known = {v.Tag for v in drafting_views(part)}
        builder = part.DraftingViews.CreateDetailViewBuilder(None)
        builder.Parent.View.Value = parent
        if shape.startswith("rect"):
            builder.Type = NX.Drawings.DetailViewBuilderTypes.RectangleByCenterAndCorner
            second = (cx + r_sheet, cy - r_sheet)
        else:
            builder.Type = NX.Drawings.DetailViewBuilderTypes.Circular
            second = (cx + r_sheet, cy)
        builder.BoundaryPoint1 = part.Points.CreatePoint(pnt(cx, cy, 0.0))
        builder.BoundaryPoint2 = part.Points.CreatePoint(pnt(second[0], second[1], 0.0))
        builder.Scale.ScaleType = NX.Drawings.ViewScaleBuilderType.Ratio
        builder.Scale.Numerator = ratio
        builder.Scale.Denominator = 1.0
        at = args.get("at") or free_spot(part)
        builder.Origin.Placement.Value = part.Points.CreatePoint(pnt(at[0], at[1], 0.0))
        builder.Validate()
        builder.Commit()
        fresh = [v for v in drafting_views(part) if v.Tag not in known]
        if not fresh:
            raise RuntimeError("the detail view was committed but no new view appeared")
        return {"view": view_summary(fresh[-1]), "parent": fix_text(parent.Name),
                "ratio": ratio, "boundary": [[round(cx, 2), round(cy, 2)],
                                             [round(second[0], 2), round(second[1], 2)]],
                "at": [round(float(at[0]), 2), round(float(at[1]), 2)]}

    def cmd_note(args):
        part = display_part()
        text = args.get("text")
        lines = text.split("\n") if isinstance(text, str) else list(text or [])
        lines = [str(line) for line in lines if str(line).strip()]
        if not lines:
            raise RuntimeError("text is required (a string with \\n, or a list of lines)")
        size = float(args.get("size") or 3.5)
        at = args.get("at") or [20.0, 275.0]
        if len(at) < 3:
            at = [at[0], at[1], 0.0]
        font = args.get("font")
        if not font and any(ord(ch) > 127 for ch in "".join(lines)):
            # NX's latin stroke fonts have no CJK glyphs and draw boxes instead.
            font = "chinesef"

        builder = part.Annotations.CreateDraftingNoteBuilder(None)
        used = None
        if font:
            try:
                index = part.Fonts.AddFont(str(font), NX.FontCollectionType.Nx)
            except Exception as error:
                raise RuntimeError("font %r is not an NX character font (try chinesef, chineset, "
                                   "kanji, blockfont, Arial): %s" % (font, error))
            builder.Style.LetteringStyle.GeneralTextFont = int(index)
            used = {"name": str(font), "index": int(index)}
        try:
            builder.Style.LetteringStyle.GeneralTextSize = size
        except Exception as error:
            out("text size not set:", repr(error))
        builder.Text.SetEditorText(lines)
        builder.Origin.OriginPoint = pnt(float(at[0]), float(at[1]), float(at[2]))
        builder.Validate()
        note = builder.Commit()
        return {"lines": len(lines), "text": lines, "font": used, "size": size,
                "at": [round(float(v), 2) for v in at], "note": str(note)}

    def cmd_whoami(args):
        part = display_part()
        return part_info(part)

    def cmd_exec(args):
        part = state["part"]
        env = {"NXOpen": NXOpen, "NX": NX, "s": s, "w": out, "os": os, "json": json, "part": part}
        code = args.get("code") or ""
        buf = []
        env["w"] = lambda *p: buf.append(" ".join(str(x) for x in p))
        try:
            exec(compile(code, "<exec>", "exec"), env)
        except Exception as error:
            # Keep whatever the script reported before it died: probes rely on it.
            state["part"] = env.get("part", part)
            raise RuntimeError("\n".join(buf[-80:])
                               + "\n--- ERROR: %s: %s" % (type(error).__name__, error))
        state["part"] = env.get("part", part)
        return {"out": buf}

    # ------------------------------------------------------------------
    # modelling
    # ------------------------------------------------------------------
    def pnt(x, y, z):
        point = NX.Point3d()
        point.X, point.Y, point.Z = float(x), float(y), float(z)
        return point

    def vec(x, y, z):
        direction = NX.Vector3d()
        direction.X, direction.Y, direction.Z = float(x), float(y), float(z)
        return direction

    def num(value, default=None):
        if value is None:
            if default is None:
                raise RuntimeError("a size value is required")
            return default
        return float(value)

    def expr_text(value):
        return ("%.6f" % float(value)).rstrip("0").rstrip(".")

    def axis_vec(axis):
        axis = axis or [0, 0, 1]
        return vec(axis[0], axis[1], axis[2])

    def axis_object(part, origin, direction):
        """NXOpen.Axis from a point plus a direction (what revolve needs)."""
        update = NX.SmartObject.UpdateOption.WithinModeling
        point = part.Points.CreatePoint(pnt(origin[0], origin[1], origin[2]))
        direction_object = part.Directions.CreateDirection(
            pnt(origin[0], origin[1], origin[2]), axis_vec(direction), update)
        return part.Axes.CreateAxis(point, direction_object, update)

    def feature_out(part, feature, extra=None):
        result = {
            "feature": fix_text(getattr(feature, "Name", "") or ""),
            "type": type(feature).__name__,
            "measure": measure(part),
        }
        if extra:
            result.update(extra)
        return result

    def cmd_new(args):
        path = os.path.abspath(args["path"])
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        # NX refuses to create a part over a file that is still loaded in the session,
        # so every part holding that path is closed first (PartCloseResponses is an
        # object type here, not an enum: None is the accepted "no responses" value).
        wanted = os.path.normcase(path)
        for loaded in list(s.Parts):
            try:
                loaded_path = os.path.normcase(fix_text(loaded.FullPath))
            except Exception:
                continue
            if loaded_path != wanted:
                continue
            try:
                loaded.Close(NX.BasePartCloseWholeTree.FalseValue,
                             NX.BasePartCloseModified.CloseModified, None)
            except Exception as failure:
                raise RuntimeError("%s is open in this session and could not be closed (%s); "
                                   "save it elsewhere or pick another path" % (path, failure))
            if state.get("part") is loaded:
                state["part"] = None
        if os.path.exists(path):
            if not args.get("overwrite"):
                raise RuntimeError("file exists: %s (pass overwrite: true)" % path)
            try:
                os.remove(path)
            except OSError as failure:
                raise RuntimeError("cannot replace %s: %s" % (path, failure))
        part = s.Parts.NewDisplay(path, NX.PartUnits.Millimeters)
        s.Parts.SetDisplay(part, True, True)
        state["part"] = part
        info = part_info(part)
        info["created"] = path
        return info

    def cmd_primitive(args):
        part = display_part()
        kind = (args.get("kind") or "block").lower()
        at = args.get("at") or [0, 0, 0]
        if kind in ("block", "box"):
            builder = part.Features.CreateBlockFeatureBuilder(NX.Features.Block.Null)
            builder.SetOriginAndLengths(
                pnt(at[0], at[1], at[2]),
                expr_text(num(args.get("length"), 10)),
                expr_text(num(args.get("width"), 10)),
                expr_text(num(args.get("height"), 10)),
            )
        elif kind in ("cylinder", "cyl"):
            builder = part.Features.CreateCylinderBuilder(NX.Features.Cylinder.Null)
            diameter = args.get("diameter")
            if diameter is None:
                diameter = 2 * num(args.get("radius"))
            builder.Diameter.SetFormula(expr_text(diameter))
            builder.Height.SetFormula(expr_text(num(args.get("height"), 10)))
            builder.Origin = pnt(at[0], at[1], at[2])
            builder.Direction = axis_vec(args.get("axis"))
        elif kind == "cone":
            builder = part.Features.CreateConeBuilder(NX.Features.Cone.Null)
            builder.BaseDiameter.SetFormula(expr_text(num(args.get("baseDiameter") or 2 * num(args.get("baseRadius")))))
            builder.TopDiameter.SetFormula(expr_text(num(args.get("topDiameter"), 0)))
            builder.Height.SetFormula(expr_text(num(args.get("height"), 10)))
            if hasattr(builder, "Axis"):
                try:
                    builder.Axis = axis_vec(args.get("axis"))
                except Exception:
                    pass
        elif kind == "sphere":
            builder = part.Features.CreateSphereBuilder(NX.Features.Sphere.Null)
            diameter = args.get("diameter")
            if diameter is None:
                diameter = 2 * num(args.get("radius"))
            builder.Diameter.SetFormula(expr_text(diameter))
            builder.CenterPoint = part.Points.CreatePoint(pnt(at[0], at[1], at[2]))
        else:
            raise RuntimeError("unknown primitive: %s (block, cylinder, cone, sphere)" % kind)
        feature = builder.CommitFeature()
        return feature_out(part, feature, {"kind": kind})

    def pick_bodies(part, spec, allow_empty=False):
        bodies = list(part.Bodies)
        if not bodies:
            # An assembly owns no bodies; its geometry lives in the components. Callers that
            # can deal with that (the exporters) ask for an empty list instead of an error.
            if allow_empty:
                return []
            assembly = getattr(part, "ComponentAssembly", None)
            try:
                leaves = len(component_pool(assembly)) if assembly is not None else 0
            except Exception:
                leaves = 0
            if leaves:
                raise RuntimeError(
                    "this is an assembly: its geometry lives in %d component(s), so there is no "
                    "body to pick here -- use the nx_assembly_* tools, or open the component part "
                    "to work on its geometry" % leaves)
            raise RuntimeError("the part has no solid body yet")
        if spec is None:
            return [bodies[-1]]
        if isinstance(spec, str) and spec.strip().lower() in ("all", "*"):
            return bodies
        picked = []
        for item in spec if isinstance(spec, (list, tuple)) else [spec]:
            if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
                index = int(item)
                if index < 0:
                    index += len(bodies)
                if not 0 <= index < len(bodies):
                    raise RuntimeError("body index out of range: %s (have %d)" % (item, len(bodies)))
                picked.append(bodies[index])
            else:
                match = [b for b in bodies if fix_text(getattr(b, "Name", "") or "") == str(item)]
                if not match:
                    raise RuntimeError("no body named %r (have %r)" % (
                        item, [fix_text(getattr(b, "Name", "") or "") for b in bodies]))
                picked.append(match[0])
        return picked

    BOOLEAN_OPS = {
        "unite": "Unite", "union": "Unite", "add": "Unite", "join": "Unite",
        "subtract": "Subtract", "cut": "Subtract", "difference": "Subtract",
        "intersect": "Intersect", "common": "Intersect", "intersection": "Intersect",
    }

    def cmd_boolean(args):
        part = display_part()
        op = BOOLEAN_OPS.get(str(args.get("op") or "").lower())
        if op is None:
            raise RuntimeError("unknown boolean op: %s (unite, subtract, intersect)" % args.get("op"))
        target = pick_bodies(part, args.get("target"))[0]
        tools = pick_bodies(part, args.get("tools"))
        tools = [t for t in tools if t.Tag != target.Tag]
        if not tools:
            raise RuntimeError("no tool body: pass tools (index or name) different from the target")
        feature = None
        for tool in tools:
            builder = part.Features.CreateBooleanBuilder(NX.Features.BooleanFeature.Null)
            builder.Operation = getattr(NX.Features.FeatureBooleanType, op)
            builder.Target = target
            builder.Tool = tool
            feature = builder.Commit()
        return feature_out(part, feature, {"op": op.lower(), "tools": len(tools)})

    def edge_pool(part, bodies=None):
        pool = []
        for body in (bodies or list(part.Bodies)):
            try:
                pool.extend(list(body.GetEdges()))
            except Exception:
                continue
        return pool

    def vertex_point(vertex):
        """Edge.GetVertices() hands back bare Point3d values in this build, but older
        ones wrap a Vertex whose Coordinates hold the point."""
        if hasattr(vertex, "X") and hasattr(vertex, "Y") and hasattr(vertex, "Z"):
            return (vertex.X, vertex.Y, vertex.Z)
        point = vertex.Coordinates
        return (point.X, point.Y, point.Z)

    def edge_point(edge):
        """A point on the edge: the midpoint of its two vertices when available."""
        try:
            vertices = list(edge.GetVertices())
            if len(vertices) >= 2:
                a = vertex_point(vertices[0])
                b = vertex_point(vertices[1])
                return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, (a[2] + b[2]) / 2.0)
            if len(vertices) == 1:
                return vertex_point(vertices[0])
        except Exception:
            pass
        return None

    def pick_edges(part, spec, bodies=None):
        pool = edge_pool(part, bodies)
        if not pool:
            raise RuntimeError("no edges on the selected bodies")
        if spec is None or spec == "all":
            return pool
        points = spec if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], (list, tuple)) else [spec]
        picked = []
        for target in points:
            best, best_distance = None, None
            for edge in pool:
                point = edge_point(edge)
                if point is None:
                    continue
                distance = sum((point[i] - float(target[i])) ** 2 for i in range(3))
                if best_distance is None or distance < best_distance:
                    best, best_distance = edge, distance
            if best is None:
                raise RuntimeError("cannot locate an edge near %r" % (target,))
            picked.append(best)
        return picked

    def edge_collector(part, edges):
        collector = part.ScCollectors.CreateCollector()
        rule = part.ScRuleFactory.CreateRuleEdgeDumb(edges)
        collector.ReplaceRules([rule], False)
        return collector

    def cmd_fillet(args):
        part = display_part()
        radius = num(args.get("radius"))
        bodies = pick_bodies(part, args.get("bodies")) if args.get("bodies") is not None else list(part.Bodies)
        edges = pick_edges(part, args.get("edges"), bodies)
        builder = part.Features.CreateEdgeBlendBuilder(NX.Features.EdgeBlend.Null)
        builder.AddChainset(edge_collector(part, edges), expr_text(radius))
        feature = builder.CommitFeature()
        return feature_out(part, feature, {"radius": radius, "edges": len(edges)})

    def is_linear_edge(edge):
        try:
            return edge.SolidEdgeType == NX.Edge.EdgeType.Linear
        except Exception:
            return False

    def cmd_chamfer(args):
        part = display_part()
        distance = num(args.get("distance"))
        bodies = pick_bodies(part, args.get("bodies")) if args.get("bodies") is not None else list(part.Bodies)
        edges = pick_edges(part, args.get("edges"), bodies)
        # Chamfer rejects edges that are already blended or curved: fall back to
        # the straight ones before giving up.
        attempts = [edges]
        linear = [e for e in edges if is_linear_edge(e)]
        if linear and len(linear) != len(edges):
            attempts.append(linear)
        error = None
        for index, pool in enumerate(attempts):
            try:
                builder = part.Features.CreateChamferBuilder(NX.Features.Chamfer.Null)
                builder.SmartCollector = edge_collector(part, pool)
                builder.FirstOffset = expr_text(distance)
                builder.SecondOffset = expr_text(distance)
                feature = builder.CommitFeature()
                return feature_out(part, feature, {"distance": distance, "edges": len(pool), "attempt": index})
            except Exception as failure:
                error = failure
        raise RuntimeError("chamfer failed on %d edges: %s" % (len(edges), error))

    def polygon_curves(part, points, plane, base):
        """Create straight curves for a closed 2D polygon on one of the principal planes."""
        plane = (plane or "xy").lower()
        if plane not in ("xy", "xz", "yz"):
            raise RuntimeError("plane must be xy, xz or yz (got %s)" % plane)

        def to3(point):
            a, b = float(point[0]), float(point[1])
            if plane == "xy":
                return (a, b, base)
            if plane == "xz":
                return (a, base, b)
            return (base, a, b)

        curves = []
        for index in range(len(points)):
            start = to3(points[index])
            end = to3(points[(index + 1) % len(points)])
            curves.append(part.Curves.CreateLine(pnt(*start), pnt(*end)))
        section = part.Sections.CreateSection(0.0095, 0.01, 0.5)
        rule = part.ScRuleFactory.CreateRuleCurveDumb(curves)
        helper = to3(points[0])
        section.AddToSection([rule], curves[0], NX.NXObject.Null, NX.NXObject.Null,
                             pnt(*helper), NX.Section.Mode.Create, False)
        return section, helper, curves

    def cmd_extrude(args):
        part = display_part()
        points = args.get("profile") or []
        if len(points) < 3:
            raise RuntimeError("profile needs at least 3 points [[a,b],...]")
        height = num(args.get("height"), 10)
        plane = (args.get("plane") or "xy").lower()
        base = float(args.get("base") or 0.0)
        section, helper, curves = polygon_curves(part, points, plane, base)
        normal = {"xy": (0, 0, 1), "xz": (0, -1, 0), "yz": (1, 0, 0)}[plane]
        builder = part.Features.CreateExtrudeBuilder(NX.Features.Extrude.Null)
        builder.Section = section
        builder.Direction = part.Directions.CreateDirection(
            pnt(*helper), vec(*normal), NX.SmartObject.UpdateOption.WithinModeling)
        builder.Limits.StartExtend.Value.SetFormula("0")
        builder.Limits.EndExtend.Value.SetFormula(expr_text(height))
        draft_angle = num(args.get("draft"), 0.0)
        if draft_angle:
            # The drafting feature refuses this batch session, but an extrusion carries its own
            # taper: SimpleFromProfile + a draft angle gives a real moulded wall.
            builder.Draft.DraftOption = NX.GeometricUtilities.SimpleDraftSimpleDraftType.SimpleFromProfile
            builder.Draft.DraftAngle.SetFormula(expr_text(draft_angle))
        feature = builder.CommitFeature()
        return feature_out(part, feature, {"height": height, "plane": plane, "draft": draft_angle})

    def cmd_revolve(args):
        part = display_part()
        points = args.get("profile") or []
        if len(points) < 3:
            raise RuntimeError("profile needs at least 3 points [[radius, height],...]")
        angle = num(args.get("angle"), 360)
        plane = (args.get("plane") or "xz").lower()
        base = float(args.get("base") or 0.0)
        section, helper, curves = polygon_curves(part, points, plane, base)
        axis = args.get("axis") or [0, 0, 1]
        builder = part.Features.CreateRevolveBuilder(NX.Features.Revolve.Null)
        builder.Section = section
        builder.Axis = axis_object(part, args.get("axisPoint") or [0.0, 0.0, 0.0], axis)
        builder.Limits.StartExtend.Value.SetFormula("0")
        builder.Limits.EndExtend.Value.SetFormula(expr_text(angle))
        feature = builder.CommitFeature()
        return feature_out(part, feature, {"angle": angle, "axis": list(axis)})

    def placement_face(body, at):
        """Pick the flat face a native hole should sit on: planar, and nearest the given height."""
        best = None
        best_gap = None
        for face in list(body.GetFaces()):
            info = s.Measurement.GetFaceProperties(
                [face], 0.99, NX.Measurement.AlternateFace.Radius, True)
            area, radius, cog = info[0], info[2], info[3]
            if radius > 0.001 or area < 1.0:
                continue
            gap = abs(cog.Z - float(at[2]))
            if best_gap is None or gap < best_gap:
                best = face
                best_gap = gap
        if best is None:
            raise RuntimeError("no planar face found near z=%.3f for a native hole" % float(at[2]))
        return best

    def cmd_hole(args):
        part = display_part()
        target = pick_bodies(part, args.get("target"))[0]
        at = args.get("at") or [0, 0, 0]
        diameter = args.get("diameter")
        if diameter is None:
            diameter = 2 * num(args.get("radius"))
        depth = num(args.get("depth"), 100)
        if args.get("native"):
            # A real Hole feature: parametric, and it shows up in the feature tree as a hole
            # rather than as a boolean of a cutting cylinder.
            face = placement_face(target, at)
            builder = part.Features.CreateHoleFeatureBuilder(NX.Features.Hole.Null)
            builder.SetSimpleHole(
                pnt(float(at[0]), float(at[1]), float(at[2])), False, face, expr_text(diameter))
            builder.SetDepth(expr_text(depth))
            feature = builder.CommitFeature()
            return feature_out(part, feature, {
                "diameter": diameter, "depth": depth, "native": True})
        builder = part.Features.CreateCylinderBuilder(NX.Features.Cylinder.Null)
        builder.Diameter.SetFormula(expr_text(diameter))
        direction = axis_vec(args.get("axis"))
        # The tool runs one millimetre past the requested depth so a through cut is clean, but
        # that overshoot must sit outside the material: start the cylinder one millimetre
        # *before* the given point instead of pushing its bottom deeper than asked.
        builder.Height.SetFormula(expr_text(depth + 1.0))
        builder.Origin = pnt(at[0] - direction.X, at[1] - direction.Y, at[2] - direction.Z)
        builder.Direction = direction
        tool = builder.CommitFeature()
        tool_body = [b for b in list(part.Bodies) if b.Tag != target.Tag]
        if not tool_body:
            raise RuntimeError("the cutting cylinder did not create a body")
        boolean = part.Features.CreateBooleanBuilder(NX.Features.BooleanFeature.Null)
        boolean.Operation = NX.Features.FeatureBooleanType.Subtract
        boolean.Target = target
        boolean.Tool = tool_body[-1]
        feature = boolean.Commit()
        return feature_out(part, feature, {"diameter": diameter, "depth": depth})

    THREAD_ROWS = {"rows": None}

    def var_num(value, fallback=0.0):
        """The standards table writes NA for values that do not apply to a row."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def thread_rows():
        """The installed thread standards table: one row per (standard, size)."""
        if THREAD_ROWS["rows"] is not None:
            return THREAD_ROWS["rows"]
        base = (os.environ.get("UGII_BASE_DIR") or os.environ.get("UGII_ROOT_DIR") or "").strip()
        path = os.path.join(base, "UGII", "modeling_standards", "NX_Thread_Standard.xml")
        rows = []
        if base and os.path.isfile(path):
            import xml.etree.ElementTree as ElementTree
            for node in ElementTree.parse(path).getroot():
                attrs = node.attrib
                # every size is listed three times, once per radial engage class
                if attrs.get("RadialEngage", "0.75") != "0.75":
                    continue
                row = {
                    "standard": attrs.get("Standard", ""),
                    "size": attrs.get("Size", ""),
                    "unit": attrs.get("Unit", ""),
                    "major": var_num(attrs.get("MajorDiameter")),
                    "minor": var_num(attrs.get("MinorDiameter")),
                    "tap": var_num(attrs.get("TapDrillDia")),
                    "shaft": var_num(attrs.get("ShaftDiameter")),
                    "pitch": var_num(attrs.get("Pitch")),
                    "angle": var_num(attrs.get("Angle"), 60.0),
                }
                # the "(n x D)" variants only republish a depth, they are not sizes
                row["variant"] = "(" in row["size"]
                if row["pitch"] > 0 and (row["major"] > 0 or row["tap"] > 0):
                    rows.append(row)
        THREAD_ROWS["rows"] = rows
        return rows

    def thread_row(standard, size, diameter, external):
        """Find the standards row asked for, or the one closest to the measured cylinder."""
        rows = thread_rows()
        if not rows:
            return None
        if standard:
            wanted = [r for r in rows if r["standard"].lower() == str(standard).lower()]
            if not wanted:
                known = sorted({r["standard"] for r in rows})
                raise RuntimeError("unknown thread standard %r; known standards: %s" % (
                    standard, ", ".join(known)))
        else:
            wanted = [r for r in rows if r["standard"].lower() == "metric coarse"] or rows
        if size:
            wanted_size = str(size).strip()
            hit = [r for r in wanted if r["size"].lower() == wanted_size.lower()]
            if not hit:
                # NX compares these strings literally, so people trip over the spacing
                squeezed = wanted_size.lower().replace(" ", "")
                hint = [r["size"] for r in wanted
                        if r["size"].lower().replace(" ", "") == squeezed]
                if hint:
                    raise RuntimeError(
                        "no size %r in %s, but %r exists — the standards table needs its exact "
                        "spelling (including the spaces)" % (size, wanted[0]["standard"], hint[0]))
                near = sorted(wanted, key=lambda r: abs(r["major"] - diameter))[:6]
                raise RuntimeError("no size %r in %s; nearest sizes: %s" % (
                    size, wanted[0]["standard"], ", ".join(r["size"] for r in near)))
            return hit[0]
        plain = [r for r in wanted if not r["variant"]] or wanted
        key = "major" if external else "tap"
        best = None
        best_gap = None
        for row in plain:
            if row[key] <= 0:
                continue
            gap = abs(row[key] - diameter)
            if best_gap is None or gap < best_gap:
                best, best_gap = row, gap
        return best

    def cylinder_face(body, at, radius=None):
        """Pick the cylindrical face nearest a point: a hole wall or a boss wall."""
        best = None
        best_score = None
        best_radius = None
        for face in list(body.GetFaces()):
            info = s.Measurement.GetFaceProperties(
                [face], 0.99, NX.Measurement.AlternateFace.Radius, True)
            area, r, cog = float(info[0]), float(info[2]), info[3]
            if r <= 0.001 or area < 1.0:
                continue
            if radius is not None and abs(r - radius) > 0.5:
                continue
            radial = math.hypot(float(at[0]) - cog.X, float(at[1]) - cog.Y)
            score = abs(radial - r) + 0.001 * abs(float(at[2]) - cog.Z)
            if best_score is None or score < best_score:
                best, best_score, best_radius = face, score, r
        if best is None:
            raise RuntimeError("no cylindrical face near %s to thread" % (list(at),))
        return best, best_radius

    def cmd_thread(args):
        """A real Thread feature. NX reads the standard/size from NX_Thread_Standard.xml."""
        part = display_part()
        body = pick_bodies(part, args.get("target"))[0]
        at = args.get("at") or [0, 0, 0]
        external = bool(args.get("external"))
        radius = None
        if args.get("diameter") is not None:
            radius = 0.5 * float(args.get("diameter"))
        elif args.get("radius") is not None:
            radius = float(args.get("radius"))
        face, face_radius = cylinder_face(body, at, radius)
        row = thread_row(args.get("standard"), args.get("size"), 2 * face_radius, external)
        if row is None:
            raise RuntimeError(
                "the thread standards table (UGII/modeling_standards/NX_Thread_Standard.xml) "
                "is not readable in this NX installation")
        start = placement_face(body, at)
        ttype = "Detailed" if str(args.get("type") or "symbolic").lower().startswith("detail") \
            else "Symbolic"
        length = args.get("length")
        engage = str(args.get("engage") or "0.75")

        def build(manual):
            builder = part.Features.CreateThreadBuilder(NX.Features.Thread.Null)
            builder.CylindricalFace.Value = face
            builder.StartObject.Value = start
            builder.ThreadStandard = row["standard"]
            builder.ThreadSize = row["size"]
            builder.RadialEngage = engage
            builder.ThreadType = getattr(NX.Features.ThreadBuilderType, ttype)
            builder.ThreadInput = (NX.Features.ThreadBuilderInput.Manual if manual
                                   else NX.Features.ThreadBuilderInput.ThreadTable)
            if manual:
                builder.MajorDiameterExp.SetFormula(expr_text(row["major"]))
                builder.MinorDiameterExp.SetFormula(expr_text(row["minor"]))
                builder.PitchExp.SetFormula(expr_text(row["pitch"]))
            if external:
                builder.ShaftPreference = NX.Features.ThreadBuilderShaftSizePreference.ShaftDiameter
                builder.ShaftDiameterExp.SetFormula(
                    expr_text(row["major"] - max(0.05, row["pitch"] * 0.06)))
            else:
                builder.HolePreference = NX.Features.ThreadBuilderHoleSizePreference.TapDrillDiameter
                builder.TapDrillDiameterExp.SetFormula(expr_text(row["tap"]))
            builder.ThreadLimit = (NX.Features.ThreadBuilderLimitOption.Value if length
                                   else NX.Features.ThreadBuilderLimitOption.Full)
            if length:
                builder.ThreadLength.SetFormula(expr_text(length))
            if str(args.get("handedness") or "right").lower().startswith("l"):
                builder.ThreadHandedness = NX.Features.ThreadBuilderHandedness.LeftHand
            builder.Validate()
            return builder.CommitFeature()

        try:
            feature = build(False)
        except Exception as first_error:
            message = str(first_error)
            if "Standard data not found" in message or "Fit data not found" in message:
                feature = build(True)   # fall back to explicit diameters
            else:
                raise
        inch = row["unit"].lower() == "inch"
        extra = {
            "feature": (getattr(feature, "Name", "") or
                        "%s %s Thread" % (row["size"], ttype)),
            "standard": row["standard"], "size": row["size"], "threadType": ttype,
            "unit": row["unit"],
            "pitch": row["pitch"] * (25.4 if inch else 1.0),
            "pitchInch": row["pitch"] if inch else None,
            "majorDiameter": row["major"] * (25.4 if inch else 1.0),
            "tapDrill": row["tap"] * (25.4 if inch else 1.0),
            "external": external, "length": length,
        }
        return feature_out(part, feature, extra)

    def face_listing(part, body):
        rows = []
        faces = list(body.GetFaces())
        try:
            props = None
            for index, face in enumerate(faces):
                row = {"index": index, "tag": face.Tag}
                try:
                    row["type"] = str(face.SolidFaceType)
                except Exception:
                    pass
                try:
                    info = s.Measurement.GetFaceProperties(
                        [face], 0.99, NX.Measurement.AlternateFace.Radius, True)
                    row["area"] = round(float(info[0]), 4)
                    cog = info[3]
                    row["cog"] = [round(cog.X, 4), round(cog.Y, 4), round(cog.Z, 4)]
                except Exception:
                    pass
                rows.append(row)
        except Exception:
            pass
        return rows

    def cmd_faces(args):
        part = display_part()
        body = pick_bodies(part, args.get("target"))[0]
        body_name = fix_text(getattr(body, "JournalIdentifier", "") or "")
        return {"body": body_name, "faceCount": len(list(body.GetFaces())),
                "faces": face_listing(part, body)}

    def cmd_shell(args):
        part = display_part()
        target = pick_bodies(part, args.get("target"))[0]
        thickness = num(args.get("thickness"), 1)
        builder = part.Features.CreateShellBuilder(NX.Features.Shell.Null)
        builder.Body = target
        builder.SetDefaultThickness(expr_text(thickness))
        # without an explicit tolerance the shell feature fails with "Tolerance error"
        builder.Tolerance = float(args.get("tolerance") or 0.01)
        open_faces = args.get("openFaces") or []
        opened = 0
        if open_faces:
            faces = list(target.GetFaces())
            rows = None
            chosen = []
            for item in open_faces:
                if isinstance(item, (list, tuple)):
                    if rows is None:
                        rows = face_listing(part, target)
                    best = None
                    best_distance = None
                    for row in rows:
                        cog = row.get("cog")
                        if not cog:
                            continue
                        distance = sum((cog[i] - float(item[i])) ** 2 for i in range(3))
                        if best_distance is None or distance < best_distance:
                            best_distance = distance
                            best = row["index"]
                    if best is None:
                        raise RuntimeError("no face had a usable centroid to match %r" % (item,))
                    chosen.append(faces[best])
                else:
                    index = int(item)
                    if index < 0 or index >= len(faces):
                        raise RuntimeError("face index %d out of range 0..%d" % (index, len(faces) - 1))
                    chosen.append(faces[index])
            collector = part.ScCollectors.CreateCollector()
            collector.ReplaceRules([part.ScRuleFactory.CreateRuleFaceDumb(chosen)], False)
            builder.RemovedFacesCollector = collector
            opened = len(chosen)
        feature = builder.CommitFeature()
        return feature_out(part, feature, {"thickness": thickness, "openFaces": opened})

    def cmd_save(args):
        part = display_part()
        path = args.get("path")
        if path:
            path = os.path.abspath(path)
            current = fix_text(part.FullPath)
            if os.path.normcase(path) == os.path.normcase(current):
                part.Save(NX.BasePartSaveComponents.FalseValue, NX.BasePartCloseAfterSave.FalseValue)
            else:
                parent = os.path.dirname(path)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent)
                if os.path.exists(path) and not args.get("overwrite"):
                    raise RuntimeError("file exists: %s" % path)
                part.SaveAs(path)
        else:
            part.Save(NX.BasePartSaveComponents.FalseValue, NX.BasePartCloseAfterSave.FalseValue)
        info = part_info(part)
        info["saved"] = True
        return info

    def cmd_polygon(args):
        # convenience: closed polygon sketch curves only (no solid)
        part = display_part()
        points = args.get("profile") or []
        if len(points) < 3:
            raise RuntimeError("profile needs at least 3 points")
        section, helper, curves = polygon_curves(part, points, args.get("plane"), float(args.get("base") or 0.0))
        return {"curves": len(curves), "helper": list(helper)}


    # ------------------------------------------------------------------
    # curves and surfaces
    # ------------------------------------------------------------------
    def point_list(raw, what="points", minimum=2):
        points = []
        for item in raw or []:
            if isinstance(item, dict):
                item = [item.get("x", 0), item.get("y", 0), item.get("z", 0)]
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                raise RuntimeError("%s must be a list of [x, y, z] points" % what)
            points.append([float(item[0]), float(item[1]), float(item[2])])
        if len(points) < minimum:
            raise RuntimeError("%s needs at least %d point(s)" % (what, minimum))
        return points

    def perp_pair(direction):
        """Two unit vectors perpendicular to direction and to each other."""
        x, y, z = direction.X, direction.Y, direction.Z
        length = math.sqrt(x * x + y * y + z * z) or 1.0
        x, y, z = x / length, y / length, z / length
        helper = (0.0, 0.0, 1.0) if abs(z) < 0.9 else (1.0, 0.0, 0.0)
        ux = y * helper[2] - z * helper[1]
        uy = z * helper[0] - x * helper[2]
        uz = x * helper[1] - y * helper[0]
        ulen = math.sqrt(ux * ux + uy * uy + uz * uz) or 1.0
        ux, uy, uz = ux / ulen, uy / ulen, uz / ulen
        vx = y * uz - z * uy
        vy = z * ux - x * uz
        vz = x * uy - y * ux
        return vec(ux, uy, uz), vec(vx, vy, vz)

    def curve_through(part, points, degree=3, closed=False):
        """A real curve through the points: a line for two, a studio spline beyond."""
        if len(points) == 2 and not closed:
            return part.Curves.CreateLine(pnt(*points[0]), pnt(*points[1]))
        builder = part.Features.CreateStudioSplineBuilderEx(None)
        builder.Type = NX.Features.StudioSplineBuilderExTypes.ThroughPoints
        builder.Degree = int(degree)
        builder.IsPeriodic = bool(closed)
        manager = builder.ConstraintManager
        for point in points:
            data = manager.CreateGeometricConstraintData()
            data.Point = part.Points.CreatePoint(pnt(*point))
            manager.Append(data)
        builder.Validate()
        builder.CommitFeature()
        return builder.Curve

    def curve_section(part, curve):
        # the single curve overload of CreateSection carries the tolerances the builders want
        return part.Sections.CreateSection(curve)

    def sheet_measure(bodies):
        area = 0.0
        faces = 0
        for body in bodies:
            for face in body.GetFaces():
                faces += 1
                area += s.Measurement.GetFaceProperties(
                    [face], 0.999, NX.Measurement.AlternateFace.Radius, False)[0]
        return {"area": round(area, 4), "faces": faces, "bodies": len(bodies)}

    def cmd_curve(args):
        part = display_part()
        kind = (args.get("kind") or "line").lower()
        points = point_list(args.get("points"), "points", 1)
        if kind == "line":
            if len(points) != 2:
                raise RuntimeError("a line takes exactly 2 points (start, end)")
            curve = part.Curves.CreateLine(pnt(*points[0]), pnt(*points[1]))
        elif kind == "arc":
            if len(points) != 3:
                raise RuntimeError("an arc takes 3 points: start, a point on the arc, end")
            curve = part.Curves.CreateArc(pnt(*points[0]), pnt(*points[1]), pnt(*points[2]), False)[0]
        elif kind in ("circle", "round"):
            radius = num(args.get("radius") or args.get("diameter") and num(args.get("diameter")) / 2.0)
            if radius is None:
                raise RuntimeError("a circle needs radius (or diameter)")
            x_dir, y_dir = perp_pair(axis_vec(args.get("axis")))
            curve = part.Curves.CreateArc(pnt(*points[0]), x_dir, y_dir, float(radius), 0.0, 2.0 * math.pi)
        elif kind in ("spline", "curve"):
            curve = curve_through(part, points, args.get("degree") or 3, bool(args.get("closed")))
        else:
            raise RuntimeError("unknown curve: %s (line, arc, circle, spline)" % kind)
        result = {
            "kind": kind,
            "type": type(curve).__name__,
            "points": len(points),
            "length": round(curve.GetLength(), 4),
            "measure": measure(part),
        }
        return result

    def cmd_surface(args):
        part = display_part()
        kind = (args.get("kind") or "through_curves").lower()
        if kind in ("through_curves", "loft", "ruled"):
            raw = args.get("sections") or []
            if len(raw) < 2:
                raise RuntimeError("through_curves needs sections: at least 2 lists of points")
            curves = [curve_through(part, point_list(section, "each section"), args.get("degree") or 3)
                      for section in raw]
            builder = part.Features.CreateThroughCurvesBuilder(None)
            builder.BodyPreference = NXOpen.Features.ThroughCurvesBuilderBodyPreferenceTypes.Sheet
            builder.SectionsList.SetContents([curve_section(part, curve) for curve in curves])
        elif kind in ("mesh", "through_curve_mesh"):
            primary = args.get("primary") or args.get("sections") or []
            cross = args.get("cross") or []
            if len(primary) < 2 or len(cross) < 2:
                raise RuntimeError("a mesh surface needs primary (>=2) and cross (>=2) point lists")
            primaries = [curve_through(part, point_list(item, "each primary curve"), args.get("degree") or 3)
                         for item in primary]
            crosses = [curve_through(part, point_list(item, "each cross curve"), args.get("degree") or 3)
                       for item in cross]
            builder = part.Features.CreateThroughCurveMeshBuilder(None)
            builder.BodyPreference = NXOpen.Features.ThroughCurveMeshBuilderBodyPreferenceTypes.Sheet
            builder.PrimaryCurvesList.SetContents([curve_section(part, c) for c in primaries])
            builder.CrossCurvesList.SetContents([curve_section(part, c) for c in crosses])
        elif kind in ("extrude", "extruded"):
            profile = args.get("profile") or args.get("points") or []
            curve = curve_through(part, point_list(profile, "profile", 2), args.get("degree") or 3,
                                  bool(args.get("closed")))
            builder = part.Features.CreateExtrudeBuilder(None)
            builder.Section = curve_section(part, curve)
            builder.Direction = part.Directions.CreateDirection(
                pnt(0, 0, 0), axis_vec(args.get("direction") or args.get("axis")),
                NX.SmartObject.UpdateOption.WithinModeling)
            builder.Limits.StartExtend.Value.SetFormula("0")
            builder.Limits.EndExtend.Value.SetFormula(expr_text(num(args.get("distance"), 20)))
            builder.BooleanOperation.Type = NXOpen.GeometricUtilities.BooleanOperationBooleanType.Create
        else:
            raise RuntimeError("unknown surface: %s (through_curves, mesh, extrude)" % kind)
        builder.Validate()
        feature = builder.CommitFeature()
        bodies = list(feature.GetBodies())
        result = feature_out(part, feature, {"kind": kind})
        result.update(sheet_measure(bodies))
        return result

    # ------------------------------------------------------------------
    # datums and sketches
    # ------------------------------------------------------------------
    SKETCH_PLANES = {
        "xy": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
               lambda u, v, base: (u, v, base)),
        "xz": ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
               lambda u, v, base: (u, base, v)),
        "yz": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
               lambda u, v, base: (base, u, v)),
    }

    def unit_vec(direction):
        if isinstance(direction, (tuple, list)):
            values = [float(v) for v in direction]
        else:
            values = [float(direction.X), float(direction.Y), float(direction.Z)]
        length = math.sqrt(values[0] * values[0] + values[1] * values[1] + values[2] * values[2])
        if length <= 1e-12:
            raise RuntimeError("a direction cannot be zero")
        return tuple(v / length for v in values)

    def matrix_from_axes(x_dir, y_dir, z_dir):
        matrix = NX.Matrix3x3()
        matrix.Xx, matrix.Xy, matrix.Xz = x_dir
        matrix.Yx, matrix.Yy, matrix.Yz = y_dir
        matrix.Zx, matrix.Zy, matrix.Zz = z_dir
        return matrix

    def plane_frame(normal):
        """Two unit vectors spanning the plane whose normal is given."""
        normal = unit_vec(normal)
        helper = (0.0, 0.0, 1.0) if abs(normal[2]) < 0.9 else (1.0, 0.0, 0.0)
        u_vec = unit_vec((helper[1] * normal[2] - helper[2] * normal[1],
                          helper[2] * normal[0] - helper[0] * normal[2],
                          helper[0] * normal[1] - helper[1] * normal[0]))
        v_vec = (normal[1] * u_vec[2] - normal[2] * u_vec[1],
                 normal[2] * u_vec[0] - normal[0] * u_vec[2],
                 normal[0] * u_vec[1] - normal[1] * u_vec[0])
        return u_vec, v_vec

    def one_point(raw, what):
        if raw is None:
            return (0.0, 0.0, 0.0)
        values = [float(v) for v in raw]
        if len(values) != 3:
            raise RuntimeError("%s needs 3 numbers [x, y, z] (got %d)" % (what, len(values)))
        return tuple(values)

    def cmd_datum(args):
        part = display_part()
        kind = (args.get("kind") or "plane").lower()
        if kind == "plane":
            origin = one_point(args.get("origin"), "origin")
            normal = unit_vec(axis_vec(args.get("normal") or [0, 0, 1]))
            u_vec, v_vec = plane_frame(normal)
            plane = part.Datums.CreateFixedDatumPlane(
                pnt(*origin), matrix_from_axes(u_vec, v_vec, normal))
            return {
                "kind": "plane",
                "datum": getattr(plane, "Name", "") or "datum plane",
                "origin": [round(v, 4) for v in origin],
                "normal": [round(v, 4) for v in normal],
            }
        if kind == "axis":
            start = one_point(args.get("start") or args.get("point"), "start")
            if args.get("end") is not None:
                end = one_point(args.get("end"), "end")
            else:
                direction = unit_vec(axis_vec(args.get("direction") or [0, 0, 1]))
                length = num(args.get("length"), 100.0)
                end = tuple(start[i] + direction[i] * length for i in range(3))
            axis = part.Datums.CreateFixedDatumAxis(pnt(*start), pnt(*end))
            return {
                "kind": "axis",
                "datum": getattr(axis, "Name", "") or "datum axis",
                "start": [round(v, 4) for v in start],
                "end": [round(v, 4) for v in end],
            }
        if kind == "point":
            at = one_point(args.get("origin") or args.get("point"), "origin")
            point = part.Points.CreatePoint(pnt(*at))
            return {"kind": "point", "datum": getattr(point, "Name", "") or "datum point",
                    "at": [round(v, 4) for v in at]}
        raise RuntimeError("unknown datum: %s (plane, axis, point)" % kind)

    def sketch_geometry(part, plane_name, base, shape):
        """Curves for one shape, in sketch coordinates (u, v); plus the chain to constrain."""
        to3 = SKETCH_PLANES[plane_name][3]
        kind = (shape.get("kind") or "rect").lower()
        curves = []
        chain = []
        if kind in ("rect", "rectangle"):
            x0 = num(shape.get("x0"), 0.0)
            y0 = num(shape.get("y0"), 0.0)
            if shape.get("width") is not None:
                x1 = x0 + num(shape.get("width"), 40.0)
            else:
                x1 = num(shape.get("x1"), x0 + 40.0)
            if shape.get("height") is not None:
                y1 = y0 + num(shape.get("height"), 20.0)
            else:
                y1 = num(shape.get("y1"), y0 + 20.0)
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            for index in range(4):
                a = corners[index]
                b = corners[(index + 1) % 4]
                line = part.Curves.CreateLine(pnt(*to3(a[0], a[1], base)),
                                              pnt(*to3(b[0], b[1], base)))
                curves.append(line)
                chain.append(line)
        elif kind in ("circle", "round"):
            center = shape.get("center") or [0.0, 0.0]
            radius = num(shape.get("radius"))
            if radius is None:
                radius = num(shape.get("diameter"), 20.0) / 2.0
            normal = SKETCH_PLANES[plane_name][0]
            u_vec, v_vec = plane_frame(normal)
            center3 = to3(float(center[0]), float(center[1]), base)
            curves.append(part.Curves.CreateArc(
                pnt(*center3), vec(*u_vec), vec(*v_vec), float(radius), 0.0, 2.0 * math.pi))
        elif kind == "line":
            a = shape.get("from") or [0.0, 0.0]
            b = shape.get("to") or [10.0, 0.0]
            line = part.Curves.CreateLine(pnt(*to3(float(a[0]), float(a[1]), base)),
                                          pnt(*to3(float(b[0]), float(b[1]), base)))
            curves.append(line)
            chain.append(line)
        elif kind in ("polyline", "polygon"):
            points = shape.get("points") or []
            if len(points) < 2:
                raise RuntimeError("a polyline needs at least 2 points")
            loop = bool(shape.get("closed", True))
            count = len(points) if loop else len(points) - 1
            for index in range(count):
                a = points[index]
                b = points[(index + 1) % len(points)]
                line = part.Curves.CreateLine(pnt(*to3(float(a[0]), float(a[1]), base)),
                                              pnt(*to3(float(b[0]), float(b[1]), base)))
                curves.append(line)
                chain.append(line)
        else:
            raise RuntimeError("unknown sketch shape: %s (rect, circle, line, polyline)" % kind)
        return curves, chain

    def sketch_geom_struct(curve, point_type=None):
        data = NX.SketchConstraintGeometry_Struct()
        data.Geometry = curve
        data.PointType = point_type or NX.SketchConstraintPointType.NotSet
        data.SplineDefiningPointIndex = 0
        return data

    def constrain_chain(sketch, chain, closed=True):
        """Horizontal / vertical on axis-aligned segments, coincident between neighbours."""
        made = 0
        for curve in chain:
            start = curve.StartPoint
            end = curve.EndPoint
            span = (abs(end.X - start.X), abs(end.Y - start.Y), abs(end.Z - start.Z))
            try:
                if span[1] < 1e-6 and span[2] < 1e-6:
                    sketch.CreateHorizontalConstraint(sketch_geom_struct(curve))
                    made += 1
                elif span[0] < 1e-6 and span[2] < 1e-6:
                    sketch.CreateVerticalConstraint(sketch_geom_struct(curve))
                    made += 1
            except Exception:
                pass
        pairs = [(chain[index], chain[index + 1]) for index in range(len(chain) - 1)]
        if closed and len(chain) > 2:
            pairs.append((chain[-1], chain[0]))
        for first, second in pairs:
            try:
                sketch.CreateCoincidentConstraint(
                    sketch_geom_struct(first, NX.SketchConstraintPointType.EndVertex),
                    sketch_geom_struct(second, NX.SketchConstraintPointType.StartVertex))
                made += 1
            except Exception:
                pass
        return made

    def cmd_sketch(args):
        part = display_part()
        plane_name = (args.get("plane") or "xy").lower()
        if plane_name not in SKETCH_PLANES:
            raise RuntimeError("plane must be xy, xz or yz (got %s)" % plane_name)
        base = float(args.get("base") or 0.0)
        normal, _u_dir, _v_dir, to3 = SKETCH_PLANES[plane_name]
        origin = to3(0.0, 0.0, base)

        plane = part.Planes.CreatePlane(pnt(*origin), vec(*normal),
                                       NX.SmartObject.UpdateOption.WithinModeling)
        builder = part.Sketches.CreateSketchInPlaceBuilder2(None)
        builder.PlaneOption = NX.SketchPlaneOption.ExistingPlane
        builder.PlaneOrFace.Value = plane
        builder.Validate()
        builder.Commit()
        sketch = list(part.Sketches)[-1]
        # geometry and constraints only land while the sketch is the active one
        sketch.Activate(NX.SketchViewReorient.FalseValue)

        shapes = args.get("shapes") or []
        if not shapes:
            shapes = [{"kind": "rect", "x0": 0.0, "y0": 0.0, "width": 60.0, "height": 40.0}]
        chains = []
        for shape in shapes:
            curves, chain = sketch_geometry(part, plane_name, base, shape)
            for curve in curves:
                sketch.AddGeometry(curve)
            if chain:
                chains.append((chain, bool(shape.get("closed", True))))
        constraints = 0
        if args.get("constrain", True):
            for chain, closed in chains:
                constraints += constrain_chain(sketch, chain, closed)
        sketch.LocalUpdate()

        result = {
            "sketch": getattr(sketch, "Name", "sketch"),
            "plane": plane_name,
            "base": base,
            "curves": len(list(sketch.GetAllGeometry())),
            "constraints": constraints,
        }
        height = num(args.get("height") or args.get("extrude"), 0.0)
        if height or args.get("through") or (args.get("cut") and args.get("height") is None):
            curves = list(sketch.GetAllGeometry())
            if not curves:
                raise RuntimeError("the sketch has no curves to extrude")
            section = part.Sections.CreateSection(0.0095, 0.01, 0.5)
            rule = part.ScRuleFactory.CreateRuleCurveDumb(curves)
            helper = curves[0].StartPoint
            section.AddToSection([rule], curves[0], NX.NXObject.Null, NX.NXObject.Null,
                                 pnt(helper.X, helper.Y, helper.Z), NX.Section.Mode.Create, False)
            extrude = part.Features.CreateExtrudeBuilder(NX.Features.Extrude.Null)
            extrude.Section = section
            extrude.Direction = part.Directions.CreateDirection(
                pnt(*origin), vec(*normal), NX.SmartObject.UpdateOption.WithinModeling)
            start = float(args.get("extend") or 0.0)
            cutting = bool(args.get("cut"))
            target = None
            through = bool(args.get("through")) or (cutting and not args.get("height"))
            if cutting:
                target = pick_bodies(part, args.get("target"))[0]
            if through:
                # cover the whole body along the normal (a cut or a through extrusion)
                body = target or pick_bodies(part, args.get("target"))[0]
                box = s.Measurement.GetBoundingBoxProperties(
                    [body], 0, NX.Point3d(0.0, 0.0, 0.0), False)
                low, high = box[0][0], box[0][6]
                along = []
                for cx in (low.X, high.X):
                    for cy in (low.Y, high.Y):
                        for cz in (low.Z, high.Z):
                            along.append((cx - origin[0]) * normal[0]
                                         + (cy - origin[1]) * normal[1]
                                         + (cz - origin[2]) * normal[2])
                start = min(along) - 1.0
                reach = max(along) + 1.0
            if bool(args.get("symmetric")):
                half = abs(float(height or 0.0)) / 2.0
                extrude.Limits.StartExtend.Value.SetFormula(expr_text(-half))
                extrude.Limits.EndExtend.Value.SetFormula(expr_text(half))
            elif through:
                extrude.Limits.StartExtend.Value.SetFormula(expr_text(start))
                extrude.Limits.EndExtend.Value.SetFormula(expr_text(reach))
            else:
                extrude.Limits.StartExtend.Value.SetFormula(expr_text(start))
                extrude.Limits.EndExtend.Value.SetFormula(expr_text(start + float(height)))
            if cutting:
                extrude.BooleanOperation.Type = NX.GeometricUtilities.BooleanOperationBooleanType.Subtract
                extrude.BooleanOperation.SetTargetBodies([target])
            elif list(part.Bodies):
                extrude.BooleanOperation.Type = NX.GeometricUtilities.BooleanOperationBooleanType.Unite
            else:
                extrude.BooleanOperation.Type = NX.GeometricUtilities.BooleanOperationBooleanType.Create
            extrude.Validate()
            feature = extrude.CommitFeature()
            result["extrude"] = feature_out(part, feature, {"height": height})
        result["measure"] = measure(part)
        try:
            sketch.Deactivate()
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------
    # assembly
    # ------------------------------------------------------------------
    def ident_matrix():
        matrix = NX.Matrix3x3()
        matrix.Xx, matrix.Xy, matrix.Xz = 1.0, 0.0, 0.0
        matrix.Yx, matrix.Yy, matrix.Yz = 0.0, 1.0, 0.0
        matrix.Zx, matrix.Zy, matrix.Zz = 0.0, 0.0, 1.0
        return matrix

    def euler_matrix(rotate):
        """Matrix3x3 (column order) from XYZ Euler degrees."""
        import math
        rx, ry, rz = [float(v) for v in (rotate or [0, 0, 0])]
        rx, ry, rz = map(math.radians, (rx, ry, rz))
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        # R = Rz * Ry * Rx, stored column by column
        r = [
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ]
        matrix = NX.Matrix3x3()
        matrix.Xx, matrix.Yx, matrix.Zx = r[0]
        matrix.Xy, matrix.Yy, matrix.Zy = r[1]
        matrix.Xz, matrix.Yz, matrix.Zz = r[2]
        return matrix

    def pnt_of(point):
        return [round(point.X, 4), round(point.Y, 4), round(point.Z, 4)]

    def matrix_rows(matrix):
        return [[round(getattr(matrix, name), 4) for name in ("Xx", "Xy", "Xz")],
                [round(getattr(matrix, name), 4) for name in ("Yx", "Yy", "Yz")],
                [round(getattr(matrix, name), 4) for name in ("Zx", "Zy", "Zz")]]

    def component_label(component):
        label = fix_text(getattr(component, "Name", "") or "")
        if label:
            return label
        try:
            return fix_text(getattr(component.Prototype, "Leaf", "") or str(component.Prototype)).upper()
        except Exception:
            return ""

    def dof_pair(component):
        try:
            dof = component.GetDegreesOfFreedom()
            return [int(dof.NumTranslational), int(dof.NumRotational)]
        except Exception:
            return None

    def component_row(component):
        row = {"name": component_label(component)}
        try:
            position, orientation = component.GetPosition()
            row["position"] = pnt_of(position)
            row["orientation"] = matrix_rows(orientation)
        except Exception:
            pass
        row["dof"] = dof_pair(component)
        try:
            row["suppressed"] = bool(component.IsSuppressed)
        except Exception:
            row["suppressed"] = False
        try:
            row["prototype"] = fix_text(component.Prototype.FullPath)
        except Exception:
            pass
        try:
            constraints = []
            for constraint in list(component.GetConstraints()):
                constraints.append({"type": constraint_name(constraint),
                                    "status": str(constraint.GetConstraintStatus())})
            row["constraints"] = constraints
        except Exception:
            pass
        return row

    CONSTRAINT_TYPES = {
        "concentric": "Concentric",
        "touch": "Touch",
        "distance": "Distance",
        "angle": "Angle",
        "parallel": "Parallel",
        "perpendicular": "Perpendicular",
        "align": "AlignLock",
        "alignlock": "AlignLock",
        "center": "Center12",
        "fix": "Fix",
        "bond": "Bond",
        "fit": "Fit",
        "slider": "Slider",
        "cylindrical": "Cylindrical",
    }

    def constraint_member(name):
        return getattr(NX.Positioning.ConstraintType, name)

    def constraint_name(constraint):
        try:
            value = constraint.ConstraintType
        except Exception:
            return "?"
        for name in set(CONSTRAINT_TYPES.values()):
            try:
                if constraint_member(name) == value:
                    return name.lower()
            except Exception:
                continue
        return str(value)

    def assembly_parts():
        """(assembly part, ComponentAssembly, positioner) of the displayed part."""
        part = display_part()
        assembly = part.ComponentAssembly
        return part, assembly, assembly.Positioner

    def component_pool(assembly):
        children = []
        try:
            children = list(assembly.RootComponent.GetChildren())
        except Exception:
            pass
        return children

    def component_bodies_in(part, budget=400):
        """Every body the assembly shows, as OCCURRENCES of the component prototypes.

        Nested sub-assemblies are walked to their leaves and suppressed components are
        skipped, so an exported mesh matches what the assembly actually displays.
        """
        assembly = getattr(part, "ComponentAssembly", None)
        rows = []
        if assembly is None:
            return rows
        fully_loaded.seen = 0
        fully_loaded.budget = budget

        def walk(component, depth=0):
            if depth > 12:
                return
            try:
                if component.IsSuppressed:
                    return
            except Exception:
                pass
            try:
                kids = list(component.GetChildren())
            except Exception:
                kids = []
            if kids:
                for kid in kids:
                    walk(kid, depth + 1)
                return
            try:
                prototypes = list(fully_loaded(component).Bodies)
            except Exception:
                return
            for body in prototypes:
                try:
                    placed = component.FindOccurrence(body)
                except Exception:
                    placed = None
                rows.append(placed if placed is not None else body)

        for child in component_pool(assembly):
            walk(child)
        return rows

    def pick_component(assembly, spec):
        children = component_pool(assembly)
        if not children:
            raise RuntimeError("this part has no components: add one with asm_add first")
        if spec is None:
            return children[-1]
        if isinstance(spec, int):
            if spec < 0 or spec >= len(children):
                raise RuntimeError("component index %d out of range 0..%d" % (spec, len(children) - 1))
            return children[spec]
        text = str(spec).strip().lower()
        if text.isdigit():
            return pick_component(assembly, int(text))
        for component in children:
            if component_label(component).lower() == text:
                return component
        for component in children:
            if text in component_label(component).lower():
                return component
        raise RuntimeError("no component named %r (have: %s)"
                           % (spec, ", ".join(component_label(c) for c in children)))

    def face_props(face):
        return s.Measurement.GetFaceProperties([face], 0.99, NX.Measurement.AlternateFace.Radius, True)

    def pick_part_face(part, spec, tolerance=0.05):
        """Find a face of a part by index / radius / plane height / centroid / area."""
        if isinstance(spec, (list, tuple)):
            spec = {"point": [float(v) for v in spec]}
        spec = spec or {}
        bodies = list(part.Bodies)
        if not bodies:
            raise RuntimeError("part %s has no bodies" % fix_text(part.Leaf))
        body_index = spec.get("body")
        if body_index is not None:
            bodies = [pick_bodies(part, body_index)[0]]
        rows = []
        for body in bodies:
            for index, face in enumerate(list(body.GetFaces())):
                try:
                    info = face_props(face)
                except Exception:
                    continue
                rows.append({
                    "face": face,
                    "index": index,
                    "area": float(info[0]),
                    "radius": float(info[2]),
                    "cog": info[3],
                })
        if not rows:
            raise RuntimeError("no face could be measured in %s" % fix_text(part.Leaf))
        if "index" in spec:
            index = int(spec["index"])
            if index < 0 or index >= len(rows):
                raise RuntimeError("face index %d out of range 0..%d" % (index, len(rows) - 1))
            return rows[index]
        if "radius" in spec or "diameter" in spec:
            wanted = float(spec["radius"]) if "radius" in spec else float(spec["diameter"]) / 2.0
            wanted = abs(wanted)
            best = None
            for row in rows:
                if row["radius"] <= 0:
                    continue
                delta = abs(row["radius"] - wanted)
                if delta <= tolerance and (best is None or delta < best[0]):
                    best = (delta, row)
            if best is None:
                raise RuntimeError("no cylindrical face with radius %g (+-%g)" % (wanted, tolerance))
            return best[1]
        if "z" in spec:
            wanted = float(spec["z"])
            best = None
            for row in rows:
                if row["radius"] > 1e-6:
                    continue
                delta = abs(row["cog"].Z - wanted)
                if delta <= tolerance and (best is None or delta < best[0]):
                    best = (delta, row)
            if best is None:
                raise RuntimeError("no planar face at z=%g (+-%g)" % (wanted, tolerance))
            return best[1]
        if "point" in spec:
            point = [float(v) for v in spec["point"]]
            best = None
            for row in rows:
                distance = sum(([row["cog"].X, row["cog"].Y, row["cog"].Z][i] - point[i]) ** 2 for i in range(3))
                if best is None or distance < best[0]:
                    best = (distance, row)
            return best[1]
        if "area" in spec:
            wanted = float(spec["area"])
            best = None
            for row in rows:
                delta = abs(row["area"] - wanted)
                if best is None or delta < best[0]:
                    best = (delta, row)
            return best[1]
        raise RuntimeError("face spec needs one of index/radius/diameter/z/point/area")

    def side_reference(spec, default_axis):
        """Resolve {component, face, usesAxis} into (component, occurrence face)."""
        if not isinstance(spec, dict):
            raise RuntimeError("each mate side must be an object: {component, face}")
        part, assembly, _ = assembly_parts()
        component = pick_component(assembly, spec.get("component"))
        geometry = pick_part_face(component.Prototype, spec.get("face") or {})
        occurrence = component.FindOccurrence(geometry["face"])
        if occurrence is None:
            raise RuntimeError("could not map the face of %s into the assembly" % component_label(component))
        return component, occurrence

    def uses_axis_for(kind, spec):
        if isinstance(spec, dict) and "usesAxis" in spec:
            return bool(spec["usesAxis"])
        return kind in ("concentric", "parallel", "perpendicular", "distance", "angle", "aliglock", "align")

    def cmd_asm_add(args):
        part, assembly, _ = assembly_parts()
        path = args.get("path")
        if not path:
            raise RuntimeError("path of the part to add is required")
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise RuntimeError("file not found: %s" % path)
        base = args.get("at") or [0, 0, 0]
        orientation = euler_matrix(args.get("rotate"))
        reference_set = str(args.get("referenceSet") or "MODEL")
        layer = int(args.get("layer", -1))
        component_name = str(args.get("name") or "").strip()
        if not component_name:
            component_name = os.path.splitext(os.path.basename(path))[0].upper()
        result = assembly.AddComponent(path, reference_set, component_name,
                                       pnt(base[0], base[1], base[2]), orientation, layer)
        component = result[0] if isinstance(result, tuple) else result
        if args.get("name"):
            try:
                component.SetName(str(args["name"]))
            except Exception:
                pass
        try:
            s.UpdateManager.DoUpdate(1)
        except Exception:
            pass
        row = component_row(component)
        row["components"] = [component_row(c) for c in component_pool(assembly)]
        return row

    def cmd_asm_move(args):
        part, assembly, _ = assembly_parts()
        component = pick_component(assembly, args.get("component"))
        before = component_row(component)
        translation = args.get("translate") or [0, 0, 0]
        rotation = args.get("rotate")
        assembly.MoveComponent(component, vec(translation[0], translation[1], translation[2]),
                               euler_matrix(rotation) if rotation else ident_matrix())
        try:
            s.UpdateManager.DoUpdate(1)
        except Exception:
            pass
        row = component_row(component)
        return {"component": row["name"], "before": before.get("position"), "after": row.get("position"),
                "dof": row.get("dof")}

    def cmd_asm_fix(args):
        part, assembly, positioner = assembly_parts()
        component = pick_component(assembly, args.get("component"))
        before = dof_pair(component)
        constraint = positioner.CreateConstraint(True)
        constraint.ConstraintType = constraint_member("Fix")
        constraint.CreateConstraintReference(component, component, False, False)
        try:
            s.UpdateManager.DoUpdate(1)
        except Exception:
            pass
        return {"component": component_label(component), "constraint": constraint_name(constraint),
                "status": str(constraint.GetConstraintStatus()), "dofBefore": before,
                "dofAfter": dof_pair(component)}

    def cmd_asm_mate(args):
        part, assembly, positioner = assembly_parts()
        kind = str(args.get("type") or "concentric").strip().lower()
        if kind not in CONSTRAINT_TYPES:
            raise RuntimeError("unknown mate type %r (use %s)" % (kind, ", ".join(sorted(CONSTRAINT_TYPES))))
        first = args.get("a")
        second = args.get("b")
        if not isinstance(first, dict) or not isinstance(second, dict):
            raise RuntimeError("mate needs a and b, each {component, face}")
        component_a, geometry_a = side_reference(first, True)
        component_b, geometry_b = side_reference(second, True)
        dof_before = dof_pair(component_a)
        constraint = positioner.CreateConstraint(True)
        constraint.ConstraintType = constraint_member(CONSTRAINT_TYPES[kind])
        constraint.CreateConstraintReference(component_a, geometry_a, uses_axis_for(kind, first), False)
        constraint.CreateConstraintReference(component_b, geometry_b, uses_axis_for(kind, second), False)
        if args.get("value") is not None:
            try:
                constraint.SetExpression(expr_text(args["value"]))
            except Exception:
                pass
        solved = False
        solver_error = None
        position_before = component_row(component_a).get("position")
        try:
            network = positioner.EstablishNetwork()
            network.AddConstraint(constraint)
            network.Solve()
            s.UpdateManager.DoAssemblyConstraintsUpdate(1)
            solved = component_row(component_a).get("position") != position_before
        except Exception as error:
            solver_error = "%s: %s" % (type(error).__name__, error)
        row = component_row(component_a)
        return {
            "mate": kind,
            "a": component_label(component_a),
            "b": component_label(component_b),
            "status": str(constraint.GetConstraintStatus()),
            "value": args.get("value"),
            "solved": solved,
            "solverError": solver_error,
            "positionBefore": position_before,
            "positionAfter": row.get("position"),
            "dofBefore": dof_before,
            "dofAfter": row.get("dof"),
        }

    def cmd_asm_list(args):
        part, assembly, _ = assembly_parts()
        children = component_pool(assembly)
        rows = []
        for component in children:
            rows.append(component_row(component))
        constraints = []
        try:
            for constraint in list(assembly.Positioner.Constraints):
                constraints.append({"type": constraint_name(constraint),
                                    "status": str(constraint.GetConstraintStatus())})
        except Exception:
            pass
        result = {"assembly": fix_text(part.Leaf), "path": fix_text(part.FullPath),
                  "components": rows, "constraints": constraints,
                  "componentCount": len(children)}
        csv_path = args.get("csv")
        if csv_path:
            import csv
            target = csv_path if os.path.isabs(csv_path) else os.path.join(OUT_DIR, csv_path)
            folder = os.path.dirname(target)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(target, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["name", "part file", "suppressed", "dof_free", "dof_fixed",
                                 "x", "y", "z"])
                for row in rows:
                    position = row.get("position") or [0.0, 0.0, 0.0]
                    dof = row.get("dof") or [0, 0]
                    writer.writerow([row.get("name") or "", row.get("prototype") or "",
                                     "yes" if row.get("suppressed") else "no",
                                     dof[0], dof[1],
                                     round(position[0], 3), round(position[1], 3),
                                     round(position[2], 3)])
            result["csv"] = target
            result["csvBytes"] = os.path.getsize(target)
        return result

    EXPLODE_BACKUP = "DSHNX_EXPLODE_ORIGINALS"

    def leaf_components(assembly):
        """Components with no children of their own -- the ones an explosion moves."""
        rows = []
        for component in component_pool(assembly):
            try:
                if list(component.GetChildren()):
                    continue
            except Exception:
                pass
            rows.append(component)
        return rows

    def component_offset(component):
        return pnt_of(component.GetPosition()[0])

    def move_component_to(assembly, component, target):
        """Translate a component so its origin lands on target [x, y, z].

        The position NX reports straight after MoveComponent is still the old one (the move lands
        on the next update), so "after" is the requested target rather than a re-read value -- a
        re-read would make every move look like a no-op and the explode count come out as zero.
        """
        current = component_offset(component)
        wanted = [round(float(target[i]), 4) for i in range(3)]
        delta = [wanted[i] - current[i] for i in range(3)]
        if abs(delta[0]) < 1e-9 and abs(delta[1]) < 1e-9 and abs(delta[2]) < 1e-9:
            return {"name": component_label(component), "before": current, "after": current,
                    "moved": False}
        assembly.MoveComponent(component, vec(delta[0], delta[1], delta[2]), ident_matrix())
        return {"name": component_label(component), "before": current, "after": wanted,
                "moved": True}

    def explode_backup(part, value=None):
        """Read or write the saved start positions of an explosion."""
        if value is None:
            try:
                raw = part.GetStringUserAttribute(EXPLODE_BACKUP, 0)
            except Exception:
                return None
            if not raw:
                return None
            try:
                return json.loads(raw)
            except Exception:
                return None
        try:
            part.SetUserAttribute(EXPLODE_BACKUP, 0, json.dumps(value), NX.Update.Option.Now)
        except Exception:
            try:
                part.SetUserAttribute(EXPLODE_BACKUP, 0, json.dumps(value), NX.Update.Option.Later)
            except Exception:
                pass
        return value

    def cmd_component_pattern(args):
        """Pattern a component: linear along X/Y with pitches, or circular about an axis."""
        part, assembly, _ = assembly_parts()
        seed = pick_component(assembly, args.get("component"))
        mode = str(args.get("mode") or "linear").lower()
        builder = assembly.CreateComponentPatternBuilder(None)
        try:
            builder.ComponentPatternSet.Clear()
            builder.ComponentPatternSet.Add(seed)
            service = builder.PatternService
            if mode == "circular":
                service.PatternType = NX.GeometricUtilities.PatternDefinitionPatternEnum.Circular
                circular = service.CircularDefinition
                count = int(args.get("count", 6))
                sweep = float(args.get("angle", 360.0))
                circular.AngularSpacing.NCopies.SetFormula(str(count))
                circular.AngularSpacing.SpanAngle.SetFormula("%.6f" % sweep)
                axis = [float(v) for v in (args.get("axis") or [0.0, 0.0, 1.0])]
                origin = [float(v) for v in (args.get("at") or [0.0, 0.0, 0.0])]
                circular.RotationAxis = part.Directions.CreateDirection(
                    pnt(origin[0], origin[1], origin[2]),
                    vec(axis[0], axis[1], axis[2]),
                    NX.SmartObject.UpdateOption.WithinModeling)
                circular.RotationCenter = pnt(origin[0], origin[1], origin[2])
                detail = {"mode": "circular", "count": count, "angle": sweep, "axis": axis}
            else:
                service.PatternType = NX.GeometricUtilities.PatternDefinitionPatternEnum.Linear
                rectangular = service.RectangularDefinition
                count_x = int(args.get("countX", 1) or 1)
                count_y = int(args.get("countY", 1) or 1)
                pitch_x = float(args.get("pitchX", 0.0) or 0.0)
                pitch_y = float(args.get("pitchY", 0.0) or 0.0)
                if count_x < 2 and count_y < 2:
                    raise RuntimeError("a linear pattern needs countX >= 2 or countY >= 2")
                rectangular.XSpacing.NCopies.SetFormula(str(count_x))
                rectangular.XSpacing.PitchDistance.SetFormula("%.6f" % pitch_x)
                rectangular.UseYDirectionToggle = count_y >= 2
                if count_y >= 2:
                    rectangular.YSpacing.NCopies.SetFormula(str(count_y))
                    rectangular.YSpacing.PitchDistance.SetFormula("%.6f" % pitch_y)
                rectangular.XDirection = part.Directions.CreateDirection(
                    pnt(0.0, 0.0, 0.0), vec(1.0, 0.0, 0.0),
                    NX.SmartObject.UpdateOption.WithinModeling)
                if count_y >= 2:
                    rectangular.YDirection = part.Directions.CreateDirection(
                        pnt(0.0, 0.0, 0.0), vec(0.0, 1.0, 0.0),
                        NX.SmartObject.UpdateOption.WithinModeling)
                detail = {"mode": "linear", "countX": count_x, "pitchX": pitch_x,
                          "countY": count_y, "pitchY": pitch_y}
            builder.Validate()
            feature = builder.Commit()
            try:
                s.UpdateManager.DoUpdate(1)
            except Exception:
                pass
            detail["feature"] = getattr(feature, "Name", "") or "ComponentPattern"
            detail["components"] = [component_row(c) for c in component_pool(assembly)]
            detail["componentCount"] = len(detail["components"])
            return detail
        finally:
            try:
                builder.Destroy()
            except Exception:
                pass

    def cmd_component_op(args):
        """Suppress, unsuppress, remove or replace (substitute) a component."""
        part, assembly, _ = assembly_parts()
        action = str(args.get("action") or "").lower()
        component = pick_component(assembly, args.get("component"))
        before = component_row(component)

        if action in ("suppress", "unsuppress"):
            pool = [component]
            wanted = str(component_label(component)).lower()
            if args.get("all"):
                same = [c for c in leaf_components(assembly)
                        if str(component_label(c)).lower() == wanted]
                if action == "suppress":
                    same = [c for c in same if not c.IsSuppressed]
                else:
                    same = [c for c in same if c.IsSuppressed]
                if same:
                    pool = same
            if action == "suppress":
                assembly.SuppressComponents(pool)
            else:
                assembly.UnsuppressComponents(pool)
            try:
                s.UpdateManager.DoUpdate(1)
            except Exception:
                pass
            active = 0
            suppressed = 0
            for item in leaf_components(assembly):
                if item.IsSuppressed:
                    suppressed += 1
                else:
                    active += 1
            return {"action": action, "target": component_label(component),
                    "count": len(pool), "suppressed": suppressed, "active": active,
                    "before": before}

        if action == "remove":
            name = component_label(component)
            assembly.RemoveComponent(component)
            try:
                s.UpdateManager.DoUpdate(1)
            except Exception:
                pass
            return {"action": "remove", "removed": name,
                    "componentCount": len(component_pool(assembly))}

        if action in ("replace", "substitute"):
            path = args.get("path")
            if not path:
                raise RuntimeError("replace needs the path of the part to substitute")
            path = os.path.abspath(path)
            if not os.path.exists(path):
                raise RuntimeError("file not found: %s" % path)
            new_part = None
            for candidate in s.Parts:
                try:
                    if os.path.normcase(os.path.abspath(candidate.FullPath or "")) == os.path.normcase(path):
                        new_part = candidate
                        break
                except Exception:
                    continue
            if new_part is None:
                new_part = s.Parts.OpenBaseDisplay(path)
            # opening the part for substitution can push it to the front; the assembly has
            # to stay the displayed part or every later command would target the substitute
            try:
                s.Parts.SetDisplay(part, True, True)
            except Exception:
                pass
            state["part"] = part
            name = str(args.get("name") or os.path.splitext(os.path.basename(path))[0])
            new_component = assembly.SubstituteComponent(
                component, new_part, name,
                str(args.get("referenceSet") or "MODEL"),
                int(args.get("layer", -1)),
                NX.Assemblies.ComponentAssemblySubstitutionMode.SingleComponent)
            try:
                s.UpdateManager.DoUpdate(1)
            except Exception:
                pass
            after = component_row(new_component)
            return {"action": "replace", "before": before, "after": after,
                    "part": fix_text(new_part.Leaf),
                    "componentCount": len(component_pool(assembly))}

        raise RuntimeError("action must be suppress / unsuppress / remove / replace")

    def cmd_explode(args):
        """Spread the leaf components apart along a direction; restore puts them back."""
        part, assembly, _ = assembly_parts()
        leaves = leaf_components(assembly)
        if not leaves:
            raise RuntimeError("this part has no components -- add some with nx_assembly_add first")

        if args.get("restore"):
            backup = explode_backup(part)
            if not backup:
                raise RuntimeError("nothing to restore: no exploded positions were recorded on this part")
            moved = []
            for index, component in enumerate(leaves):
                target = backup.get("%s#%d" % (component_label(component), index))
                if not target:
                    continue
                row = move_component_to(assembly, component, target)
                if row.get("moved", row["before"] != row["after"]):
                    moved.append(row)
            try:
                s.UpdateManager.DoUpdate(1)
            except Exception:
                pass
            explode_backup(part, {})
            return {"restored": len(moved), "moves": moved,
                    "components": [component_row(c) for c in leaves]}

        direction = [float(v) for v in (args.get("direction") or [0.0, 0.0, 1.0])]
        length = sum(v * v for v in direction) ** 0.5
        if length < 1e-9:
            raise RuntimeError("direction must be a non-zero vector")
        direction = [v / length for v in direction]
        distance = float(args.get("distance", 40.0))
        stagger = float(args.get("stagger", 0.0))
        keep = args.get("keep")

        keep_component = None
        if keep:
            keep_component = pick_component(assembly, keep)

        backup = {}
        for index, component in enumerate(leaves):
            # the name alone is not an identity -- four pattern instances can all be P1_PIN
            backup["%s#%d" % (component_label(component), index)] = component_offset(component)
        explode_backup(part, backup)

        moved = []
        index = 0
        for order, component in enumerate(leaves):
            if keep_component is not None and component == keep_component:
                continue
            offset = distance + stagger * index
            index += 1
            start = component_offset(component)
            target = [start[0] + direction[0] * offset,
                      start[1] + direction[1] * offset,
                      start[2] + direction[2] * offset]
            row = move_component_to(assembly, component, target)
            row["offset"] = round(offset, 3)
            moved.append(row)
        try:
            s.UpdateManager.DoUpdate(1)
        except Exception:
            pass
        return {"exploded": len(moved), "direction": direction, "distance": distance,
                "stagger": stagger, "kept": component_label(keep_component) if keep_component else None,
                "moves": moved, "components": [component_row(c) for c in leaves]}

    def cmd_asm_gap(args):
        """Minimum distance between two components (their bodies), for fit verification."""
        part, assembly, _ = assembly_parts()
        first = pick_component(assembly, args.get("a"))
        second = pick_component(assembly, args.get("b"))
        rows = []
        for component, other in ((first, second), (second, first)):
            for body in list(fully_loaded(component).Bodies):
                try:
                    occurrence = component.FindOccurrence(body)
                    targets = []
                    for other_body in list(fully_loaded(other).Bodies):
                        target = other.FindOccurrence(other_body)
                        if target is not None:
                            targets.append(target)
                    if occurrence is None or not targets:
                        continue
                    try:
                        distance = s.Measurement.GetMinimumDistance([occurrence], targets)
                    except Exception:
                        distance = s.Measurement.GetMinimumDistance(occurrence, targets[0])
                    if isinstance(distance, tuple):
                        distance = distance[0]
                    rows.append({"a": component_label(component), "b": component_label(other),
                                 "distance": round(float(distance), 5)})
                except Exception as error:
                    rows.append({"a": component_label(component), "b": component_label(other),
                                 "error": "%s: %s" % (type(error).__name__, error)})
        distances = [row["distance"] for row in rows if "distance" in row]
        return {"pairs": rows, "minimum": min(distances) if distances else None}

    def component_pair_distance(first, second):
        """Smallest distance between two components, None when no geometry is reachable."""
        try:
            own_bodies = list(fully_loaded(first).Bodies)
            other_bodies = list(fully_loaded(second).Bodies)
        except Exception:
            return None
        targets = []
        for body in other_bodies:
            target = second.FindOccurrence(body)
            if target is not None:
                targets.append(target)
        if not targets:
            return None
        best = None
        for body in own_bodies:
            occurrence = first.FindOccurrence(body)
            if occurrence is None:
                continue
            try:
                distance = s.Measurement.GetMinimumDistance([occurrence], targets)
            except Exception:
                try:
                    distance = s.Measurement.GetMinimumDistance(occurrence, targets[0])
                except Exception:
                    continue
            if isinstance(distance, tuple):
                distance = distance[0]
            value = float(distance)
            if best is None or value < best:
                best = value
        return best

    def cmd_assembly_clearance(args):
        """Gap matrix over component pairs: NX has no interference API, so distances are measured."""
        part, assembly, _ = assembly_parts()
        children = component_pool(assembly)
        if len(children) < 2:
            raise RuntimeError("an assembly needs at least two components to compare")
        picked = children
        wanted = args.get("components")
        if wanted:
            specs = wanted if isinstance(wanted, (list, tuple)) else [wanted]
            picked = [pick_component(assembly, spec) for spec in specs]
            if len(picked) < 2:
                raise RuntimeError("clearance needs at least two components to compare")
        tolerance = float(args.get("tolerance") or 0.0)
        threshold = args.get("threshold")
        limit = None if threshold in (None, "") else float(threshold)
        pairs = []
        for index, first in enumerate(picked):
            for second in picked[index + 1:]:
                row = {"a": component_label(first), "b": component_label(second)}
                value = component_pair_distance(first, second)
                if value is None:
                    row["error"] = "no measurable geometry"
                else:
                    if limit is not None and value > limit:
                        continue
                    row["distance"] = round(value, 5)
                pairs.append(row)
        ordered = sorted(pairs, key=lambda row: (row.get("distance") is None, row.get("distance", 0.0)))
        distances = [row["distance"] for row in ordered if "distance" in row]
        return {
            "assembly": fix_text(part.Leaf),
            "componentCount": len(children),
            "checked": len(picked),
            "tolerance": tolerance,
            "threshold": limit,
            "pairs": ordered,
            "minimum": min(distances) if distances else None,
            "interference": [row for row in ordered if row.get("distance", 1.0e9) < tolerance],
        }

    def cmd_assembly_bom(args):
        """Bill of materials: quantities, per-part volume and mass, optional CSV.

        Suppressed components are left out unless includeSuppressed is set, the way
        an NX parts list treats them; the count of skipped ones comes back as
        totals.suppressed.
        """
        part, assembly, _ = assembly_parts()
        rollup = bom_rollup(assembly, bool(args.get("fast", False)),
                            bool(args.get("includeSuppressed", False)))
        suppressed = rollup["suppressed"]
        rows = rollup["components"]
        groups = rollup["groups"]
        totals = {
            "components": len(rows),
            "distinct": len(groups),
            "suppressed": suppressed,
            "volume": round(sum(float(row.get("volume") or 0.0) for row in groups), 4),
            "mass": round(sum(float(row.get("mass") or 0.0) for row in groups), 4),
        }
        result = {"assembly": fix_text(part.Leaf), "path": fix_text(part.FullPath),
                  "groups": groups, "components": rows, "totals": totals}
        csv_path = args.get("csv")
        if csv_path:
            import csv
            target = csv_path if os.path.isabs(csv_path) else os.path.join(OUT_DIR, csv_path)
            folder = os.path.dirname(target)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(target, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["name", "part file", "quantity", "bodies",
                                 "volume_mm3", "mass_kg"])
                for row in groups:
                    writer.writerow([row["name"], row["file"] or "", row["quantity"],
                                     row["bodies"], row["volume"], row["mass"]])
            result["csv"] = target
            result["csvBytes"] = os.path.getsize(target)
        return result


    # ---------------------------------------------------------------- bom table & batch drafting
    def bom_rollup(assembly, fast=False, include_suppressed=False):
        """Per-part rollup shared by nx_assembly_bom and the drawing table."""
        children = component_pool(assembly)
        fully_loaded.budget = max(fully_loaded.budget, len(children) + 8)
        suppressed = 0
        rows = []
        buckets = {}
        for component in children:
            try:
                if component.IsSuppressed:
                    suppressed += 1
                    if not include_suppressed:
                        continue
            except Exception:
                pass
            prototype = getattr(component, "Prototype", None)
            path = ""
            count = 0
            if prototype is not None:
                try:
                    path = fix_text(prototype.FullPath) or ""
                except Exception:
                    path = ""
                try:
                    count = len(list(prototype.Bodies))
                except Exception:
                    count = 0
            row = {"name": component_label(component), "file": path or None, "bodies": count}
            if not fast and prototype is not None:
                try:
                    # A component of a big assembly is only partially loaded, so its
                    # prototype reports no bodies and therefore no mass; pull it in
                    # first or the BOM lists the workpiece as 0.0000 kg.
                    stats = measure(fully_loaded(component) or prototype)
                except Exception:
                    stats = {}
                for field in ("volume", "mass", "area"):
                    if field in stats:
                        row[field] = round(float(stats[field]), 4)
                if (float(row.get("mass") or 0.0) <= 0.0
                        and float(row.get("volume") or 0.0) > 0.0):
                    row["mass"] = round(float(row["volume"]) * STEEL_DENSITY, 4)
                    row["massEstimated"] = True
            rows.append(row)
            key = os.path.normcase(path) or ("name:" + str(row["name"]))
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {"name": row["name"], "file": path or None, "quantity": 0,
                          "bodies": count, "volume": 0.0, "mass": 0.0}
                buckets[key] = bucket
            bucket["quantity"] += 1
            bucket["volume"] = round(bucket["volume"] + float(row.get("volume") or 0.0), 4)
            bucket["mass"] = round(bucket["mass"] + float(row.get("mass") or 0.0), 4)
            if row.get("massEstimated"):
                bucket["massEstimated"] = True
        groups = sorted(buckets.values(), key=lambda row: (-row["quantity"], str(row["name"])))
        return {"groups": groups, "components": rows, "suppressed": suppressed}

    # Column catalogue: id -> (中文表头, English header, width mm). The panel lets the user tick
    # which of these go on the sheet and switch the header language, so the table is not fixed to
    # one layout any more.
    BOM_FIELDS = {
        "no": ("序号", "No.", 12.0),
        "part": ("零件号", "Part No.", 58.0),
        "name": ("名称", "Name", 38.0),
        "qty": ("数量", "Qty", 14.0),
        "unit": ("单重 kg", "Unit kg", 24.0),
        "total": ("总重 kg", "Total kg", 24.0),
        "volume": ("体积 mm3", "Vol mm3", 28.0),
    }
    BOM_ORDER = ("no", "part", "name", "qty", "unit", "total")
    BOM_SUMS = ("qty", "unit", "total", "volume")

    def bom_columns(spec, language):
        """Resolve the requested columns to (id, label, width) triples."""
        english = str(language or "zh").lower().startswith("en")
        if spec is None:
            ids = list(BOM_ORDER)
        elif isinstance(spec, str):
            ids = [item.strip() for item in spec.split(",") if item.strip()]
        else:
            ids = [str(item).strip() for item in spec if str(item).strip()]
        if not ids:
            ids = list(BOM_ORDER)
        unknown = [item for item in ids if item not in BOM_FIELDS]
        if unknown:
            raise RuntimeError("unknown BOM column(s) %s — available: %s"
                               % (", ".join(unknown), ", ".join(BOM_FIELDS)))
        columns = []
        for key in ids:
            zh, en, width = BOM_FIELDS[key]
            columns.append((key, en if english else zh, width))
        return columns

    # Parts whose .prt carries no material come back with mass 0 and a real volume;
    # a BOM that silently reports 0.0000 kg for the workpiece is worse than an
    # estimate, so those rows fall back to steel and get marked in the table.
    STEEL_DENSITY = 7.85e-6

    def text_width_mm(text, size):
        """Rough advance width: CJK glyphs are square, capitals and digits wider.

        A flat 0.58 em for latin under-estimates upper case, and a long part number
        then runs across the next cell line (which is exactly what the fixture BOM
        did), so the classes are counted separately.
        """
        width = 0.0
        for char in str(text):
            if ord(char) > 0x2E80:
                width += size * 1.15
            elif char.isupper() or char.isdigit():
                width += size * 0.78
            elif char in " .,:;'`|!":
                width += size * 0.45
            else:
                width += size * 0.66
        return width

    def fit_text(value, limit, size):
        text = str(value)
        if text_width_mm(text, size) <= limit:
            return text
        trimmed = text
        while trimmed and text_width_mm(trimmed + "..", size) > limit:
            trimmed = trimmed[:-1]
        return trimmed + ".." if trimmed != text else text

    def sheet_line(part, x1, y1, x2, y2):
        return part.Curves.CreateLine(pnt(x1, y1, 0.0), pnt(x2, y2, 0.0))

    def cell_text(part, text, x, y, size, font_index):
        builder = part.Annotations.CreateDraftingNoteBuilder(None)
        builder.Style.LetteringStyle.GeneralTextFont = int(font_index)
        try:
            builder.Style.LetteringStyle.GeneralTextSize = size
        except Exception:
            pass
        builder.Text.SetEditorText([text])
        builder.Origin.OriginPoint = pnt(x, y, 0.0)
        builder.Validate()
        return builder.Commit()

    def cjk_font(part):
        try:
            return int(part.Fonts.AddFont("chinesef", NX.FontCollectionType.Nx))
        except Exception:
            return None

    def cmd_bom_table(args):
        """Draw the bill of materials as a ruled table on the current drawing sheet."""
        part, assembly, _ = assembly_parts()
        # The table goes on a sheet of the requested size; if the part only has A3 sheets and A2
        # was asked for, build the A2 instead of squeezing 19 rows onto the wrong paper.
        sheet_size = str(args.get("size") or "A3").upper()
        paper = SHEET_SIZES.get(sheet_size)

        def on_size(candidate):
            if paper is None:
                return True
            try:
                return (abs(float(candidate.Length) - paper[0]) < 1.0
                        and abs(float(candidate.Height) - paper[1]) < 1.0)
            except Exception:
                return True

        sheets = [sh for sh in list(part.DraftingDrawingSheets) if on_size(sh)]
        if not sheets:
            if args.get("drawing") is False:
                raise RuntimeError("this part has no %s drawing sheet — run nx_drawing first" % sheet_size)
            cmd_drawing({"size": sheet_size, "projection": "first",
                         "views": {"top": True, "front": True, "isometric": True}})
            sheets = [sh for sh in list(part.DraftingDrawingSheets) if on_size(sh)]
        if not sheets:
            sheets = list(part.DraftingDrawingSheets)
        sheet = None
        try:
            current = part.DraftingDrawingSheets.CurrentDrawingSheet
        except Exception:
            current = None
        if current is not None and on_size(current):
            sheet = current
        for candidate in reversed(sheets):
            if sheet is None:
                sheet = candidate
        if sheet is None:
            raise RuntimeError("this part has no drawing sheet — run nx_drawing first")
        rollup = bom_rollup(assembly, bool(args.get("fast", False)),
                            bool(args.get("includeSuppressed", False)))
        groups = rollup["groups"]
        if not groups:
            raise RuntimeError("the assembly has no components to list")

        text_size = float(args.get("textSize") or 3.0)
        row_height = float(args.get("rowHeight") or 7.0)
        header_height = row_height + 1.5
        total_height = row_height + 1.0
        at = args.get("at") or [20.0, 25.0]
        x0, y0 = float(at[0]), float(at[1])
        columns = bom_columns(args.get("columns"), args.get("language"))
        table_width = sum(width for _, _, width in columns)
        data_rows = len(groups)
        header_bottom = y0 + total_height + data_rows * row_height
        table_top = header_bottom + header_height

        # grid: outer frame, one line per row boundary, one per column boundary
        tops = [y0, y0 + total_height] + [y0 + total_height + index * row_height
                                          for index in range(1, data_rows + 1)]
        tops += [table_top]
        pieces = []
        for value in tops:
            pieces.append(sheet_line(part, x0, value, x0 + table_width, value))
        edges = [x0]
        cursor = x0
        for _, _, width in columns:
            cursor += width
            edges.append(cursor)
        # The 合计 row spans the leading identity columns; when none of them is a numeric
        # summary column the row stays a normal row and every vertical runs the full height.
        merge_count = len(columns)
        for index, (key, _, _) in enumerate(columns):
            if key in BOM_SUMS:
                merge_count = index
                break
        for index, value in enumerate(edges):
            full = merge_count == 0 or index in (0, len(edges) - 1)
            low = y0 if full else y0 + total_height
            pieces.append(sheet_line(part, value, low, value, table_top))

        font = cjk_font(part)
        cells = 0

        def put(text, cell_x, cell_width, bottom, height):
            nonlocal cells
            if font is None:
                return
            try:
                cell_text(part, fit_text(text, cell_width - min(6.0, max(2.5, cell_width * 0.09)),
                                         text_size),
                          cell_x + 1.0, bottom + (height + text_size) * 0.5,
                          text_size, font)
                cells += 1
                return
            except Exception:
                # One cell refusing to draw must not lose the whole table.
                return
            # The margin shrinks with the column so a narrow one (序号, 12 mm) does
            # not eat its own header, while a wide one still keeps clear of the line.
            margin = min(6.0, max(2.5, cell_width * 0.09))
            label = fit_text(text, cell_width - margin, text_size)
            # A drafting note hangs BELOW its origin point, so centring a line in a
            # row means origin = bottom + (height + text size) / 2.
            try:
                cell_text(part, label, cell_x + 1.0, bottom + (height + text_size) * 0.5,
                          text_size, font)
                cells += 1
            except Exception:
                pass

        # header
        cursor = x0
        for (_, label, width) in columns:
            put(label, cursor, width, header_bottom, header_height)
            cursor += width

        # data rows, bottom-up so row 1 sits under the header
        total_mass = 0.0
        total_quantity = 0
        total_volume = 0.0
        for index, row in enumerate(reversed(groups)):
            bottom = y0 + total_height + index * row_height
            number = data_rows - index
            leaf = os.path.splitext(os.path.basename(str(row.get("file") or "")))[0] or "-"
            quantity = int(row.get("quantity") or 0)
            mass = float(row.get("mass") or 0.0)
            total_mass += mass
            total_quantity += quantity
            volume = float(row.get("volume") or 0.0)
            total_volume += volume
            unit = mass / quantity if quantity else 0.0
            label = str(row.get("name") or "")
            if label.upper() == leaf.upper():
                label = ""          # the component name would only repeat 零件号
            mark = "*" if row.get("massEstimated") else ""
            cells_by_id = {"no": str(number), "part": leaf, "name": label,
                           "qty": str(quantity), "unit": "%.4f%s" % (unit, mark),
                           "total": "%.4f%s" % (mass, mark),
                           "volume": "%.1f" % volume}
            cursor = x0
            for (key, _, width) in columns:
                put(cells_by_id.get(key, ""), cursor, width, bottom, row_height)
                cursor += width

        # total row: label in the merged leading cell, sums under their own columns
        merge_label = args.get("totalLabel")
        if merge_label is None:
            merge_label = ("合计 / TOTAL" if not str(args.get("language") or "zh").lower().startswith("en")
                           else "TOTAL")
        merge_width = sum(width for _, _, width in columns[:merge_count]) if merge_count else 0.0
        if merge_count:
            put(str(merge_label), x0, merge_width, y0, total_height)
        sums_by_id = {"qty": str(total_quantity), "total": "%.4f" % total_mass,
                      "volume": "%.1f" % total_volume}
        cursor = x0 + merge_width
        for (key, _, width) in columns[merge_count:]:
            put(sums_by_id.get(key, ""), cursor, width, y0, total_height)
            cursor += width

        caption = args.get("caption")
        if caption is None:
            caption = "明细表  %s   %d 项 / %d 件   总重 %.4f kg" % (
                fix_text(part.Leaf), data_rows, total_quantity, total_mass)
            if any(row.get("massEstimated") for row in groups):
                caption += "   *无材质，按钢 7.85 估算"
        if font is not None:
            # The note hangs below its origin, so lift it clear of the top border.
            cell_text(part, str(caption), x0, table_top + text_size + 4.0, text_size + 0.5, font)
        csv_path = args.get("csv")
        csv_info = {}
        if csv_path:
            # One button draws the table and drops the same numbers as a CSV: they come from
            # the same rollup, so the sheet and the spreadsheet cannot disagree.
            import csv as csv_module
            target = csv_path if os.path.isabs(csv_path) else os.path.join(OUT_DIR, csv_path)
            folder = os.path.dirname(target)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(target, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv_module.writer(handle)
                writer.writerow(["no", "part file", "name", "quantity", "unit_mass_kg",
                                 "mass_kg", "volume_mm3"])
                for index, row in enumerate(groups, start=1):
                    quantity = int(row.get("quantity") or 0)
                    mass = float(row.get("mass") or 0.0)
                    writer.writerow([index, row.get("file") or "", row.get("name") or "",
                                     quantity, round(mass / quantity, 4) if quantity else 0.0,
                                     round(mass, 4), round(float(row.get("volume") or 0.0), 4)])
            csv_info = {"csv": target, "csvBytes": os.path.getsize(target)}

        result = {
            "assembly": fix_text(part.Leaf),
            "sheet": fix_text(sheet.Name),
            "at": [round(x0, 2), round(y0, 2)],
            "columns": [label for _, label, _ in columns],
            "columnIds": [key for key, _, _ in columns],
            "language": "en" if str(args.get("language") or "zh").lower().startswith("en") else "zh",
            "rows": data_rows,
            "quantity": total_quantity,
            "mass": round(total_mass, 4),
            "table": {"width": round(table_width, 2),
                      "height": round(table_top - y0, 2),
                      "rect": [[round(x0, 2), round(y0, 2)],
                               [round(x0 + table_width, 2), round(table_top, 2)]]},
            "lines": len(pieces),
            "cells": cells,
            "suppressed": rollup["suppressed"],
        }
        result.update(csv_info)
        return result

    def open_path(path):
        """Open a part (reusing a loaded one) and put it on display."""
        wanted = os.path.normcase(os.path.abspath(path))
        for loaded in list(s.Parts):
            try:
                if os.path.normcase(os.path.abspath(fix_text(loaded.FullPath))) == wanted:
                    s.Parts.SetDisplay(loaded, True, True)
                    return loaded
            except Exception:
                continue
        result = s.Parts.OpenDisplay(path)
        handle = result[0] if isinstance(result, (tuple, list)) else result
        s.Parts.SetDisplay(handle, True, True)
        return handle

    def cmd_batch_drawings(args):
        """A3 sheet + PDF for each part of an assembly, or for a list of files."""
        paths = []
        for raw in (args.get("paths") or []):
            paths.append(str(raw))
        if not paths and args.get("fromAssembly", True):
            part = display_part()
            children = component_pool(part.ComponentAssembly) if getattr(part, "ComponentAssembly", None) else []
            for component in children:
                prototype = getattr(component, "Prototype", None)
                if prototype is None:
                    continue
                try:
                    path = fix_text(prototype.FullPath) or ""
                except Exception:
                    path = ""
                if path and os.path.exists(path):
                    paths.append(path)
        seen = []
        unique = []
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.append(key)
            unique.append(path)
        if not unique:
            raise RuntimeError("nothing to draw: pass paths, or open an assembly whose "
                               "components live on disk")
        out_dir = args.get("outDir") or os.path.join(OUT_DIR, "batch")
        out_dir = out_dir if os.path.isabs(out_dir) else os.path.join(OUT_DIR, out_dir)
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        size = str(args.get("size") or "A3").upper()
        views = args.get("views") or {"top": True, "front": True, "right": True, "isometric": True}
        reuse = args.get("reuse", True) is not False
        keep = state.get("part")
        rows = []
        for path in unique:
            entry = {"path": path}
            started_at = time.time()
            try:
                part = open_path(path)
                state["part"] = part
                entry["part"] = fix_text(part.Leaf)
                sheets = list(part.DraftingDrawingSheets)
                if not sheets:
                    cmd_drawing({"size": size, "projection": args.get("projection") or "first",
                                 "views": views, "autoScale": True})
                    entry["created"] = True
                elif not reuse:
                    cmd_drawing({"size": size, "projection": args.get("projection") or "first",
                                 "views": views, "autoScale": True})
                    entry["created"] = True
                else:
                    entry["created"] = False
                sheet = part.DraftingDrawingSheets.CurrentDrawingSheet
                entry["sheet"] = sheet_info(sheet)
                target = os.path.join(out_dir, re.sub(r"[^A-Za-z0-9_.-]+", "_",
                                                      os.path.splitext(os.path.basename(path))[0]) + ".pdf")
                printed = cmd_pdf({"path": target})
                entry["pdf"] = printed["path"]
                entry["pdfBytes"] = printed["bytes"]
                if not printed.get("exists") or not os.path.exists(target) or os.path.getsize(target) < 1000:
                    raise RuntimeError("the PDF was not written (%s bytes)" %
                                       printed.get("bytes", 0))
            except Exception as error:
                entry["error"] = "%s: %s" % (type(error).__name__, error)
            entry["seconds"] = round(time.time() - started_at, 1)
            rows.append(entry)
        if keep is not None:
            try:
                s.Parts.SetDisplay(keep, True, True)
                state["part"] = keep
            except Exception:
                pass
        ok = [row for row in rows if "pdf" in row and not row.get("error")]
        return {
            "outDir": out_dir,
            "parts": len(unique),
            "drawn": len(ok),
            "failed": [row for row in rows if row.get("error")],
            "rows": rows,
        }

    LAYER_LABELS = ("Hidden", "Selectable", "Visible", "WorkLayer")

    def layer_state_label(value):
        text = str(value)
        for label in LAYER_LABELS:
            if str(getattr(NX.Layer.State, label)) == text:
                return label
        return text

    def layer_number(raw, low=1, high=256):
        try:
            value = int(raw)
        except Exception:
            raise RuntimeError("layer must be a number between %d and %d" % (low, high))
        if value < low or value > high:
            raise RuntimeError("layer must be between %d and %d, got %d" % (low, high, value))
        return value

    def cmd_layers(args):
        """Layer states, object counts, moving displayable bodies between layers."""
        part = display_part()
        layers = part.Layers
        action = (args.get("action") or "list").lower()
        if action == "list":
            work = int(layers.WorkLayer)
            rows = []
            for number in range(1, 257):
                try:
                    label = layer_state_label(layers.GetState(number))
                    count = len(list(layers.GetAllObjectsOnLayer(number)))
                except Exception:
                    continue
                if count == 0 and label == "Hidden" and number != work:
                    continue
                row = {"layer": number, "state": label, "objects": count}
                if number == work:
                    row["work"] = True
                rows.append(row)
            return {"part": fix_text(part.Leaf), "workLayer": work, "layers": rows}
        if action == "move":
            number = layer_number(args.get("layer"))
            chosen = bodies_of(part, args.get("bodies"))
            if not chosen:
                raise RuntimeError("this part has no bodies to move")
            layers.MoveDisplayableObjects(number, chosen)
            return {"moved": len(chosen), "layer": number,
                    "objectsOnLayer": len(list(layers.GetAllObjectsOnLayer(number))),
                    "state": layer_state_label(layers.GetState(number))}
        if action in ("show", "hide", "selectable"):
            number = layer_number(args.get("layer"))
            constant = {"show": NX.Layer.State.Visible,
                        "hide": NX.Layer.State.Hidden,
                        "selectable": NX.Layer.State.Selectable}[action]
            layers.SetState(number, constant)
            return {"layer": number, "state": layer_state_label(layers.GetState(number)),
                    "objects": len(list(layers.GetAllObjectsOnLayer(number)))}
        if action == "work":
            number = layer_number(args.get("layer"))
            layers.WorkLayer = number
            return {"workLayer": int(layers.WorkLayer)}
        raise RuntimeError("unknown layers action %r (use list, move, show, hide, selectable, work)" % action)

    def cmd_refset(args):
        """Reference sets: list, create from bodies, delete by name."""
        part = display_part()
        action = (args.get("action") or "list").lower()

        def describe(refset):
            entry = {"name": fix_text(getattr(refset, "Name", "") or "")}
            try:
                entry["members"] = len(list(refset.AskMembersInReferenceSet()))
            except Exception:
                entry["members"] = None
            return entry

        if action == "list":
            rows = []
            for refset in part.GetAllReferenceSets():
                rows.append(describe(refset))
            return {"part": fix_text(part.Leaf), "referenceSets": rows, "count": len(rows)}
        if action == "create":
            name = args.get("name")
            if not name:
                raise RuntimeError("create needs a name for the reference set")
            chosen = bodies_of(part, args.get("bodies"))
            if not chosen:
                raise RuntimeError("this part has no bodies to put into the reference set")
            for existing in part.GetAllReferenceSets():
                if fix_text(getattr(existing, "Name", "") or "") == str(name):
                    raise RuntimeError("a reference set named %r already exists" % name)
            refset = part.CreateReferenceSet()
            refset.SetName(str(name))
            refset.AddObjectsToReferenceSet(chosen)
            return {"created": describe(refset), "bodies": len(chosen),
                    "referenceSets": [describe(r) for r in part.GetAllReferenceSets()]}
        if action == "delete":
            name = args.get("name")
            if not name:
                raise RuntimeError("delete needs the name of the reference set")
            target = None
            for existing in part.GetAllReferenceSets():
                if fix_text(getattr(existing, "Name", "") or "") == str(name):
                    target = existing
                    break
            if target is None:
                names = [fix_text(getattr(r, "Name", "") or "") for r in part.GetAllReferenceSets()]
                raise RuntimeError("no reference set named %r (have %s)" % (name, ", ".join(names) or "none"))
            part.DeleteReferenceSet(target)
            return {"deleted": str(name),
                    "referenceSets": [describe(r) for r in part.GetAllReferenceSets()]}
        raise RuntimeError("unknown refset action %r (use list, create, delete)" % action)

    # ------------------------------------------------------------- transform / pattern
    def update_option():
        return NX.SmartObject.UpdateOption.WithinModeling

    def direction_at(part, point, vector):
        return part.Directions.CreateDirection(
            NX.Point3d(float(point[0]), float(point[1]), float(point[2])),
            NX.Vector3d(float(vector[0]), float(vector[1]), float(vector[2])),
            update_option(),
        )

    def axis_at(part, point, vector):
        origin = NX.Point3d(float(point[0]), float(point[1]), float(point[2]))
        return part.Axes.CreateAxis(
            part.Points.CreatePoint(origin),
            direction_at(part, point, vector),
            update_option(),
        )

    def body_collector(part, picked):
        collector = part.ScCollectors.CreateCollector()
        collector.ReplaceRules([part.ScRuleFactory.CreateRuleBodyDumb(list(picked))], False)
        return collector

    def move_bodies(part, picked, setup):
        builder = part.Features.CreateMoveBodyBuilder(NX.Features.MoveBody.Null)
        builder.BodyToMove.ReplaceRules([part.ScRuleFactory.CreateRuleBodyDumb(list(picked))], False)
        setup(builder.Motion)
        return builder.CommitFeature()

    def bbox_of(picked):
        if not picked:
            return None
        box = s.Measurement.GetBoundingBoxProperties(list(picked), 0, NX.Point3d(0.0, 0.0, 0.0), False)
        sizes = [float(v) for v in box[2]]
        return {"size": sizes, "volume": float(box[5])}

    def cmd_transform(args):
        """Move one or more bodies: translate, rotate, or mirror across a plane."""
        part = display_part()
        mode = (args.get("mode") or "translate").lower()
        picked = pick_bodies(part, args.get("bodies"))
        if not picked:
            raise RuntimeError("this part has no bodies to transform")
        before = bbox_of(picked)

        if mode == "translate":
            if args.get("direction"):
                vector = [float(v) for v in args["direction"]]
            else:
                vector = [float(args.get("dx") or 0.0), float(args.get("dy") or 0.0), float(args.get("dz") or 0.0)]
            length = math.sqrt(sum(v * v for v in vector))
            if length < 1e-9:
                raise RuntimeError("translate needs a non-zero direction or dx/dy/dz")
            unit = [v / length for v in vector]
            distance = float(args.get("distance") or length)

            def setup(motion):
                motion.Option = motion.Options.Distance
                motion.DistanceVector = direction_at(part, [0.0, 0.0, 0.0], unit)
                motion.DistanceValue.SetFormula(expr_text(distance))

            feature = move_bodies(part, picked, setup)
            detail = {"direction": unit, "distance": distance}
        elif mode == "rotate":
            axis = [float(v) for v in (args.get("axis") or [0.0, 0.0, 1.0])]
            origin = [float(v) for v in (args.get("axisPoint") or [0.0, 0.0, 0.0])]
            angle = float(args.get("angle") or 90.0)

            def setup(motion):
                motion.Option = motion.Options.Angle
                motion.AngularAxis = axis_at(part, origin, axis)
                motion.Angle.SetFormula(expr_text(angle))

            feature = move_bodies(part, picked, setup)
            detail = {"axis": axis, "axisPoint": origin, "angle": angle}
        elif mode == "mirror":
            normal = [float(v) for v in (args.get("normal") or [1.0, 0.0, 0.0])]
            origin = [float(v) for v in (args.get("planePoint") or [0.0, 0.0, 0.0])]
            length = math.sqrt(sum(v * v for v in normal))
            if length < 1e-9:
                raise RuntimeError("mirror needs a non-zero plane normal")
            normal = [v / length for v in normal]
            datum = part.Features.CreateDatumPlaneBuilder(NX.Features.DatumPlaneFeature.Null)
            datum.SetPointAndDirection(
                part.Points.CreatePoint(NX.Point3d(float(origin[0]), float(origin[1]), float(origin[2]))),
                direction_at(part, origin, normal),
            )
            plane_feature = datum.CommitFeature()
            builder = part.Features.CreateMirrorBodyBuilder(NX.Features.MirrorBody.Null)
            builder.MirrorBodyCollector.ReplaceRules([part.ScRuleFactory.CreateRuleBodyDumb(list(picked))], False)
            builder.Plane.Value = plane_feature.DatumPlane
            keep = args.get("keepOriginal")
            if keep is not None:
                builder.DeleteSourceBody = not bool(keep)
            feature = builder.CommitFeature()
            detail = {"planePoint": origin, "normal": normal, "keepOriginal": bool(keep)}
        else:
            raise RuntimeError("unknown transform mode %r (use translate, rotate, mirror)" % mode)

        after = bbox_of(picked)
        return {
            "mode": mode,
            "feature": type(feature).__name__,
            "bodies": len(picked),
            "detail": detail,
            "before": before,
            "after": after,
            "partBodies": len(list(part.Bodies)),
            "measure": measure(part),
        }

    def cmd_pattern(args):
        """Pattern bodies linearly or circularly."""
        import NXOpen.GeometricUtilities as GU  # noqa: F401  (module import kept local)

        part = display_part()
        mode = (args.get("mode") or "linear").lower()
        picked = pick_bodies(part, args.get("bodies"))
        if not picked:
            raise RuntimeError("this part has no bodies to pattern")
        count = max(2, int(args.get("count") or 3))

        builder = part.Features.CreatePatternGeometryBuilder(NX.Features.PatternGeometry.Null)
        for body in picked:
            builder.GeometryToPattern.Add(body)
        definition = builder.PatternService

        if mode == "linear":
            pitch = float(args.get("pitch") or args.get("spacing") or 50.0)
            direction = [float(v) for v in (args.get("direction") or [1.0, 0.0, 0.0])]
            length = math.sqrt(sum(v * v for v in direction))
            if length < 1e-9:
                raise RuntimeError("linear pattern needs a non-zero direction")
            unit = [v / length for v in direction]
            definition.PatternType = GU.PatternDefinitionPatternEnum.Linear
            rect = definition.RectangularDefinition
            rect.UseYDirectionToggle = False
            rect.XSpacing.NCopies.SetFormula(str(count))
            rect.XSpacing.PitchDistance.SetFormula(expr_text(pitch))
            rect.XDirection = part.Directions.CreateDirection(
                NX.Point3d(0.0, 0.0, 0.0), NX.Vector3d(unit[0], unit[1], unit[2]), update_option()
            )
            detail = {"direction": unit, "pitch": pitch, "count": count}
        elif mode == "circular":
            angle = float(args.get("angle") or 360.0)
            axis = [float(v) for v in (args.get("axis") or [0.0, 0.0, 1.0])]
            origin = [float(v) for v in (args.get("axisPoint") or [0.0, 0.0, 0.0])]
            definition.PatternType = GU.PatternDefinitionPatternEnum.Circular
            circ = definition.CircularDefinition
            circ.AngularSpacing.NCopies.SetFormula(str(count))
            circ.AngularSpacing.SpanAngle.SetFormula(expr_text(angle))
            circ.RotationAxis = axis_at(part, origin, axis)
            detail = {"axis": axis, "axisPoint": origin, "angle": angle, "count": count}
        else:
            raise RuntimeError("unknown pattern mode %r (use linear, circular)" % mode)

        feature = builder.CommitFeature()
        return {
            "mode": mode,
            "feature": type(feature).__name__,
            "detail": detail,
            "bodies": len(list(part.Bodies)),
            "measure": measure(part),
        }

        # --------------------------------------------------------- expressions (P0-2)
    def expression_rows(limit=None, needle=None):
        rows = []
        seen = 0
        part = display_part()
        for expr in list(part.Expressions):
            seen += 1
            try:
                name = fix_text(getattr(expr, "Name", "") or "")
            except Exception:
                continue
            if needle and needle.lower() not in name.lower():
                continue
            try:
                value = float(getattr(expr, "Value", 0.0) or 0.0)
            except Exception:
                value = None
            try:
                formula = getattr(expr, "RightHandSide", None) or ""
            except Exception:
                formula = ""
            try:
                kind = str(getattr(expr, "Type", ""))
            except Exception:
                kind = ""
            try:
                description = fix_text(getattr(expr, "Description", "") or "")
            except Exception:
                description = ""
            rows.append({
                "name": name,
                "value": value,
                "formula": fix_text(formula),
                "type": kind,
                "description": description,
            })
            if limit is not None and len(rows) >= limit:
                break
        return rows, seen

    def find_expression(name):
        target = (name or "").strip().lower()
        if not target:
            raise RuntimeError("expression name is required")
        part = display_part()
        for expr in list(part.Expressions):
            try:
                current = fix_text(getattr(expr, "Name", "") or "")
            except Exception:
                continue
            if current.lower() == target:
                return expr
        raise RuntimeError("no expression named %r" % name)

    def cmd_expression(args):
        part = display_part()
        action = (args.get("action") or "list").lower()
        limit = args.get("limit")
        limit = int(limit) if limit not in (None, "") else None
        needle = args.get("filter") or None
        result = {"action": action}

        if action == "list":
            rows, total = expression_rows(limit=None, needle=needle)
            if limit is not None and limit > 0:
                rows = rows[:limit]
            result.update({"expressions": rows, "count": len(rows), "total": total})
            return result

        if action == "set":
            name = args.get("name")
            formula = args.get("formula")
            if formula is None and args.get("value") is not None:
                formula = expr_text(float(args.get("value")))
            if formula is None:
                raise RuntimeError("action=set needs a formula (or value)")
            expr = find_expression(name)
            before = {
                "value": float(getattr(expr, "Value", 0.0) or 0.0),
                "formula": fix_text(getattr(expr, "RightHandSide", "") or ""),
            }
            measure_before = measure(part)
            expr.SetFormula(str(formula))
            s.UpdateManager.DoUpdate(1)
            after = {
                "value": float(getattr(expr, "Value", 0.0) or 0.0),
                "formula": fix_text(getattr(expr, "RightHandSide", "") or ""),
            }
            result.update({
                "name": fix_text(getattr(expr, "Name", "") or name),
                "before": before,
                "after": after,
                "measureBefore": measure_before,
                "measureAfter": measure(part),
            })
            return result

        if action == "create":
            name = (args.get("name") or "").strip()
            formula = args.get("formula")
            if not name or formula is None:
                raise RuntimeError("action=create needs name and formula")
            created = part.Expressions.CreateExpression("Number", "%s = %s" % (name, formula))
            s.UpdateManager.DoUpdate(1)
            result.update({
                "name": fix_text(getattr(created, "Name", "") or name),
                "value": float(getattr(created, "Value", 0.0) or 0.0),
                "formula": fix_text(getattr(created, "RightHandSide", "") or ""),
            })
            return result

        if action == "delete":
            expr = find_expression(args.get("name"))
            label = fix_text(getattr(expr, "Name", "") or "")
            part.Expressions.Delete(expr)
            s.UpdateManager.DoUpdate(1)
            result.update({"name": label, "deleted": True})
            return result

        raise RuntimeError("unknown expression action %r (list | set | create | delete)" % action)

# ------------------------------------------------------------- FE / NX Nastran
    CAE_TIMEOUT = float(os.environ.get("NXC_CAE_TIMEOUT") or 900)
    OUT_DIR = os.environ.get("NXBRIDGE_OUT") or r"D:\dsh\work\nx\out"

    def cae_python():
        """The bundled runtime carries Pillow; NX's own python usually does not."""
        candidates = [os.environ.get("NXC_CAE_PYTHON") or "",
                      r"C:\Users\a1053\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
                      r"C:\Python314\python.exe"]
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return sys.executable

    def cae_script():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cae_run.py")

    def clamp_int(value, low, high):
        return int(max(low, min(high, int(value))))

    def cae_spec_from_part(part, args):
        """Box-projection spec from the current part: L along X, W along Y, H along Z."""
        bodies = bodies_of(part, args.get("bodies"))
        box = s.Measurement.GetBoundingBoxProperties(bodies, 0, NX.Point3d(0.0, 0.0, 0.0), False)
        extents = [round(float(v), 4) for v in box[2]]
        element = float(args.get("elementSize") or max(extents) / 30.0)
        material = args.get("material") or {"E": 210000.0, "nu": 0.3}
        spec = {
            "name": fix_text(getattr(part, "Leaf", "")) or "part",
            "length": extents[0],
            "width": extents[1],
            "height": extents[2],
            "nx": clamp_int(round(extents[0] / element), 4, 80),
            "ny": clamp_int(round(extents[1] / element), 2, 40),
            "nz": clamp_int(round(extents[2] / element), 1, 30),
            "load": float(args.get("load") or 200.0),
            "direction": args.get("direction") or "z",
            "material": {"E": float(material.get("E", 210000.0)), "nu": float(material.get("nu", 0.3))},
            "source": {"part": fix_text(getattr(part, "FullPath", "")), "bbox": extents,
                       "elementSize": round(element, 4), "bodies": len(bodies)},
        }
        return spec

    def cmd_cae_part(args):
        part = display_part()
        return {"spec": cae_spec_from_part(part, args), "python": cae_python(),
                "note": "box projection of the current part; edit length/width/height/nx/ny/nz before cae_solve"}

    def cmd_cae_solve(args):
        workdir = args.get("workdir") or os.path.join(OUT_DIR, "cae", time.strftime("%Y%m%d_%H%M%S"))
        if not os.path.isdir(workdir):
            os.makedirs(workdir)
        spec = args.get("spec")
        if not spec:
            spec = cae_spec_from_part(display_part(), args)
        for key in ("load", "direction", "length", "width", "height", "nx", "ny", "nz"):
            if key in args and args[key] not in (None, ""):
                spec[key] = args[key]
        if args.get("material"):
            spec["material"] = args["material"]
        spec_path = os.path.join(workdir, "spec.json")
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump(spec, handle, ensure_ascii=False, indent=2)
        result_path = os.path.join(workdir, "result.json")
        if os.path.exists(result_path):
            os.remove(result_path)
        started = time.time()
        proc = subprocess.run([cae_python(), cae_script(), spec_path, workdir, result_path],
                              capture_output=True, text=True, timeout=CAE_TIMEOUT)
        elapsed = round(time.time() - started, 1)
        if not os.path.exists(result_path):
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
            raise RuntimeError("FE pipeline failed after %ss (exit %s): %s" % (elapsed, proc.returncode, tail))
        with open(result_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        log("cae_solve", workdir, "%.1fs" % elapsed)
        return {"spec": spec, "specPath": spec_path, "workdir": workdir, "wallSeconds": elapsed,
                "solver": data.get("solver"), "results": data.get("results"),
                "files": data.get("files"), "mesh": data.get("mesh")}


    def cae_solid_script():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cae_solid.py")

    def cae_solid_spec(part, args, workdir):
        """The real geometry, not a box: mesh the exported STL, keep the part's features."""
        bodies = bodies_of(part, args.get("bodies"))
        box = s.Measurement.GetBoundingBoxProperties(bodies, 0, NX.Point3d(0.0, 0.0, 0.0), False)
        extents = [float(v) for v in box[2]]
        leaf = fix_text(getattr(part, "Leaf", "")) or "part"
        stl = args.get("stl") or os.path.join(workdir, "%s.stl" % leaf)
        if not os.path.exists(stl):
            cmd_export({"format": "stl", "path": stl,
                        "tolerance": float(args.get("tolerance") or 0.15),
                        "bodies": args.get("bodies")})
        material = {"E": 210000.0, "nu": 0.3, "rho": 7.85e-9}
        for key, value in (args.get("material") or {}).items():
            if key in material and value not in (None, ""):
                material[key] = float(value)
        loads = args.get("loads")
        if not loads:
            loads = [{"type": "force", "face": args.get("face") or "+z",
                      "direction": args.get("direction") or [0.0, 0.0, -1.0],
                      "value": float(args.get("load") or 1000.0)}]
        cad_volume = None
        try:
            cad_volume = float(measure(part, args.get("bodies"))["volume"])
        except Exception:
            cad_volume = None
        return {"stl": stl,
                "title": leaf,
                "mesh": args.get("mesh") or "draped",
                "elementSize": float(args.get("elementSize") or max(extents) / 40.0),
                "material": material,
                "fix": args.get("fix") or ["-x"],
                "loads": loads,
                "solver": args.get("solver") or "statics",
                "modes": int(args.get("modes") or 6),
                "source": {"part": fix_text(getattr(part, "FullPath", "")),
                           "bbox": [round(v, 3) for v in extents],
                           "volume": cad_volume}}

    def cmd_cae_solid(args):
        part = display_part()
        workdir = args.get("workdir") or os.path.join(OUT_DIR, "cae_solid", time.strftime("%Y%m%d_%H%M%S"))
        if not os.path.isdir(workdir):
            os.makedirs(workdir)
        spec = args.get("spec") or cae_solid_spec(part, args, workdir)
        for key in ("elementSize", "material", "fix", "loads", "solver", "modes"):
            if args.get(key) not in (None, "", []):
                spec[key] = args[key]
        spec_path = os.path.join(workdir, "spec.json")
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump(spec, handle, ensure_ascii=False, indent=2)
        result_path = os.path.join(workdir, "result.json")
        if os.path.exists(result_path):
            os.remove(result_path)
        started = time.time()
        proc = subprocess.run([cae_python(), cae_solid_script(), spec_path, workdir, result_path],
                              capture_output=True, text=True, timeout=CAE_TIMEOUT * 2)
        elapsed = round(time.time() - started, 1)
        if not os.path.exists(result_path):
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1500:]
            raise RuntimeError("solid FE failed after %ss (exit %s): %s" % (elapsed, proc.returncode, tail))
        with open(result_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        log("cae_solid", workdir, "%.1fs" % elapsed)
        return {"spec": spec, "specPath": spec_path, "workdir": workdir, "wallSeconds": elapsed,
                "mesh": data.get("mesh"), "solver": data.get("solver"),
                "results": data.get("results"), "files": data.get("files")}


    # ------------------------------------------------------------- NX Motion (运动仿真)
    MOTION_TIMEOUT = float(os.environ.get("NXC_MOTION_TIMEOUT") or 1800)
    MOTION_SOLVER_NAMES = {"-1": "未指定", "0": "Recurdyn", "1": "Adams", "2": "Simcenter", "3": "ScDesigner"}

    def motion_module():
        import NXOpen.Motion as NXMotion
        return NXMotion

    def motion_session():
        return s.MotionSession

    def find_open_part(path):
        wanted = os.path.normcase(os.path.abspath(path))
        for loaded in list(s.Parts):
            try:
                if os.path.normcase(os.path.abspath(fix_text(loaded.FullPath))) == wanted:
                    return loaded
            except Exception:
                continue
        return None

    def motion_part(args):
        path = args.get("path")
        target = state.get("part")
        if path:
            existing = find_open_part(path)
            if existing is not None:
                target = existing
            else:
                opened = s.Parts.OpenDisplay(os.path.abspath(path))
                target = opened[0] if isinstance(opened, tuple) else opened
            state["part"] = target
            try:
                s.Parts.SetDisplay(target, False, True)
            except Exception:
                pass
            for loaded in list(s.Parts):
                try:
                    if loaded is not target:
                        loaded.Close(NX.BasePartCloseWholeTree.FalseValue,
                                     NX.BasePartCloseModified.CloseModified, None)
                except Exception:
                    continue
        if target is None:
            raise RuntimeError("no motion part is open; pass path to a .sim file")
        return target

    def motion_solution(target, args):
        solutions = list(target.MotionManager.MotionSolutions)
        if not solutions:
            raise RuntimeError("no motion solution in %s" % fix_text(getattr(target, "Leaf", "")))
        wanted = args.get("solution")
        if wanted:
            for solution in solutions:
                if str(solution.Name) == str(wanted):
                    return solution
            raise RuntimeError("solution %s not found (have: %s)"
                               % (wanted, ", ".join(str(item.Name) for item in solutions)))
        return solutions[0]

    def motion_init(target):
        session = motion_session()
        try:
            session.InitializeSimulation(target)
        except Exception as error:
            if "already" not in str(error).lower() and "initialized" not in str(error).lower():
                raise
        return session

    def motion_overview(target, args):
        session = motion_init(target)
        solution = motion_solution(target, args)
        manager = target.MotionManager
        links = list(manager.Links)
        joints = list(manager.Joints)
        couplers = list(manager.Couplers)
        drivers = list(manager.JointDrivers)
        dof = None
        try:
            dof = session.CalculateDof(target, solution)
        except Exception:
            dof = None
        solver_code = str(session.Environments.GetSolver())
        return {
            "part": fix_text(getattr(target, "Leaf", "")),
            "fullPath": fix_text(getattr(target, "FullPath", "")),
            "solver": MOTION_SOLVER_NAMES.get(solver_code, "solver %s" % solver_code),
            "solverCode": solver_code,
            "solutions": [str(item.Name) for item in list(manager.MotionSolutions)],
            "solution": str(solution.Name),
            "dof": dof,
            "links": [str(item.Name) for item in links],
            "joints": [str(item.Name) for item in joints],
            "couplers": ["%s:%s" % (type(item).__name__, getattr(item, "Name", "?")) for item in couplers],
            "jointDrivers": len(drivers),
            "isMotionPart": True,
        }


    def motion_control(target, args):
        solution = motion_solution(target, args)
        control = solution.GetAnimationControl()
        return solution, control

    def motion_frames(target, args):
        """Sample every link transformation over the solved steps."""
        session = motion_init(target)
        solution, control = motion_control(target, args)
        steps = int(control.NumberSteps)
        current = control.CurrentStep
        sample_count = clamp_int(args.get("samples") or 101, 2, 501)
        if sample_count >= steps:
            sampled = list(range(steps))
        else:
            step_size = (steps - 1) / float(sample_count - 1)
            sampled = sorted({int(round(index * step_size)) for index in range(sample_count)})
        links = list(target.MotionManager.Links)
        series = []
        summary = []
        started = time.time()
        for link in links:
            frames = []
            for step in sampled:
                matrix = control.GetLinkTransformation(link, step)
                rotation = [matrix.Rxx, matrix.Rxy, matrix.Rxz,
                            matrix.Ryx, matrix.Ryy, matrix.Ryz,
                            matrix.Rzx, matrix.Rzy, matrix.Rzz]
                origin = [matrix.Xt, matrix.Yt, matrix.Zt]
                frames.append([step, [round(value, 6) for value in rotation],
                               [round(value, 4) for value in origin]])
            name = str(link.Name)
            first = frames[0]
            last = frames[-1]
            span = sum((last[2][index] - first[2][index]) ** 2 for index in range(3)) ** 0.5
            angle_first = motion_angle(first[1])
            angle_last = motion_angle(last[1])
            excursion = 0.0
            swing = 0.0
            for frame in frames:
                offset = sum((frame[2][index] - first[2][index]) ** 2 for index in range(3)) ** 0.5
                excursion = max(excursion, offset)
                swing = max(swing, abs(motion_angle(frame[1]) - angle_first))
            series.append({"name": name, "frames": frames})
            summary.append({"link": name,
                            "start": first[2], "end": last[2],
                            "deltaMm": round(span, 4),
                            "rotDeg": round(angle_last - angle_first, 4),
                            "excursionMm": round(excursion, 4),
                            "swingDeg": round(swing, 4)})
        try:
            control.StepTo(current if current >= 0 else 0)
        except Exception:
            pass
        control.Finish()
        payload = {
            "title": args.get("title") or "NX Motion 运动仿真结果",
            "subtitle": args.get("subtitle") or [],
            "steps": sampled,
            "links": series,
            "summary": summary,
            "footer": args.get("footer") or [],
        }
        workdir = args.get("workdir") or os.path.join(OUT_DIR, "motion")
        if not os.path.isdir(workdir):
            os.makedirs(workdir)
        frames_path = os.path.join(workdir, "motion_frames.json")
        with open(frames_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        csv_path = os.path.join(workdir, "motion_frames.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write("link,step,rotdeg,x,y,z,rxx,rxy,rxz,ryx,ryy,ryz,rzx,rzy,rzz\n")
            for link in series:
                base = motion_angle(link["frames"][0][1])
                for frame in link["frames"]:
                    handle.write("%s,%d,%.5f,%.4f,%.4f,%.4f,%s\n" % (
                        link["name"], frame[0], motion_angle(frame[1]) - base,
                        frame[2][0], frame[2][1], frame[2][2],
                        ",".join("%.6f" % value for value in frame[1])))
        moving = [row for row in summary if row["excursionMm"] > 0.05 or row["swingDeg"] > 0.2]
        moving.sort(key=lambda row: -row["deltaMm"])
        return {"part": fix_text(getattr(target, "Leaf", "")),
                "solution": str(solution.Name),
                "steps": len(sampled),
                "totalSteps": steps,
                "links": len(series),
                "sampleSeconds": round(time.time() - started, 1),
                "summary": summary,
                "moving": moving[:10],
                "files": {"frames": frames_path, "csv": csv_path}}

    def motion_angle(rotation):
        trace = rotation[0] + rotation[4] + rotation[8]
        value = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
        return math.degrees(math.acos(value))

    def motion_python():
        return cae_python()

    def motion_viz_script():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "motion_viz.py")

    def cmd_motion_open(args):
        target = motion_part(args)
        return motion_overview(target, args)

    def cmd_motion_status(args):
        target = motion_part(args)
        return motion_overview(target, args)

    def cmd_motion_solve(args):
        target = motion_part(args)
        motion_init(target)
        solution = motion_solution(target, args)
        started = time.time()
        solution.SolveNormalRunSolution()
        elapsed = round(time.time() - started, 1)
        control = solution.GetAnimationControl()
        steps = int(control.NumberSteps)
        current = int(control.CurrentStep)
        control.Finish()
        return {"part": fix_text(getattr(target, "Leaf", "")),
                "solution": str(solution.Name),
                "solveSeconds": elapsed,
                "steps": steps,
                "currentStep": current}

    def cmd_motion_frames(args):
        target = motion_part(args)
        return motion_frames(target, args)

    def cmd_motion_report(args):
        target = motion_part(args)
        workdir = args.get("workdir") or os.path.join(OUT_DIR, "motion")
        args = dict(args)
        args["workdir"] = workdir
        overview = motion_overview(target, args)
        _solution, _control = motion_control(target, args)
        total_steps = int(_control.NumberSteps)
        _control.Finish()
        if not args.get("subtitle"):
            args["subtitle"] = [
                "求解器 %s · 解算方案 %s · 时间步 %d" % (
                    overview.get("solver"), overview.get("solution"), total_steps),
                "构件 %d 个 · 运动副 %d 个 · 齿条齿轮副 %d 个（NX 自带运动求解器 mechanisms 许可）" % (
                    len(overview.get("links") or []), len(overview.get("joints") or []),
                    len(overview.get("couplers") or [])),
            ]
            args["footer"] = ["nxworker motion_report · 数据来源 NX Motion 求解结果 (.mres)",
                              "文件：motion_frames.json / motion_frames.csv / motion_report.png / motion_animation.gif"]
        frames = motion_frames(target, args)
        started = time.time()
        proc = subprocess.run([motion_python(), motion_viz_script(),
                               frames["files"]["frames"], workdir],
                              capture_output=True, text=True, timeout=600)
        rendered = None
        if proc.stdout:
            tail = proc.stdout.strip().splitlines()[-1]
            try:
                rendered = json.loads(tail)
            except Exception:
                rendered = None
        if rendered is None:
            raise RuntimeError("motion visualisation failed (exit %s): %s"
                               % (proc.returncode, ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]))
        return {"part": overview.get("part"), "solution": frames.get("solution"),
                "dof": overview.get("dof"), "totalSteps": frames.get("totalSteps"),
                "links": frames.get("links"), "moving": frames.get("moving"),
                "renderSeconds": round(time.time() - started, 1),
                "files": {"frames": frames["files"]["frames"], "csv": frames["files"]["csv"],
                          "report": rendered.get("report"), "animation": rendered.get("animation")},
                "sizes": {"report": rendered.get("reportBytes"), "animation": rendered.get("animationBytes")}}

    def cmd_quit(args):
        write_json(READY, {"ok": True, "stopping": True})
        return {"bye": True}

    handlers = {
        "status": cmd_status,
        "open": cmd_open,
        "info": cmd_info,
        "measure": cmd_measure,
        "export": cmd_export,
        "drawing": cmd_drawing,
        "pdf": cmd_pdf,
        "annotate": cmd_annotate,
        "section_view": cmd_section_view,
        "detail_view": cmd_detail_view,
        "note": cmd_note,
        "whoami": cmd_whoami,
        "exec": cmd_exec,
        "new": cmd_new,
        "primitive": cmd_primitive,
        "boolean": cmd_boolean,
        "fillet": cmd_fillet,
        "chamfer": cmd_chamfer,
        "extrude": cmd_extrude,
        "revolve": cmd_revolve,
        "hole": cmd_hole,
        "thread": cmd_thread,
        "curve": cmd_curve,
        "surface": cmd_surface,
        "datum": cmd_datum,
        "sketch": cmd_sketch,
        "shell": cmd_shell,
        "faces": cmd_faces,
        "save": cmd_save,
        "polygon": cmd_polygon,
        "asm_add": cmd_asm_add,
        "asm_move": cmd_asm_move,
        "asm_fix": cmd_asm_fix,
        "asm_mate": cmd_asm_mate,
        "asm_list": cmd_asm_list,
        "asm_gap": cmd_asm_gap,
        "component_pattern": cmd_component_pattern,
        "component_op": cmd_component_op,
        "explode": cmd_explode,
        "asm_bom": cmd_assembly_bom,
        "bom_table": cmd_bom_table,
        "batch_drawings": cmd_batch_drawings,
        "asm_clearance": cmd_assembly_clearance,
        "layers": cmd_layers,
        "refset": cmd_refset,
        "transform": cmd_transform,
        "pattern": cmd_pattern,
        "expression": cmd_expression,
        "cae_part": cmd_cae_part,
        "cae_solve": cmd_cae_solve,
        "cae_solid": cmd_cae_solid,
        "motion_open": cmd_motion_open,
        "motion_status": cmd_motion_status,
        "motion_solve": cmd_motion_solve,
        "motion_frames": cmd_motion_frames,
        "motion_report": cmd_motion_report,
        "quit": cmd_quit,
    }

    write_json(READY, {
        "ok": True,
        "pid": os.getpid(),
        "release": s.ReleaseNumber,
        "fullRelease": s.FullReleaseNumber,
        "root": ROOT,
        "commands": sorted(handlers),
        "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    log("ready root=%s release=%s pid=%s" % (ROOT, s.ReleaseNumber, os.getpid()))

    stopping = False
    while not stopping:
        if time.time() - started > MAXSECONDS:
            log("max seconds reached, exiting")
            break
        files = sorted(glob.glob(os.path.join(REQ, "*.json")))
        if not files:
            time.sleep(0.05)
            continue
        for path in files:
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    req = json.load(f)
            except Exception:
                continue
            rid = req.get("id") or os.path.basename(path).split(".")[0]
            cmd = req.get("cmd")
            args = req.get("args") or {}
            t0 = time.time()
            resp = {"id": rid, "ok": True, "result": None, "log": []}
            try:
                handler = handlers.get(cmd)
                if handler is None:
                    raise RuntimeError("unknown command: %s" % cmd)
                log("-> %s %s" % (rid, cmd))
                fully_loaded.seen = 0  # budget is per command, not per session
                resp["result"] = handler(args)
                if cmd == "quit":
                    stopping = True
            except Exception as e:
                resp["ok"] = False
                resp["error"] = "%s: %s" % (type(e).__name__, e)
                resp["trace"] = traceback.format_exc()
                log("!! %s %s" % (rid, resp["error"]))
            resp["ms"] = int((time.time() - t0) * 1000)
            write_json(os.path.join(RESP, rid + ".json"), resp)
            try:
                os.remove(path)
            except Exception:
                pass

    log("exit")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(FATAL, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n")
        raise
