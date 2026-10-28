export const GUIDE = `
## Siemens NX (dsh-nx)

A background NX session is driven through JSON commands; the first nx_* call starts it
(about 25 s) and it then stays resident, so keep it for the whole task. NX's own GUI is
not needed and must not be automated — never send keystrokes or clicks to ugraf.exe.

Units are always mm, degrees and kg. Every coordinate is model space [x, y, z] mm, Z up.

Work order for a modelling task:
1. nx_status — is a session running, which parts are open. Do this first.
2. nx_open (existing .prt/.step/.iges) or nx_new (fresh millimetre part).
3. Build geometry with nx_primitive / nx_extrude / nx_revolve, combine with nx_boolean,
   refine with nx_fillet / nx_chamfer / nx_hole / nx_shell. nx_faces lists face indices and
   centres (what nx_shell openFaces and mate references need).
   Curves and sheet bodies: nx_curve draws a real line / arc / circle / spline (through points);
   nx_surface skins curves — "through_curves" lofts 2..n sections (2 sections = the ruled surface
   NX refuses to build directly), "mesh" skins primary x cross curves that actually intersect,
   "extrude" sweeps a profile along a direction. Sheets have no volume, so judge them by area
   (returned in mm²) and bounding box; nx_export stl of a sheet-only part needs bodies set to
   every body, and the default export takes just one.
   Datums and sketches: nx_datum places a datum plane (origin + normal), a datum axis or a datum
   point; nx_sketch creates a real sketch on xy/xz/yz and draws rect / circle / line / polyline
   into it (horizontal, vertical and coincident constraints are added for you), then optionally
   extrudes it — cut: true subtracts, through: true runs across the whole body, symmetric centres
   it on the plane, and a cut with no height is taken through. Sketch coordinates are 2D on the
   plane (xy maps u,v to X,Y).
4. Check the numbers after each feature: nx_measure returns volume, area, mass, bounding box
   and centroid — compare them with what the shape should be before going on.
5. nx_save the part, then satisfy the deliverable:
   - nx_drawing (A3 sheet, four views placed automatically), nx_pdf;
   - nx_export to step / iges / stl (asynchronous: it waits, typically 20–40 s);
   - assemblies: nx_assembly_add (component names default to the file name in caps) /
     nx_assembly_move / nx_assembly_fix / nx_assembly_mate / nx_assembly_gap / nx_assembly_list;
     the solver only registers mates, so place components with nx_assembly_move;
     nx_assembly_bom rolls the components up by part file (quantity, volume, mass, optional CSV)
     and nx_assembly_clearance measures every component pair — NX has no interference query, so a
     negative or sub-tolerance minimum distance is what flags an overlap;
     nx_bom_table draws that same rollup as a ruled table on the sheet (NX has no tabular-note
     API, so the table is real sheet curves plus one drafting note per cell), and
     nx_batch_drawings makes an A3 sheet and a PDF for every part of the assembly in one call —
     per-part failures come back as rows instead of aborting the batch;
   - linear statics: nx_fea_spec then nx_fea_solve (NX Nastran, cantilever bar, PNG + STL);
     real geometry: nx_fea_solid solves the actual part (voxel hexahedra on the exported STL),
     takes several loads at once (force / pressure / moment / gravity / centrifugal), returns
     contour PNGs (deflection, von Mises), and runs a modal study with solver "modal".
     mesh "draped" (default) pulls the outer nodes onto the real surface: on a 120x80x20 plate it
     moves ~900 of 3750 boundary nodes, cuts the volume error from -1.31% to -0.93% and removes the
     step pattern on the walls, but holes keep their voxel-shaped rim — a fully conforming mesh
     needs the Simcenter mesher, which this session cannot start. Use "voxel" for a plain grid and
     a straight answer about the grid resolution.
6. Show the user something they can look at: present the STEP/STL/PDF/PNG the tools wrote.

Housekeeping that works: nx_layers lists layers in use, moves the bodies to another layer,
shows/hides a layer and sets the work layer; nx_refset lists, creates (from the part bodies)
and deletes reference sets.

Things NX refuses in this batch session — do not fight them, design around them:
- DXF/DWG export hangs the translator; other tools cover the formats.
- Native drafting dimensions cannot be created (auto-annotation only picks up existing PMI).
- A drawing sheet created from a template prints its views but not the template frame and title
  block; say so instead of promising a framed sheet.
- The assembly solver registers mates but does not move components: position with
  nx_assembly_move and verify with nx_assembly_gap.
- Assembly STEP export reads the saved file, so nx_save the assembly first; deeply nested
  assemblies whose components have no bodies may export nothing.
- nx_fillet refuses overlapping blends: round a few edges per call (edges take a point each).
- STEP export needs a non-empty layer mask; nx_export already sets it.
- Projection views print visible edges only: hidden lines are never generated in this headless
  session (the view style reports HiddenLine=True and setting it False changes the PDF by one
  byte, with no dash operators anywhere in the file). Use nx_section_view to expose internal
  geometry instead of promising dashed hidden lines.
- nx_detail_view commits a real DetailView whose placement lands where asked, but the zoomed
  content prints empty in this session (only the boundary circle and the label are drawn).
  Treat it as a placeholder; section views are the reliable way to show detail.
- Four surface builders exist in the API but die on commit in this batch session: Ruled
  ("The Tolerance Specification requires three numbers"), Swept ("Unable to approximate guide
  string"), ProjectCurve ("Invalid faces or planes to project to"), IntersectionCurve
  ("No objects for intersection curve"), and a revolved sheet ("Internal error: memory access
  violation" for any angle). Use nx_surface through_curves / mesh / extrude, and nx_revolve
  for solids of revolution, instead of promising these.
- Sketch dimensions cannot be created in this batch session (sketch.CreateDimension rejects every
  overload); size a sketch by its own geometry or drive the model with nx_expression afterwards.
- Motion (NX Motion / .sim) is not exposed by this plugin.
- Anything else: nx_python runs NXOpen code in the session, with NXOpen, NX, s and part in scope.
`;
