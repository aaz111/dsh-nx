# dsh-nx

Siemens NX tools for [dsh](https://github.com/deepseek-ai): solid and sheet modelling, curves,
datums and sketches, engineering drawings, PDF, STEP/IGES/STL export, assemblies with BOM and
batch drafting, and NX Nastran FE (statics + modal) — all driven in a **background NX session**,
so NX's GUI never has to be touched. A sidebar panel watches that session and fires the common
actions.

Version 0.3.0 adds: curves and sheets, datums and real sketches, threads, section/detail views
and drawing notes, component patterns / replace / suppress, explode, solid-geometry FE with
contour plots, BOM tables drawn on the sheet, and per-part batch PDF.

## What it gives the model

| tool | does |
| --- | --- |
| `nx_status`, `nx_session` | is the NX session up, its release, open parts; stop / restart it |
| `nx_open`, `nx_new`, `nx_info`, `nx_save` | open .prt/.step/.iges, create a mm part, inspect, save |
| `nx_measure`, `nx_faces` | volume, area, mass, bounding box, centroid; faces with area + centre |
| `nx_primitive`, `nx_extrude`, `nx_revolve`, `nx_polygon` | blocks/cylinders/cones/spheres, extruded and revolved profiles |
| `nx_curve`, `nx_surface` | lines / arcs / circles / splines; lofts through sections, mesh patches, extruded sheets |
| `nx_datum`, `nx_sketch` | datum planes / axes / points; real sketches (rect, circle, line, polyline) with automatic constraints, optional extrude |
| `nx_boolean`, `nx_fillet`, `nx_chamfer`, `nx_hole`, `nx_thread`, `nx_shell` | combine and refine solids; real NX Hole and Thread features |
| `nx_transform`, `nx_pattern`, `nx_expression` | move/rotate/mirror a body, linear/circular patterns, read/write driven expressions |
| `nx_drawing`, `nx_pdf`, `nx_annotate` | sheet with projected views placed automatically, PDF; auto-annotation picks up existing PMI |
| `nx_section_view`, `nx_detail_view`, `nx_note` | section views with a cut line, detail views, drafting notes (CJK font handled) |
| `nx_bom_table`, `nx_batch_drawings` | the BOM drawn as a ruled table on the sheet; one A3 sheet + PDF per part of an assembly |
| `nx_export` | STEP / IGES / STL (waits for NX's asynchronous translator) |
| `nx_assembly_add`, `nx_assembly_move`, `nx_assembly_fix`, `nx_assembly_mate`, `nx_assembly_list`, `nx_assembly_gap` | build and verify assemblies, including minimum-distance fit checks |
| `nx_component_pattern`, `nx_component_op`, `nx_explode` | component patterns, replace/suppress/remove, exploded arrangement (real positions, restorable) |
| `nx_assembly_bom`, `nx_assembly_clearance` | BOM rolled up by part file (quantity/volume/mass, CSV) and a pairwise gap matrix that flags interference |
| `nx_layers`, `nx_refset` | layer states + moving bodies between layers, work layer; reference set list/create/delete |
| `nx_fea_spec`, `nx_fea_solve` | NX Nastran SOL 101 cantilever study on a box mesh; PNG, deformed STL, SVG, result.json |
| `nx_fea_solid` | FE on the REAL geometry: voxel CHEXA8 or surface-draped mesh, force/pressure/moment/gravity/centrifugal loads, statics or modal, contour plots |
| `nx_python` | escape hatch: NXOpen code inside the live session |

Units are mm, degrees and kg everywhere. NX Motion is deliberately **not** exposed.
NX has no interference-check API in this build, so `nx_assembly_clearance` measures every
component pair with the minimum distance instead (below `tolerance` = overlap).

## How it runs

`python/nxworker.py` is a resident journal: `run_journal.exe nxworker.py` starts NX once
(~25 s), writes `ready.json`, then answers `req/<id>.json` with `resp/<id>.json` until it
times out or is asked to quit. `lib/bridge.js` spawns it detached (so it outlives dsh turns),
serialises requests, and kills the NX process tree when a command hangs.

The FE side is pure python under the bundled dsh interpreter: `cae_run.py` writes the bulk-data
deck and reads the `.f06`, `cae_solid.py` meshes real geometry, `cae_drape.py` pulls skin nodes
onto the STL surface, `cae_plot.py` renders the contour PNGs (numpy + Pillow, no matplotlib).

Requires a local NX install (defaults to `D:\software\NX`, override with `nxHome` or
`UGII_BASE_DIR`) plus the `simulation`/`nastran` licences for the FE tools.

## Sidebar panel

The bundle also ships a web UI: an **NX** icon in the dsh sidebar opens a panel showing the
background session (pid, bridge folder, NX home, command count, uptime, open part), a table of
recent artefacts and a row of quick actions — open a part, info, measure, export STEP/STL, A3
drawing, PDF, BOM, clearance matrix, layers. Everything runs through the host routes that
`lib/panel.js` registers with the dsh web server:

- `GET /dsh-nx/state` — bridge status plus recent files under `outputDir`; add `?probe=1` to
  also ask NX for release/uptime/open parts (the only request that may start the NX session).
- `POST /dsh-nx/action` — `{command, args, timeoutMs}` against a whitelist
  (`status, open, info, measure, save, export, drawing, pdf, annotate, faces, asm_list,
  asm_bom, asm_clearance, layers, refset`; Motion and `nx_python` are excluded). Output paths
  default to `<outputDir>/panel/`.
- `POST /dsh-nx/preview` + `GET /dsh-nx/preview-file` — tessellate a part or whole assembly
  (all bodies, 0.02 mm / 8°) and serve the STL to the viewer card; results are cached by
  name/mtime/size under `out/panel/preview`.

Config keys: `panel: false` skips the routes, `outputDir` (default `D:\dsh\work\nx\out`) is
where artefacts are collected. The panel is optional — the tools work without it.

## Limits worth knowing

- DXF/DWG export hangs NX's translator in batch — use STEP/IGES/STL.
- Native drafting dimensions cannot be created (auto-annotation only sees existing PMI), and
  sketch dimensions cannot be created either.
- Projection views never get hidden lines in batch, and detail views come out as an empty
  boundary circle: the batch session has no hidden-line-removal pass.
- A template sheet prints its views but not the frame/title block; `nx_bom_table` draws a table
  with sheet curves plus one note per cell because NX exposes no tabular-note API.
- The assembly solver registers mates but does not move components: position with
  `nx_assembly_move`, verify with `nx_assembly_gap`.
- `nx_explode` moves real component positions (so drawings show the exploded arrangement) and
  stores the originals in a part attribute for `restore: true`; NX's own Explosion is a
  display state only and has no drawing-view API.
- An assembly cannot be exported to STL directly (it owns no body) — use the panel preview or
  export a component.
- `nx_fillet` refuses overlapping blends; round fewer edges per call.
- `nx_fea_solid` builds a voxel mesh and, by default, drapes its skin onto the surface: volume
  error drops from −1.3% to −0.9% and the stair steps disappear, but holes keep a voxel-shaped
  rim because draping moves nodes, it does not add or remove them. A conforming mesher means
  Simcenter, which this session cannot start.
- Nastran needs an explicit memory request here (`mem=2000mb`); the default open-core request
  is 14 GB and fails on a desktop.
