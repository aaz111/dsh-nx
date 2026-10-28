import { defineTool } from '@deepseek-ai/dsh-tools';

const POINT = {
  type: 'array',
  items: { type: 'number' },
  description: '[x, y, z] in mm',
};

// Sheet coordinates are plain [x, y] in millimetres, bottom-left origin.
const POINT_2D = {
  type: 'array',
  items: { type: 'number' },
  description: '[x, y] on the sheet in mm',
};

// Parameters travel as FLAT number arrays: the tool-call layer hands nested arrays through as
// strings, so every point list is written x0, y0, z0, x1, y1, z1 … and regrouped here.
const POINTS = {
  type: 'array',
  items: { type: 'number' },
  description: 'points as a flat list x0,y0,z0, x1,y1,z1, … in mm',
};

const PROFILE = {
  type: 'array',
  items: { type: 'number' },
  description: 'closed outline as a flat list a0,b0, a1,b1, … in mm, on the sketch plane',
};

const SELECTOR =
  'body selector: an index ("0", "-1" = the last body) or a body name; ' +
  'several values separated by commas (omit to mean the last body)';

const NX = 'Siemens NX';

function text(...lines) {
  return [{ type: 'text', text: lines.filter((line) => line !== undefined && line !== null && line !== '').join('\n') }];
}

function pretty(value) {
  const body = JSON.stringify(value, null, 2);
  return body.length > 7000 ? `${body.slice(0, 7000)}\n… (${body.length - 7000} more characters)` : body;
}

const round = (value, digits = 3) => (typeof value === 'number' ? Number(value.toFixed(digits)) : value);
const triple = (value) => (Array.isArray(value) ? value.map((item) => round(item, 3)).join(', ') : String(value));

/** A job file entry is either a path or a small object describing what was written. */
function describeFile(entry) {
  if (typeof entry === 'string') return entry;
  if (entry?.path) return `${entry.path}${entry.bytes ? ` (${entry.bytes} bytes)` : ''}`;
  return Object.entries(entry ?? {}).map(([key, value]) => `${key}=${value}`).join(', ');
}

/** Flat [a0,b0,a1,b1,…] (or "x,y,z; x,y,z", or already nested) → [[a0,b0],[a1,b1],…]. */
function group(values, size, what) {
  if (!Array.isArray(values) || values.length === 0) return values;
  if (Array.isArray(values[0])) return values;
  let flat = values;
  if (typeof values[0] === 'string') {
    flat = String(values.join(';'))
      .split(/[;|]/)
      .map((chunk) => chunk.trim())
      .filter(Boolean)
      .flatMap((chunk) => chunk.split(/[,\s]+/).filter(Boolean))
      .map(Number);
  }
  if (flat.length % size !== 0) {
    throw new Error(`${what} needs a multiple of ${size} numbers, got ${flat.length}`);
  }
  const out = [];
  for (let index = 0; index < flat.length; index += size) out.push(flat.slice(index, index + size));
  return out;
}

/** One string (or number list) per curve → [[[x,y,z],…], …] for the surface builders. */
function curveList(values, what) {
  if (!Array.isArray(values) || values.length === 0) return values;
  return values.map((item, index) => group(Array.isArray(item) ? item : [item], 3, `${what} ${index + 1}`));
}

/** Turn "0, 2" or "-1" or "Body01" into what nxworker's pick_bodies expects. */function selector(value) {
  if (value === undefined || value === null || value === '') return undefined;
  const raw = Array.isArray(value) ? value : String(value).split(',');
  const picked = [];
  for (const item of raw) {
    if (typeof item === 'number') {
      picked.push(item);
      continue;
    }
    const text = String(item).trim();
    if (!text) continue;
    picked.push(/^-?\d+$/.test(text) ? Number(text) : text);
  }
  return picked.length ? picked : undefined;
}

/** How a mate side names the geometry it constrains: a face on the component's prototype part. */
const FACE_SPEC = {
  type: 'object',
  description: 'precise face picker: {radius} | {diameter} | {z} | {index} | {area} | {point:[x,y,z]}',
  properties: {
    radius: { type: 'number' },
    diameter: { type: 'number' },
    z: { type: 'number' },
    index: { type: 'number' },
    area: { type: 'number' },
    point: { type: 'array', items: { type: 'number' } },
  },
  additionalProperties: false,
};

/** {component, face:[x,y,z], faceSpec, usesAxis} → {component, face:{…}, usesAxis} for nxworker. */
function mateSide(side = {}) {
  const face = side.faceSpec ?? (Array.isArray(side.face) ? { point: side.face } : {});
  const built = { component: side.component, face };
  if (side.usesAxis !== undefined) built.usesAxis = side.usesAxis;
  return built;
}

function bboxText(value) {
  if (!value || !value.bboxSize) return '';
  return `bbox    ${value.bboxSize.map((item) => round(item, 3)).join(' × ')} mm` +
    ` (min ${triple(value.bboxMin)} → max ${triple(value.bboxMax)})`;
}

function measureLines(value) {
  if (!value) return [];
  if (value.assembly) {
    return [
      `assembly with ${value.components} component(s), no body of its own`,
      value.componentNames?.length ? `components: ${value.componentNames.join(', ')}` : '',
    ];
  }
  return [
    `volume  ${round(value.volume, 4)} mm³`,
    `area    ${round(value.area, 4)} mm²`,
    `mass    ${round(value.mass, 6)} kg   density ${round(value.density, 10)} kg/mm³`,
    bboxText(value),
    `centroid ${triple(value.centroid)} mm`,
    value.bodies ? `bodies  ${value.bodies}` : '',
  ];
}

function featureLines(value) {
  if (!value) return [];
  const name = value.feature || '';
  const type = value.type || '';
  const head = name && type ? `${name}  (${type})` : (name || type || 'feature');
  return [head, ...measureLines(value.measure)];
}

/**
 * Build every nx_* tool. The bridge is the only side effect: each call starts the
 * background NX session on demand (about 25 s the first time) and then talks JSON.
 */
export function createNxTools(bridge) {
  const call = (command, args, timeoutMs) => bridge.request(command, args, { timeoutMs });

  const make = ({ name, description, parameters = {}, timeoutMs = 300_000, run, render }) => defineTool({
    name,
    description,
    parameters,
    timeoutMs,
    isConcurrencySafe: () => false,
    // The bridge answers with command-specific JSON, so each tool renders that
    // answer into the text the model reads and reports it as a string value.
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args = {}) {
      await bridge.ensure();
      const value = await run(args);
      const parts = (render ?? ((_args, result) => text(pretty(result))))(args, value);
      return parts.map((part) => part.text).join('\n');
    },
  });

  const tools = [];

  // ---------------------------------------------------------------- session
  tools.push(make({
    name: 'nx_status',
    description:
      `Health of the ${NX} bridge: whether the background NX session is running, its release, ` +
      'the parts open in it and the commands it serves. Call this first in an NX task.',
    timeoutMs: 180_000,
    run: async () => {
      const state = bridge.status();
      if (!state.running) {
        return {
          bridge: state,
          nx: null,
          note: 'no NX session is running; the first modelling call starts one and takes about 25 s',
        };
      }
      return { bridge: state, nx: await call('status', {}, 120_000) };
    },
    render: (_args, value) => text(
      value.bridge?.running
        ? `bridge running (pid ${value.bridge.pid ?? '?'}) at ${value.bridge.root}`
        : `bridge idle: ${value.note}`,
      value.nx ? `NX ${value.nx.fullRelease ?? value.nx.release}  batch=${value.nx.isBatch}  uptime ${value.nx.uptimeSeconds}s` : '',
      value.nx?.displayParts?.length ? `open parts: ${value.nx.displayParts.map((p) => p.leaf ?? p).join(', ')}` : '',
      value.nx?.hasPart ? 'a part is on the bridge side' : '',
      `NX home ${value.bridge?.nxHome ?? ''}`,
    ),
  }));

  tools.push(make({
    name: 'nx_session',
    description:
      'Manage the background NX session: action "status" reports it, "stop" shuts it down ' +
      '(the next tool call starts a fresh one), "restart" stops and immediately starts again.',
    parameters: {
      action: { type: 'string', required: true, description: 'status | stop | restart' },
    },
    timeoutMs: 400_000,
    run: async (args) => {
      const action = String(args.action ?? 'status').toLowerCase();
      if (action === 'stop') {
        await bridge.stop('nx_session stop');
        return { action, bridge: bridge.status() };
      }
      if (action === 'restart') {
        await bridge.restart();
        return { action, bridge: bridge.status(), nx: await call('status', {}, 120_000) };
      }
      return { action: 'status', bridge: bridge.status() };
    },
  }));

  // ---------------------------------------------------------------- documents
  tools.push(make({
    name: 'nx_open',
    description:
      'Open a part into the background NX session and make it the active working part: ' +
      '.prt (modelling, assembly, drawing), .step/.stp, .iges/.igs. Already-loaded parts are reused. ' +
      'Returns identity, units and the drawing sheets it carries.',
    parameters: {
      path: { type: 'string', required: true, description: 'absolute Windows path of the file to open' },
    },
    timeoutMs: 600_000,
    run: (args) => call('open', { path: args.path }, 540_000),
    render: (_args, value) => text(
      `opened ${value.leaf ?? '?'}${value.reused ? ' (already loaded)' : ''}`,
      `file ${value.fullPath ?? ''}`,
      `units ${value.units ?? '?'}`,
      value.sheets?.length ? `sheets ${value.sheets.map((s) => `${s.name} ${round(s.width)}×${round(s.height)}`).join(', ')}` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_new',
    description:
      'Create a new empty millimetre part with NX and make it the active working part. ' +
      'The path is the file to create (parent folders are made for you); NX refuses a path that is ' +
      'already loaded in the session, so pass overwrite: true to replace it.',
    parameters: {
      path: { type: 'string', required: true, description: 'absolute Windows path of the .prt to create' },
      overwrite: { type: 'boolean', description: 'replace an existing file at that path' },
    },
    timeoutMs: 400_000,
    run: (args) => call('new', { path: args.path, overwrite: Boolean(args.overwrite) }, 360_000),
    render: (_args, value) => text(
      `created ${value.created ?? value.fullPath ?? ''}`,
      `units ${value.units ?? '?'}`,
    ),
  }));

  tools.push(make({
    name: 'nx_info',
    description:
      'Describe the active NX part: leaf name, file, units, drawing sheets with their views and scale, ' +
      'and the standard modelling views available for putting views on a sheet.',
    timeoutMs: 180_000,
    run: () => call('info', {}, 150_000),
    render: (_args, value) => text(
      `part ${value.leaf ?? '?'}  (${value.fullPath ?? ''})`,
      `units ${value.units ?? '?'}`,
      value.sheets?.length
        ? `sheets:\n${value.sheets.map((sheet) => (
          `  ${sheet.name}${sheet.current ? ' *' : ''} ${round(sheet.width)}×${round(sheet.height)} mm ` +
          `scale ${sheet.scale?.map((v) => round(v, 4)).join(':')} projection ${sheet.projection} ` +
          `views ${sheet.views?.map((v) => v.name).join(', ') || 'none'}`
        )).join('\n')}`
        : 'sheets: none',
      value.modelingViews?.length ? `modelling views: ${value.modelingViews.join(', ')}` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_measure',
    description:
      'Measure the active NX part (or chosen bodies): volume mm³, surface area mm², mass kg, ' +
      'density, axis-aligned bounding box and centre of mass. Assemblies report their components instead.',
    parameters: {
      bodies: { type: 'string', description: SELECTOR },
    },
    timeoutMs: 240_000,
    run: (args) => call('measure', { bodies: selector(args.bodies) }, 200_000),
    render: (_args, value) => text(...measureLines(value)),
  }));

  tools.push(make({
    name: 'nx_save',
    description:
      'Save the active NX part. With path it is saved as that file (folders are created); ' +
      'without a path it is saved over its own file. Returns the new identity.',
    parameters: {
      path: { type: 'string', description: 'absolute Windows path of the .prt to save as' },
      overwrite: { type: 'boolean', description: 'replace an existing file at that path' },
    },
    timeoutMs: 300_000,
    run: (args) => call('save', { path: args.path, overwrite: Boolean(args.overwrite) }, 260_000),
    render: (_args, value) => text(
      `saved ${value.fullPath ?? ''}`,
      value.leaf ? `part ${value.leaf}` : '',
    ),
  }));

  // ---------------------------------------------------------------- modelling
  tools.push(make({
    name: 'nx_primitive',
    description:
      'Add a primitive solid to the active NX part: kind "block" (length × width × height from the ' +
      'at corner), "cylinder" (diameter/height along axis from the at base point), "cone" ' +
      '(baseDiameter → topDiameter) or "sphere" (diameter at the at centre). Sizes and positions are mm. ' +
      'Returns the feature and the part measurement afterwards.',
    parameters: {
      kind: { type: 'string', required: true, description: 'block | cylinder | cone | sphere' },
      at: { ...POINT, description: '[x, y, z] mm: block corner, cylinder/cone base centre, sphere centre' },
      length: { type: 'number', description: 'block size along X, mm (default 10)' },
      width: { type: 'number', description: 'block size along Y, mm (default 10)' },
      height: { type: 'number', description: 'block Z size / cylinder or cone height, mm (default 10)' },
      diameter: { type: 'number', description: 'cylinder or sphere diameter, mm' },
      radius: { type: 'number', description: 'cylinder or sphere radius, mm (instead of diameter)' },
      baseDiameter: { type: 'number', description: 'cone base diameter, mm' },
      baseRadius: { type: 'number', description: 'cone base radius, mm (instead of baseDiameter)' },
      topDiameter: { type: 'number', description: 'cone top diameter, mm (0 = pointed, default 0)' },
      axis: { ...POINT, description: 'cylinder/cone axis direction (default +Z)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('primitive', {
      kind: args.kind,
      at: args.at,
      length: args.length,
      width: args.width,
      height: args.height,
      diameter: args.diameter,
      radius: args.radius,
      baseDiameter: args.baseDiameter,
      baseRadius: args.baseRadius,
      topDiameter: args.topDiameter,
      axis: args.axis,
    }, 360_000),
    render: (_args, value) => text(...featureLines(value)),
  }));

  tools.push(make({
    name: 'nx_boolean',
    description:
      'Boolean-combine bodies of the active NX part: op "unite" (union), "subtract" (cut the tools ' +
      'out of the target) or "intersect". The result keeps the target body. Bodies are picked by ' +
      `index or name (${SELECTOR}).`,
    parameters: {
      op: { type: 'string', required: true, description: 'unite | subtract | intersect' },
      target: { type: 'string', description: 'body kept as the result (default: the last body)' },
      tools: { type: 'string', required: true, description: 'body/bodies consumed into the target, comma separated' },
    },
    timeoutMs: 400_000,
    run: (args) => call('boolean', {
      op: args.op,
      target: selector(args.target),
      tools: selector(args.tools),
    }, 360_000),
    render: (_args, value) => text(...featureLines(value)),
  }));

  tools.push(make({
    name: 'nx_fillet',
    description:
      'Round edges of the active NX part with one radius (mm). Pass edges as a point on each edge ' +
      '(flat x0,y0,z0, x1,y1,z1 …) or omit it to round every edge; bodies narrows which bodies are ' +
      'used. NX refuses blends that overlap — round fewer edges per call then.',
    parameters: {
      radius: { type: 'number', required: true, description: 'blend radius, mm' },
      edges: { ...POINTS, description: 'a point on each edge to blend; omit = all edges' },
      bodies: { type: 'string', description: SELECTOR },
    },
    timeoutMs: 400_000,
    run: (args) => call('fillet', {
      radius: args.radius,
      edges: args.edges && args.edges.length ? group(args.edges, 3, 'edges') : 'all',
      bodies: selector(args.bodies),
    }, 360_000),
    render: (_args, value) => text(...featureLines(value), value.edges ? `edges ${value.edges}` : ''),
  }));

  tools.push(make({
    name: 'nx_chamfer',
    description:
      'Chamfer edges of the active NX part with one equal offset (mm). Same edge/bodies arguments as ' +
      'nx_fillet. NX rejects edges that are already blended or curved; the tool retries with the ' +
      'straight edges only and reports how many edges it used.',
    parameters: {
      distance: { type: 'number', required: true, description: 'chamfer offset, mm' },
      edges: { ...POINTS, description: 'a point on each edge to chamfer; omit = all edges' },
      bodies: { type: 'string', description: SELECTOR },
    },
    timeoutMs: 400_000,
    run: (args) => call('chamfer', {
      distance: args.distance,
      edges: args.edges && args.edges.length ? group(args.edges, 3, 'edges') : 'all',
      bodies: selector(args.bodies),
    }, 360_000),
    render: (_args, value) => text(...featureLines(value), value.edges ? `edges ${value.edges}` : ''),
  }));

  tools.push(make({
    name: 'nx_extrude',
    description:
      'Extrude a closed 2D outline into a solid. profile is the outline as a flat list a0,b0, a1,b1 … mm; ' +
      'plane "xy" maps a→X b→Y (extrudes +Z), "xz" maps a→X b→Z (extrudes −Y), "yz" maps a→Y b→Z ' +
      '(extrudes +X); base offsets the sketch plane along its normal; draft tapers the walls ' +
      'by that many degrees (the moulded-wall / taper path — the drafting feature itself is refused ' +
      'in this batch session).',
    parameters: {
      profile: { ...PROFILE, required: true },
      height: { type: 'number', description: 'extrusion depth, mm (default 10)' },
      plane: { type: 'string', description: 'xy | xz | yz (default xy)' },
      base: { type: 'number', description: 'offset of the sketch plane along its normal, mm (default 0)' },
      draft: { type: 'number', description: 'taper the walls by this many degrees (0 = straight, default 0)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('extrude', {
      profile: group(args.profile, 2, 'profile'),
      height: args.height,
      plane: args.plane,
      base: args.base,
      draft: args.draft,
    }, 360_000),
    render: (_args, value) => text(...featureLines(value)),
  }));

  tools.push(make({
    name: 'nx_revolve',
    description:
      'Revolve a closed outline into a solid of revolution. profile is a flat list r0,h0, r1,h1 … mm — ' +
      'the first value of each pair is the radius from the axis, the second the distance along it; plane ' +
      '"xz" (default) maps r→X h→Z; angle in degrees (default 360); axis and axisPoint place the axis.',
    parameters: {
      profile: { ...PROFILE, required: true, description: 'closed r,h pairs (flat list) in mm' },
      angle: { type: 'number', description: 'sweep angle in degrees (default 360)' },
      plane: { type: 'string', description: 'sketch plane xy | xz | yz (default xz)' },
      base: { type: 'number', description: 'offset of the sketch plane, mm (default 0)' },
      axis: { ...POINT, description: 'axis direction (default +Z)' },
      axisPoint: { ...POINT, description: 'point the axis passes through (default origin)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('revolve', {
      profile: group(args.profile, 2, 'profile'),
      angle: args.angle,
      plane: args.plane,
      base: args.base,
      axis: args.axis,
      axisPoint: args.axisPoint,
    }, 360_000),
    render: (_args, value) => text(...featureLines(value)),
  }));

  tools.push(make({
    name: 'nx_hole',
    description:
      'Drill a hole at a point. Default: a cylinder of diameter (or radius) starting at the point ' +
      'and running along axis, subtracted from the target body. With native:true it commits a real ' +
      'NX Hole feature instead (parametric, sits in the feature tree as a hole, drilled into the ' +
      'flat face nearest the point along the axis). The native hole can be a simple bore or, with ' +
      'type, a counterbore (counterboreDiameter + counterboreDepth) or a countersink ' +
      '(countersinkDiameter + countersinkAngle).',
    parameters: {
      target: { type: 'string', description: 'body to cut (default: the last body)' },
      at: { ...POINT, required: true, description: '[x, y, z] mm where the hole starts (on the face)' },
      diameter: { type: 'number', description: 'hole diameter, mm' },
      radius: { type: 'number', description: 'hole radius, mm (instead of diameter)' },
      depth: { type: 'number', description: 'cutting length, mm (default 100)' },
      axis: { ...POINT, description: 'hole direction (default +Z)' },
      native: { type: 'boolean', description: 'commit a native Hole feature instead of a cut cylinder' },
      type: {
        type: 'string',
        description: 'native hole kind: simple (default) | counterbore | countersink',
      },
      counterboreDiameter: { type: 'number', description: 'counterbore: head diameter, mm' },
      counterboreDepth: { type: 'number', description: 'counterbore: head depth, mm (must be shallower than depth)' },
      countersinkDiameter: { type: 'number', description: 'countersink: head diameter, mm' },
      countersinkAngle: { type: 'number', description: 'countersink: included angle, degrees (default 90)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('hole', {
      target: selector(args.target),
      at: args.at,
      diameter: args.diameter,
      radius: args.radius,
      depth: args.depth,
      axis: args.axis,
      native: Boolean(args.native),
      type: args.type,
      counterboreDiameter: args.counterboreDiameter,
      counterboreDepth: args.counterboreDepth,
      countersinkDiameter: args.countersinkDiameter,
      countersinkAngle: args.countersinkAngle,
    }, 360_000),
    render: (_args, value) => text(...featureLines(value)),
  }));

  tools.push(make({
    name: 'nx_tube',
    description:
      'Sweep a pipe along a polyline (a real NX Tube feature): a solid rod with outerDiameter, or a ' +
      'hollow pipe when innerDiameter is given. Consecutive points become line segments. The tube is ' +
      'a separate body — unite it with nx_boolean when it has to join a solid.',
    parameters: {
      path: {
        type: 'array',
        items: POINT,
        required: true,
        description: 'points [x, y, z] along the path, mm (at least two)',
      },
      outerDiameter: { type: 'number', description: 'outer diameter, mm (default 10)' },
      diameter: { type: 'number', description: 'alias of outerDiameter' },
      innerDiameter: { type: 'number', description: 'bored diameter, mm (default 0 = solid rod)' },
      tolerance: {
        type: 'number',
        description: 'modelling tolerance, mm (default 0.1; the tighter default fails with "Tolerance error")',
      },
    },
    timeoutMs: 120_000,
    run: (args) => call('tube', {
      path: args.path,
      outerDiameter: args.outerDiameter,
      diameter: args.diameter,
      innerDiameter: args.innerDiameter,
      tolerance: args.tolerance,
    }, 100_000),
    render: (_args, value) => text(
      `tube Ø${value.outerDiameter}${value.innerDiameter > 0 ? `/${value.innerDiameter}` : ''} along ` +
      `${value.segments} segment(s): volume ${value.measure.volume} mm³`,
    ),
  }));

  tools.push(make({
    name: 'nx_csys',
    description:
      'Create a cartesian coordinate system from an origin plus X and Y direction vectors (Z follows ' +
      'from the right-hand rule). It lands in the part as a real datum coordinate system.',
    parameters: {
      origin: { ...POINT, description: 'origin [x, y, z] mm (default 0, 0, 0)' },
      xDirection: { ...POINT, description: 'X direction (default 1, 0, 0)' },
      yDirection: { ...POINT, description: 'Y direction (default 0, 1, 0)' },
    },
    timeoutMs: 60_000,
    run: (args) => call('csys', {
      origin: args.origin,
      xDirection: args.xDirection,
      yDirection: args.yDirection,
    }, 50_000),
    render: (_args, value) => text(
      `coordinate system ${value.csys} at ${value.origin.join(', ')} ` +
      `(X ${value.xDirection.join(', ')} · Y ${value.yDirection.join(', ')})`,
    ),
  }));

  tools.push(make({
    name: 'nx_rib',
    description:
      'Build a contour rib (筋): a polyline laid on a face, grown to a height and a width, then united ' +
      'into the target body. This is the moulded-rib feature — thinner than a boss and drafted by the ' +
      'face it grows from.',
    parameters: {
      path: {
        type: 'array',
        items: POINT,
        required: true,
        description: 'points of the rib curve, mm (at least two)',
      },
      face: { ...POINT, description: 'a point on the face the rib grows from' },
      faces: { type: 'array', items: POINT, description: 'several face points (instead of face)' },
      height: { type: 'number', description: 'rib height above the face, mm (default 8)' },
      width: { type: 'number', description: 'rib thickness, mm (default 3)' },
      target: { type: 'string', description: 'body selector (default: the last body)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('rib', {
      path: args.path,
      face: args.face,
      faces: args.faces,
      height: args.height,
      width: args.width,
      target: selector(args.target),
    }, 100_000),
    render: (_args, value) => text(
      `rib ${value.height} × ${value.width} mm over ${value.segments} segment(s), united into ` +
      `${value.united} body: volume ${value.measure.volume} mm³`,
    ),
  }));

  tools.push(make({
    name: 'nx_pattern_feature',
    description:
      'Pattern real FEATURES (a hole, a boss, a pad) instead of whole bodies: linear, countX × countY ' +
      'with pitchX / pitchY. Features are picked by index ("-1" = the last feature) or by exact name. ' +
      'Use nx_pattern for bodies.',
    parameters: {
      features: {
        type: 'array',
        items: { type: 'string' },
        description: 'feature indexes or names (default: the last feature)',
      },
      countX: { type: 'number', description: 'copies along X (default 2)' },
      countY: { type: 'number', description: 'copies along Y (default 1)' },
      pitchX: { type: 'number', description: 'X pitch, mm (default 20)' },
      pitchY: { type: 'number', description: 'Y pitch, mm (default 20)' },
    },
    timeoutMs: 180_000,
    run: (args) => call('pattern_feature', {
      features: args.features,
      countX: args.countX,
      countY: args.countY,
      pitchX: args.pitchX,
      pitchY: args.pitchY,
    }, 150_000),
    render: (_args, value) => text(
      `${value.instances} instance(s) from ${value.features} feature(s) at ` +
      `${value.pitchX}/${value.pitchY} mm: volume ${value.measure.volume} mm³`,
    ),
  }));

  tools.push(make({
    name: 'nx_motion_open',
    description:
      'Open a motion part (.sim, or a .prt carrying NX Motion data) and report what it contains: ' +
      'solver, solutions, links, joints, couplers and the remaining degrees of freedom.',
    parameters: {
      path: { type: 'string', description: 'absolute Windows path of a .sim / .prt (omit: the active part)' },
      solution: { type: 'string', description: 'solution name (default: the first solution)' },
    },
    timeoutMs: 180_000,
    run: (args) => call('motion_open', { path: args.path, solution: args.solution }, 150_000),
    render: (_args, v) => text(
      `motion ${v.part}: ${v.solver} · ${(v.links || []).length} link(s) · ` +
      `${(v.joints || []).length} joint(s) · DOF ${v.dof}`,
    ),
  }));

  tools.push(make({
    name: 'nx_motion_status',
    description: 'The same overview as nx_motion_open for the motion part that is already open.',
    parameters: {
      solution: { type: 'string', description: 'solution name (default: the first solution)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('motion_status', { solution: args.solution }, 100_000),
    render: (_args, v) => text(
      `${v.part}: ${v.solver} · solutions ${(v.solutions || []).join(', ')} · DOF ${v.dof}`,
    ),
  }));

  tools.push(make({
    name: 'nx_motion_solve',
    description:
      'Run the NX Motion solver for a solution (SolveNormalRunSolution) and report how many steps ' +
      'it produced and how long it took.',
    parameters: {
      path: { type: 'string', description: 'absolute Windows path of a .sim / .prt (omit: the active part)' },
      solution: { type: 'string', description: 'solution name (default: the first solution)' },
    },
    timeoutMs: 600_000,
    run: (args) => call('motion_solve', { path: args.path, solution: args.solution }, 590_000),
    render: (_args, v) => text(
      `solution ${v.solution} solved in ${v.solveSeconds} s over ${v.steps} step(s)`,
    ),
  }));

  tools.push(make({
    name: 'nx_motion_frames',
    description:
      'Sample every link transformation over the solved steps and write motion_frames.json / ' +
      'motion_frames.csv into workdir — the per-frame positions behind an animation.',
    parameters: {
      path: { type: 'string', description: 'absolute Windows path of a .sim / .prt (omit: the active part)' },
      solution: { type: 'string', description: 'solution name (default: the first solution)' },
      samples: { type: 'number', description: 'how many frames to sample, 2..501 (default 101)' },
      workdir: { type: 'string', description: 'folder for the output files' },
    },
    timeoutMs: 600_000,
    run: (args) => call('motion_frames', {
      path: args.path,
      solution: args.solution,
      samples: args.samples,
      workdir: args.workdir,
    }, 590_000),
    render: (_args, v) => text(
      `${(v.links || 0)} link(s) × ${v.totalSteps} step(s) → ${v.files && v.files.frames}`,
    ),
  }));

  tools.push(make({
    name: 'nx_motion_report',
    description:
      'Solve, sample the frames and render a motion report: motion_report.png (trajectory plot) plus ' +
      'motion_animation.gif, with the CSV as the underlying data.',
    parameters: {
      path: { type: 'string', description: 'absolute Windows path of a .sim / .prt (omit: the active part)' },
      solution: { type: 'string', description: 'solution name (default: the first solution)' },
      samples: { type: 'number', description: 'how many frames to sample, 2..501 (default 101)' },
      workdir: { type: 'string', description: 'folder for the report files' },
      subtitle: { type: 'array', items: { type: 'string' }, description: 'text lines under the title' },
    },
    timeoutMs: 900_000,
    run: (args) => call('motion_report', {
      path: args.path,
      solution: args.solution,
      samples: args.samples,
      workdir: args.workdir,
      subtitle: args.subtitle,
    }, 890_000),
    render: (_args, v) => text(
      `report: ${v.files && v.files.report} (${v.sizes && v.sizes.report} B) · ` +
      `animation: ${v.files && v.files.animation} (${v.sizes && v.sizes.animation} B)`,
    ),
  }));

  tools.push(make({
    name: 'nx_thread',
    description:
      'Cut a real NX Thread feature into a cylindrical face: the hole wall at `at` for an internal ' +
      'thread, or a boss/shaft wall with external:true. The size is taken from NX\'s own thread ' +
      'standards table (UGII/modeling_standards/NX_Thread_Standard.xml) — by default the row whose ' +
      'tap-drill (internal) or major diameter (external) matches the measured cylinder, or name it ' +
      'exactly with standard ("Metric Coarse", "Metric Fine", "GB193", "Inch UNC", …) and size ' +
      '("M12 x 1.75", "M12 x 1.25", "1/2-13"). type symbolic (default, cosmetic, no material removed) ' +
      'or detailed (real thread geometry); length caps the thread, default full.',
    parameters: {
      target: { type: 'string', description: 'body to thread (default: the last body)' },
      at: { ...POINT, required: true, description: '[x, y, z] mm at the thread mouth, on or in the hole/boss' },
      diameter: { type: 'number', description: 'diameter of the cylinder to thread, mm (default: the nearest one)' },
      radius: { type: 'number', description: 'radius of that cylinder, mm (instead of diameter)' },
      standard: { type: 'string', description: 'thread standard name, exact, e.g. "Metric Coarse"' },
      size: { type: 'string', description: 'thread size, exact, e.g. "M12 x 1.75"' },
      external: { type: 'boolean', description: 'thread a shaft/boss instead of a hole' },
      type: { type: 'string', enum: ['symbolic', 'detailed'], description: 'default symbolic' },
      length: { type: 'number', description: 'thread length mm (default: full depth)' },
      engage: { type: 'string', description: 'radial engage class: "0.75" (default), "0.5" or "Custom"' },
      handedness: { type: 'string', enum: ['right', 'left'], description: 'default right' },
    },
    timeoutMs: 400_000,
    run: (args) => call('thread', {
      target: selector(args.target),
      at: args.at,
      diameter: args.diameter,
      radius: args.radius,
      standard: args.standard,
      size: args.size,
      external: Boolean(args.external),
      type: args.type,
      length: args.length,
      engage: args.engage,
      handedness: args.handedness,
    }, 360_000),
    render: (_args, value) => text(
      `thread ${value.size} ${value.threadType} (${value.standard}, pitch ` +
      `${value.pitch.toFixed(3)} mm${value.pitchInch ? ` / ${value.pitchInch} in` : ''}, ` +
      `${value.external ? 'external' : 'internal'}, major ${value.majorDiameter.toFixed(2)} mm)`,
      ...featureLines(value),
    ),
  }));

  tools.push(make({
    name: 'nx_shell',
    description:
      'Hollow a body into a wall of the given thickness (mm, grows inward). openFaces lists the faces ' +
      'to remove — face indices from nx_faces, or a [x,y,z] point on each face; omit for a closed hollow. ' +
      'NX needs a tolerance (default 0.01 mm) for this feature.',
    parameters: {
      target: { type: 'string', description: 'body to hollow (default: the last body)' },
      thickness: { type: 'number', description: 'wall thickness, mm (default 1)' },
      tolerance: { type: 'number', description: 'shell tolerance, mm (default 0.01)' },
      openFaces: {
        ...POINTS,
        description: 'a point on each face to open (flat x,y,z list); omit = closed hollow',
      },
    },
    timeoutMs: 400_000,
    run: (args) => call('shell', {
      target: selector(args.target),
      thickness: args.thickness,
      tolerance: args.tolerance,
      openFaces: args.openFaces && args.openFaces.length
        ? group(args.openFaces, 3, 'openFaces')
        : undefined,
    }, 360_000),
    render: (_args, value) => text(...featureLines(value)),
  }));

  tools.push(make({
    name: 'nx_faces',
    description:
      'List the faces of a body with their index, type, area mm² and centre point — what nx_shell ' +
      'needs for openFaces, and how to tell which face a hole or boss is on.',
    parameters: {
      target: { type: 'string', description: 'body (default: the last body)' },
    },
    timeoutMs: 300_000,
    run: (args) => call('faces', { target: selector(args.target) }, 260_000),
    render: (_args, value) => text(
      `body ${value.body ?? '?'}  ${value.faceCount} face(s)`,
      ...(value.faces ?? []).map((face) => (
        `  #${face.index}  ${face.type ?? ''}  area ${round(face.area)} mm²  centre ${triple(face.cog)}`
      )),
    ),
  }));

  tools.push(make({
    name: 'nx_polygon',
    description:
      'Draw a closed polygon of straight curves on a principal plane (no solid) — useful before ' +
      'nx_extrude/nx_revolve, or to see an outline in the part.',
    parameters: {
      profile: { ...PROFILE, required: true },
      plane: { type: 'string', description: 'xy | xz | yz (default xy)' },
      base: { type: 'number', description: 'offset of the sketch plane, mm' },
    },
    timeoutMs: 300_000,
    run: (args) => call('polygon', {
      profile: group(args.profile, 2, 'profile'),
      plane: args.plane,
      base: args.base,
    }, 260_000),
    render: (_args, value) => text(`drew ${value.curves} curve(s)`),
  }));

  // ---------------------------------------------------------------- curves & surfaces
  tools.push(make({
    name: 'nx_curve',
    description:
      'Create real NX curves: "line" (2 points), "arc" (start, a point on the arc, end), "circle" ' +
      '(centre point + radius, axis default +Z) or "spline" (through every point, degree 3 by default, ' +
      'closed: true for a periodic loop). Reports the curve length.',
    parameters: {
      kind: { type: 'string', description: 'line (default) | arc | circle | spline' },
      points: { ...POINTS, required: true },
      radius: { type: 'number', description: 'circle: radius, mm' },
      diameter: { type: 'number', description: 'circle: diameter, mm (instead of radius)' },
      axis: { ...POINT, description: 'circle: plane normal [x,y,z] (default 0,0,1)' },
      degree: { type: 'number', description: 'spline: degree (default 3)' },
      closed: { type: 'boolean', description: 'spline: close the loop periodically' },
    },
    timeoutMs: 300_000,
    run: (args) => call('curve', {
      kind: args.kind ?? 'line',
      points: group(args.points, 3, 'points'),
      radius: args.radius,
      diameter: args.diameter,
      axis: args.axis,
      degree: args.degree,
      closed: args.closed,
    }, 260_000),
    render: (_args, value) => text(
      `${value.kind} ${value.type}  length ${round(value.length)} mm  (${value.points} point(s) given)`,
    ),
  }));

  tools.push(make({
    name: 'nx_surface',
    description:
      'Build a surface (sheet body) from curves through points. "through_curves" lofts through 2 or ' +
      'more sections — with exactly 2 sections it is the ruled surface NX 2512 refuses to build ' +
      'natively; "mesh" skins primary x cross curves that intersect each other; "extrude" sweeps one ' +
      'profile along a direction for a distance. Each curve is written "x0,y0,z0; x1,y1,z1; …".',
    parameters: {
      kind: { type: 'string', description: 'through_curves (default) | mesh | extrude' },
      sections: {
        type: 'array',
        items: { type: 'string' },
        description: 'through_curves: one string per section, "x0,y0,z0; x1,y1,z1; …"',
      },
      primary: {
        type: 'array',
        items: { type: 'string' },
        description: 'mesh: the primary curves (>=2), same string form',
      },
      cross: {
        type: 'array',
        items: { type: 'string' },
        description: 'mesh: the cross curves (>=2), same string form',
      },
      profile: { ...POINTS, description: 'extrude: the profile points' },
      direction: { ...POINT, description: 'extrude: direction (default 0,0,1)' },
      distance: { type: 'number', description: 'extrude: distance, mm (default 20)' },
      closed: { type: 'boolean', description: 'extrude: close the profile into a loop' },
      degree: { type: 'number', description: 'spline degree for the section curves (default 3)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('surface', {
      kind: args.kind ?? 'through_curves',
      sections: curveList(args.sections, 'section'),
      primary: curveList(args.primary, 'primary curve'),
      cross: curveList(args.cross, 'cross curve'),
      profile: args.profile && args.profile.length ? group(args.profile, 3, 'profile') : undefined,
      direction: args.direction,
      distance: args.distance,
      closed: args.closed,
      degree: args.degree,
    }, 360_000),
    render: (_args, value) => text(
      `${value.kind} ${value.type ?? 'surface'}  ${value.bodies ?? 0} sheet(s)  area ${round(value.area)} mm²`,
      ...featureLines(value),
    ),
  }));

  tools.push(make({
    name: 'nx_surface_sew',
    description:
      'Sew sheet bodies into one body — the sheet counterpart of a boolean unite, which NX refuses ' +
      'on sheets. Give the bodies with the body selector ("0,1", "all" or names); the first one is ' +
      'the target. Closed seams turn the result into a solid.',
    parameters: {
      bodies: { type: 'string', description: 'body selector, e.g. "0,1" or "all" (default: the last body)' },
      tolerance: { type: 'number', description: 'sew tolerance, mm (default 0.01)' },
    },
    timeoutMs: 240_000,
    run: (args) => call('surface_sew', { bodies: args.bodies, tolerance: args.tolerance }, 200_000),
    render: (_args, value) => text(
      `sew  ${value.sewn ?? 0} body(s) from ${value.inputs ?? 0}  area ${round(value.area)} mm²`,
      ...featureLines(value),
    ),
  }));

  tools.push(make({
    name: 'nx_thicken',
    description:
      'Thicken a sheet body into a solid: wall thickness grows along the face normal (reverse: true ' +
      'flips the side). secondOffset adds a wall on the other side too, so 2 + 2 gives a 4 mm wall ' +
      'centred on the sheet.',
    parameters: {
      bodies: { type: 'string', description: 'sheet body selector (default: the last body)' },
      thickness: { type: 'number', description: 'first offset, mm (default 1)' },
      secondOffset: { type: 'number', description: 'offset on the other side, mm (default 0 = one-sided)' },
      reverse: { type: 'boolean', description: 'grow to the other side of the sheet' },
      tolerance: { type: 'number', description: 'thicken tolerance, mm (default 0.01)' },
    },
    timeoutMs: 240_000,
    run: (args) => call('thicken', {
      bodies: args.bodies,
      thickness: args.thickness,
      second_offset: args.secondOffset,
      reverse: args.reverse,
      tolerance: args.tolerance,
    }, 200_000),
    render: (_args, value) => text(
      `thicken ${round(value.thickness)} mm → ${value.solids ?? 0} solid(s)  ` +
        `volume ${round(value.measure?.volume)} mm³`,
      ...featureLines(value),
    ),
  }));

  tools.push(make({
    name: 'nx_surface_trim_extend',
    description:
      'Trim or extend one sheet against another: mode "trim" cuts the target at the tool (until ' +
      'selected), "extend" runs both sides out by distance, "corner" makes a corner between them.',
    parameters: {
      target: { type: 'string', description: 'body/sheet to trim (default: the last body)' },
      tools: { type: 'string', description: 'trimming body/sheet selector ("0", name, "0,1")' },
      mode: { type: 'string', description: 'trim (default) | extend | corner' },
      distance: { type: 'number', description: 'extend: how far, mm' },
      tolerance: { type: 'number', description: 'tolerance, mm (default 0.01)' },
    },
    timeoutMs: 240_000,
    run: (args) => call('surface_trim_extend', {
      target: args.target,
      tools: args.tools,
      mode: args.mode ?? 'trim',
      distance: args.distance,
      tolerance: args.tolerance,
    }, 200_000),
    render: (_args, value) => text(
      `${value.mode ?? 'trim'}  ${value.bodies ?? 0} body(s)  area ${round(value.area)} mm²`,
      ...featureLines(value),
    ),
  }));

  tools.push(make({
    name: 'nx_surface_nside',
    description:
      'Patch a closed boundary loop with one N-sided surface (the holes a lofted surface leaves). ' +
      'Give 3 or more boundary curves in order, each "x0,y0,z0; x1,y1,z1; …"; trim: false lets the ' +
      'patch run past the boundary.',
    parameters: {
      curves: {
        type: 'array',
        items: { type: 'string' },
        description: 'boundary curves in loop order, "x0,y0,z0; x1,y1,z1; …" (at least 3)',
      },
      trim: { type: 'boolean', description: 'trim the patch to the boundary (default true)' },
      degree: { type: 'number', description: 'spline degree of each boundary curve (default 3)' },
      tolerance: { type: 'number', description: 'position tolerance, mm (default 0.01)' },
    },
    timeoutMs: 240_000,
    run: (args) => call('surface_nside', {
      curves: curveList(args.curves, 'boundary curve'),
      trim: args.trim,
      degree: args.degree,
      tolerance: args.tolerance,
    }, 200_000),
    render: (_args, value) => text(
      `n-sided patch from ${value.boundary ?? 0} curve(s)  area ${round(value.area)} mm²`,
      ...featureLines(value),
    ),
  }));

  // ---------------------------------------------------------------- drafting annotations
  tools.push(make({
    name: 'nx_gdt',
    description:
      'Put a GD&T feature control frame on the current drawing sheet: the geometric characteristic, ' +
      'its tolerance value and, optionally, the view it belongs to. Needs a sheet with views (nx_drawing).',
    parameters: {
      characteristic: {
        type: 'string',
        description: 'position | flatness | straightness | circularity | cylindricity | perpendicularity | ' +
          'parallelism | angularity | concentricity | symmetry | profile_line | profile_surface | ' +
          'circular_runout | total_runout (default position)',
      },
      tolerance: { type: 'string', description: 'the tolerance value shown in the frame, e.g. "0.05"' },
      at: { ...POINT, description: 'sheet placement [x, y] mm (default: a free spot)' },
      view: { type: 'string', description: 'view name to attach the frame to (default: free on the sheet)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('gdt', {
      characteristic: args.characteristic,
      tolerance: args.tolerance,
      at: args.at,
      view: args.view,
    }, 100_000),
    render: (_args, value) => text(
      `GD&T ${value.characteristic}${value.tolerance ? ` ${value.tolerance}` : ''} at (${value.at.join(', ')})`
        + (value.view ? ` on ${value.view}` : ''),
    ),
  }));

  tools.push(make({
    name: 'nx_balloon',
    description:
      'Add a numbered balloon (item tag) to the drawing sheet — a text label at a sheet point, ' +
      'optionally attached to a view.',
    parameters: {
      number: { type: 'string', description: 'the balloon number (default "1")' },
      text: {
        type: 'array',
        items: { type: 'string' },
        description: 'several lines instead of one number',
      },
      at: { ...POINT, description: 'sheet placement [x, y] mm (default: a free spot)' },
      size: { type: 'number', description: 'text height mm (default 5)' },
      view: { type: 'string', description: 'view name to attach the balloon to (default: free on the sheet)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('balloon', {
      number: args.number,
      text: args.text,
      at: args.at,
      size: args.size,
      view: args.view,
    }, 100_000),
    render: (_args, value) => text(
      `balloon ${value.text.join(' / ')} at (${value.at.join(', ')})` + (value.view ? ` on ${value.view}` : ''),
    ),
  }));

  tools.push(make({
    name: 'nx_surface_finish',
    description:
      'Put a surface finish (roughness) symbol on the drawing sheet: the Ra value and an optional ' +
      'process note, at a sheet point.',
    parameters: {
      value: { type: 'string', description: 'the roughness value, e.g. "3.2" (default "3.2")' },
      method: { type: 'string', description: 'process note shown with the symbol, e.g. "machined"' },
      at: { ...POINT, description: 'sheet placement [x, y] mm (default: a free spot)' },
      view: { type: 'string', description: 'view name to attach the symbol to (default: free on the sheet)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('surface_finish', {
      value: args.value,
      method: args.method,
      at: args.at,
      view: args.view,
    }, 100_000),
    render: (_args, value) => text(
      `surface finish Ra ${value.value}${value.method ? ` ${value.method}` : ''} at (${value.at.join(', ')})`,
    ),
  }));

  tools.push(make({
    name: 'nx_centerline',
    description:
      'Put a centre mark on a drafting view at a sheet point — the cross on a hole or a circle in ' +
      'the view. Give the view and the point on the sheet.',
    parameters: {
      at: { ...POINT, description: 'sheet point [x, y] mm (required)' },
      view: { type: 'string', description: 'the view to put the mark on (default: the base top/front view)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('centerline', { at: args.at, view: args.view }, 100_000),
    render: (_args, value) => text(`centre mark on ${value.view} at (${value.at.join(', ')})`),
  }));

  tools.push(make({
    name: 'nx_drawing_frame',
    description:
      'Draw a GB drawing frame and title block onto the current sheet as real curves and notes — ' +
      'the template frame does not print, this one does. Binding margin 25 mm on the left, other ' +
      'margins 10 mm (A0–A2) or 5 mm, title block 180 × 56 mm in the bottom-right corner with part ' +
      'name, material, scale, drawing number, designer, checker and date.',
    parameters: {
      partName: { type: 'string', description: 'part name for the title block (default: the part file name)' },
      drawingNo: { type: 'string', description: 'drawing number' },
      material: { type: 'string', description: 'material shown in the title block (default "45")' },
      designer: { type: 'string', description: 'designer name' },
      checker: { type: 'string', description: 'checker name' },
      company: { type: 'string', description: 'company / unit shown across the bottom row' },
      date: { type: 'string', description: 'date string (default: today)' },
      scale: { type: 'string', description: 'scale text (default: the sheet scale)' },
      binding: { type: 'number', description: 'left binding margin, mm (default 25)' },
      margin: { type: 'number', description: 'other margins, mm (default 10 for A0–A2, else 5)' },
      titleWidth: { type: 'number', description: 'title block width, mm (default 180)' },
      titleHeight: { type: 'number', description: 'title block height, mm (default 56)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('drawing_frame', args, 100_000),
    render: (_args, value) => text(
      `frame on ${value.sheet} (${value.size.join(' × ')} mm): ${value.curves} curves, ` +
      `${value.texts} texts, title block ${value.titleBlock.join(' × ')} mm`,
    ),
  }));

  // ---------------------------------------------------------------- synchronous modelling
  tools.push(make({
    name: 'nx_sync_face',
    description:
      'Change one face of a solid without a sketch (synchronous modelling): "move" translates it ' +
      'along a direction, "offset" pushes it along its own normal, "pull" drags it by a direction ' +
      'and a distance, "resize" changes the diameter of a cylindrical face (hole wall, boss, ' +
      'fillet). NX rebuilds the neighbouring faces to keep the body closed; the answer reports ' +
      'the volume before and after.',
    parameters: {
      op: { type: 'string', description: 'move (default) | offset | pull | resize' },
      face: { ...POINT, description: 'a point on the face to change, mm' },
      vector: { ...POINT, description: 'move / pull: direction (default 0,0,1)' },
      distance: { type: 'number', description: 'move / offset / pull: distance, mm' },
      diameter: { type: 'number', description: 'resize: the new diameter, mm (default: unchanged)' },
      body: { type: 'string', description: 'body selector (default: the last body)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('sync_face', args, 100_000),
    render: (_args, value) => text(
      `${value.op} on the face at ${value.face.join(', ')}: volume ${value.volumeBefore} → ` +
      `${value.measure.volume} mm³ (${value.volumeChange >= 0 ? '+' : ''}${value.volumeChange})`,
    ),
  }));

  tools.push(make({
    name: 'nx_sync_region',
    description:
      'Delete faces of a solid and let NX heal the gap (synchronous modelling) — the way to remove ' +
      'a blend, a small boss or a hole wall without a sketch. NX refuses when the remaining faces ' +
      'cannot close the area, and the error says so. (Face replacement is not exposed: it hangs ' +
      'the batch session.)',
    parameters: {
      face: { ...POINT, description: 'a point on a face to delete' },
      faces: { type: 'array', items: POINT, description: 'several points, one per face' },
      heal: { type: 'boolean', description: 'heal the gap (default true)' },
      body: { type: 'string', description: 'body selector (default: the last body)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('sync_region', args, 100_000),
    render: (_args, value) => text(
      `deleted ${value.faces} face(s): volume ${value.volumeBefore} → ` +
      `${value.measure.volume} mm³ (${value.volumeChange >= 0 ? '+' : ''}${value.volumeChange})`,
    ),
  }));

  // ---------------------------------------------------------------- datums & sketches
  tools.push(make({
    name: 'nx_datum',
    description:
      'Create a datum plane, datum axis or datum point the rest of the model can hang on: ' +
      '"plane" from an origin and a normal, "axis" from two points (or a point plus a direction and ' +
      'length), "point" at a coordinate. Datum planes are what a sketch can be attached to later.',
    parameters: {
      kind: { type: 'string', description: 'plane (default) | axis | point' },
      origin: { ...POINT, description: 'plane / point: the origin [x,y,z] mm (default 0,0,0)' },
      normal: { ...POINT, description: 'plane: the plane normal (default 0,0,1)' },
      start: { ...POINT, description: 'axis: start point (default 0,0,0)' },
      end: { ...POINT, description: 'axis: end point (instead of direction + length)' },
      direction: { ...POINT, description: 'axis: direction when no end point is given' },
      length: { type: 'number', description: 'axis: length along direction (default 100 mm)' },
    },
    timeoutMs: 120_000,
    run: (args) => call('datum', {
      kind: args.kind ?? 'plane',
      origin: args.origin,
      normal: args.normal,
      start: args.start,
      end: args.end,
      direction: args.direction,
      length: args.length,
    }, 100_000),
    render: (_args, value) => text(
      value.kind === 'plane'
        ? `datum plane ${value.datum} at (${value.origin.join(', ')}) normal (${value.normal.join(', ')})`
        : value.kind === 'axis'
          ? `datum axis ${value.datum} from (${value.start.join(', ')}) to (${value.end.join(', ')})`
          : `datum point ${value.datum} at (${value.at.join(', ')})`,
    ),
  }));

  tools.push(make({
    name: 'nx_sketch',
    description:
      'Create a real NX sketch on a principal plane and draw into it: "rect" (x0,y0 + width,height), ' +
      '"circle" (centre + radius, in sketch coordinates), "line" (from/to) or "polyline" (point list, ' +
      'closed by default). Horizontal / vertical and coincident constraints are added automatically. ' +
      'Pass height to extrude it straight into a solid (cut: true subtracts instead, through: true runs ' +
      'the extrusion across the whole body, symmetric centres it on the plane). Sketch coordinates are ' +
      '2D in the plane: xy maps (u,v) to X,Y; xz maps to X,Z; yz maps to Y,Z; base offsets the plane ' +
      'along its normal. A cut without height is taken through the body.',
    parameters: {
      plane: { type: 'string', description: 'xy (default) | xz | yz' },
      base: { type: 'number', description: 'offset of the sketch plane along its normal, mm' },
      shapes: {
        type: 'array',
        items: { type: 'object', additionalProperties: true },
        description:
          '{kind: rect|circle|line|polyline, x0, y0, width, height, x1, y1, center: [u,v], radius, ' +
          'from: [u,v], to: [u,v], points: [[u,v], …], closed}',
      },
      constrain: { type: 'boolean', description: 'add horizontal/vertical/coincident constraints (default true)' },
      height: { type: 'number', description: 'extrude the finished sketch by this much, mm' },
      extend: { type: 'number', description: 'start the extrusion this far along the normal (default 0)' },
      symmetric: { type: 'boolean', description: 'extrude symmetrically about the sketch plane' },
      cut: { type: 'boolean', description: 'subtract the extrusion instead of adding it' },
      through: { type: 'boolean', description: 'run the extrusion / cut across the whole body along the normal (a cut without height defaults to through)' },
    },
    timeoutMs: 300_000,
    run: (args) => call('sketch', {
      plane: args.plane ?? 'xy',
      base: args.base,
      shapes: args.shapes,
      constrain: args.constrain,
      height: args.height,
      extend: args.extend,
      symmetric: args.symmetric,
      cut: args.cut,
      through: args.through,
    }, 260_000),
    render: (_args, value) => text(
      `${value.sketch} on ${value.plane}${value.base ? ` (base ${round(value.base)} mm)` : ''}: ` +
      `${value.curves} curve(s), ${value.constraints} constraint(s)` +
      (value.extrude ? ` → ${value.extrude.feature || 'extrude'} ${round(value.extrude.height ?? 0)} mm` : ''),
      ...(value.extrude ? featureLines(value.extrude) : []),
    ),
  }));

  // ---------------------------------------------------------------- exchange
  tools.push(make({
    name: 'nx_export',
    description:
      'Export the active part to another format: "step", "iges" or "stl". The NX translator runs ' +
      'asynchronously, so this waits for the file (typically 20–40 s) and reports its size and how many ' +
      'solids it actually contained. Assemblies are exported from their on-disk file, so save first.',
    parameters: {
      path: { type: 'string', required: true, description: 'absolute Windows path of the file to write' },
      format: { type: 'string', description: 'step (default) | iges | stl' },
      bodies: { type: 'string', description: SELECTOR },
      layers: { type: 'string', description: 'layer mask handed to the translator (default "1-256")' },
      waitSeconds: { type: 'number', description: 'how long to wait for the translator (default 120)' },
    },
    timeoutMs: 900_000,
    run: (args) => call('export', {
      format: args.format ?? 'step',
      path: args.path,
      bodies: selector(args.bodies),
      layers: args.layers,
      waitSeconds: args.waitSeconds,
    }, 840_000),
    render: (_args, value) => text(
      `${String(value.format ?? '').toUpperCase()} ${value.exists ? 'written' : 'NOT written'}  ${value.bytes ?? 0} bytes`,
      `file ${value.path ?? ''}`,
      value.solidCounts ? `translator solids: ${JSON.stringify(value.solidCounts)}` : '',
      value.bodies !== undefined ? `bodies offered ${value.bodies}${value.accepted !== undefined ? `, accepted ${value.accepted}` : ''}` : '',
      value.assembly ? 'exported from the assembly file' : '',
    ),
  }));

  // ---------------------------------------------------------------- drafting
  tools.push(make({
    name: 'nx_drawing',
    description:
      'Create an engineering drawing of the active part: a sheet of the given size with the NX ' +
      'template frame and title block, then a base view plus the requested projected views, placed and ' +
      'scaled automatically. Returns the sheet, its scale and the views. The projected views print ' +
      'visible edges only — hidden lines are never generated in this batch session, so use ' +
      'nx_section_view when internal geometry has to be shown.',
    parameters: {
      size: { type: 'string', description: 'sheet size A0 | A1 | A2 | A3 | A4 (default A3)' },
      projection: { type: 'string', description: 'first (default) | third angle' },
      baseView: { type: 'string', description: 'base modelling view: TOP (default), FRONT, RIGHT, LEFT, BACK, BOTTOM, ISOMETRIC, TRIMETRIC' },
      views: {
        type: 'object',
        description: 'which views to place, e.g. {"front": true, "right": true, "isometric": true}',
        properties: {
          top: { type: 'boolean' },
          front: { type: 'boolean' },
          right: { type: 'boolean' },
          left: { type: 'boolean' },
          back: { type: 'boolean' },
          bottom: { type: 'boolean' },
          isometric: { type: 'boolean' },
          trimetric: { type: 'boolean' },
        },
        additionalProperties: false,
      },
      autoScale: { type: 'boolean', description: 'let NX scale the views to fit (default true)' },
      template: { type: 'boolean', description: 'use the NX sheet template with frame and title block (default true)' },
      sheetName: { type: 'string', description: 'sheet name when no template is used' },
    },
    timeoutMs: 600_000,
    run: (args) => call('drawing', {
      size: args.size,
      projection: args.projection,
      baseView: args.baseView,
      views: args.views,
      autoScale: args.autoScale,
      template: args.template,
      sheetName: args.sheetName,
    }, 540_000),
    render: (_args, value) => text(
      `sheet ${value.sheet?.name ?? '?'}  ${round(value.sheet?.width)}×${round(value.sheet?.height)} mm` +
      `  scale ${value.sheet?.scale?.map((v) => round(v, 4)).join(':')}`,
      `views ${value.sheet?.views?.map((v) => `${v.name} (${v.kind}, ${round(v.scale, 3)})`).join(', ') || 'none'}`,
    ),
  }));

  tools.push(make({
    name: 'nx_pdf',
    description:
      'Print the drawing sheets of the active part to a PDF (A3 template sheet → 420×297 mm). ' +
      'Takes 15–20 s. sheets names particular sheets; the default is the current sheet.',
    parameters: {
      path: { type: 'string', required: true, description: 'absolute Windows path of the PDF to write' },
      sheets: { type: 'array', items: { type: 'string' }, description: 'sheet names (default: the current sheet)' },
    },
    timeoutMs: 900_000,
    run: (args) => call('pdf', { path: args.path, sheets: args.sheets }, 840_000),
    render: (_args, value) => text(
      value.exists ? `PDF written  ${value.bytes} bytes` : 'PDF NOT written',
      `file ${value.path ?? ''}`,
      value.sheets?.length ? `sheets ${value.sheets.join(', ')}` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_annotate',
    description:
      'Run the drafting auto-annotation pass over every view of the active drawing (dimensions come ' +
      'from NX PMI, so a plain imported solid usually yields none).',
    timeoutMs: 400_000,
    run: () => call('annotate', {}, 360_000),
    render: (_args, value) => text(
      `annotated ${value.annotated} view(s): ${(value.views ?? []).join(', ')}`,
    ),
  }));

  tools.push(make({
    name: 'nx_section_view',
    description:
      'Add a section view (剖视图) to the current drawing sheet: cut the parent view along a line ' +
      'and place the section beside it, with the NX section label and cut arrows. start/end are the ' +
      'two ends of the cut line in the parent view\'s MODEL space (mm), e.g. [-10,30,10] to [110,30,10] ' +
      'cuts through y = 30. The new view is placed automatically unless at is given',
    parameters: {
      parent: { type: 'string', description: 'view to cut: name or part of a name (default: the Top or Front base view)' },
      start: { ...POINT, description: 'first end of the cut line, model space [x,y,z]' },
      end: { ...POINT, description: 'second end of the cut line, model space [x,y,z]' },
      type: { type: 'string', description: 'pointtopoint (default) | half | pointandangle | revolved | stepped' },
      at: { type: 'array', items: { type: 'number' }, description: 'sheet placement [x, y] mm (default: a free spot)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('section_view', {
      parent: args.parent,
      start: args.start,
      end: args.end,
      type: args.type,
      at: args.at,
    }, 360_000),
    render: (_args, value) => text(
      `section ${value.view?.name ?? '?'} of ${value.parent ?? '?'}  (${value.type ?? 'pointtopoint'})` +
      `  scale ${round(value.view?.scale, 3)}  objects ${value.view?.visible ?? 0}`,
      `placed at (${round(value.at?.[0])}, ${round(value.at?.[1])}) mm`,
      value.fellBack ? (value.note ?? `fell back from ${value.requested}`) : '',
    ),
  }));

  tools.push(make({
    name: 'nx_detail_view',
    description:
      'Add a detail view (局部放大图) to the current drawing sheet: circle a region of the parent ' +
      'view and show it enlarged. center [x,y] and radius are in the parent view\'s MODEL space (mm). ' +
      'The view lands where asked, but in this batch session its zoomed content prints empty (only ' +
      'the boundary circle and label appear) — treat it as a placeholder and use nx_section_view ' +
      'when the detail has to be readable.',
    parameters: {
      parent: { type: 'string', description: 'view to enlarge a part of: name or part of a name (default: Top or Front)' },
      center: { type: 'array', items: { type: 'number' }, description: 'centre of the region, model space [x,y]' },
      radius: { type: 'number', required: true, description: 'region radius in model mm' },
      scale: { type: 'number', description: 'enlargement ratio (default 2 = 2:1)' },
      shape: { type: 'string', description: 'circular (default) | rectangle' },
      at: { type: 'array', items: { type: 'number' }, description: 'sheet placement [x, y] mm (default: a free spot)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('detail_view', {
      parent: args.parent,
      center: args.center,
      radius: args.radius,
      scale: args.scale,
      shape: args.shape,
      at: args.at,
    }, 360_000),
    render: (_args, value) => text(
      `detail ${value.view?.name ?? '?'} of ${value.parent ?? '?'}  ${round(value.ratio, 2)}:1` +
      `  scale ${round(value.view?.scale, 3)}  objects ${value.view?.visible ?? 0}`,
      `placed at (${round(value.at?.[0])}, ${round(value.at?.[1])}) mm`,
      value.boundary ? `boundary (${round(value.boundary[0]?.[0])}, ${round(value.boundary[0]?.[1])}) r` +
        `${round((value.boundary[1]?.[0] ?? 0) - (value.boundary[0]?.[0] ?? 0), 2)} mm on the sheet` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_note',
    description:
      'Add a drafting note (技术要求 / any text block) to the current drawing sheet. Chinese ' +
      'characters need a CJK stroke font and get one automatically (chinesef); latin text uses the ' +
      'sheet default. at is the text origin in sheet mm',
    parameters: {
      text: { type: 'string', required: true, description: 'the note text; \\n starts a new line' },
      at: { type: 'array', items: { type: 'number' }, description: 'text origin on the sheet [x, y] mm (default 20,275)' },
      size: { type: 'number', description: 'text height mm (default 3.5)' },
      font: { type: 'string', description: 'NX character font: chinesef (default for CJK) | chineset | kanji | blockfont | Arial' },
    },
    timeoutMs: 400_000,
    run: (args) => call('note', {
      text: args.text,
      at: args.at,
      size: args.size,
      font: args.font,
    }, 360_000),
    render: (_args, value) => text(
      `note ${value.lines} line(s) at (${round(value.at?.[0])}, ${round(value.at?.[1])})  size ${round(value.size, 2)} mm` +
      `  font ${value.font ? `${value.font.name} #${value.font.index}` : 'sheet default'}`,
      (value.text ?? []).slice(0, 4).join(' | '),
    ),
  }));

  // ---------------------------------------------------------------- assemblies
  tools.push(make({
    name: 'nx_assembly_add',
    description:
      'Insert a saved part into the active assembly as a component placed at a point with an XYZ Euler ' +
      'rotation (degrees). The first component stays fixed in practice; use nx_assembly_fix to pin one.',
    parameters: {
      path: { type: 'string', required: true, description: 'absolute Windows path of the .prt to insert' },
      at: { ...POINT, description: 'component origin in assembly space, mm (default 0,0,0)' },
      rotate: { ...POINT, description: '[rx, ry, rz] XYZ Euler degrees (default 0,0,0)' },
      name: { type: 'string', description: 'component name to use' },
      referenceSet: { type: 'string', description: 'reference set name (default MODEL)' },
    },
    timeoutMs: 600_000,
    run: (args) => call('asm_add', {
      path: args.path,
      at: args.at,
      rotate: args.rotate,
      name: args.name,
      referenceSet: args.referenceSet,
    }, 540_000),
    render: (_args, value) => text(
      `component ${value.name ?? ''}  at ${triple(value.position)}  dof ${triple(value.dof)}`,
      value.components?.length ? `assembly now: ${value.components.map((c) => c.name).join(', ')}` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_assembly_move',
    description:
      'Move a component of the active assembly by a translation and/or an XYZ Euler rotation ' +
      '(degrees). This is the reliable way to position a component in batch — the assembly solver ' +
      'registers constraints but does not move geometry.',
    parameters: {
      component: { type: 'string', required: true, description: 'component name, index, or part of the name' },
      translate: { ...POINT, description: 'translation [dx, dy, dz] mm' },
      rotate: { ...POINT, description: 'rotation [rx, ry, rz] degrees' },
    },
    timeoutMs: 400_000,
    run: (args) => call('asm_move', {
      component: args.component,
      translate: args.translate,
      rotate: args.rotate,
    }, 360_000),
    render: (_args, value) => text(
      `moved ${value.component ?? ''}: ${triple(value.before)} → ${triple(value.after)}`,
      value.dof ? `dof ${triple(value.dof)}` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_assembly_fix',
    description:
      'Fix a component of the active assembly in place (the only constraint type NX really solves ' +
      'in batch): its degrees of freedom drop to zero.',
    parameters: {
      component: { type: 'string', required: true, description: 'component name, index, or part of the name' },
    },
    timeoutMs: 400_000,
    run: (args) => call('asm_fix', { component: args.component }, 360_000),
    render: (_args, value) => text(
      `fixed ${value.component ?? ''}`,
      `dof ${triple(value.dofBefore)} → ${triple(value.dofAfter)}`,
    ),
  }));

  tools.push(make({
    name: 'nx_assembly_mate',
    description:
      'Create a mating constraint between two components and try to solve it: type concentric, touch, ' +
      'distance, angle, parallel, perpendicular, align, center, fix, bond, fit, slider or cylindrical. ' +
      'Each side names a component and either a face point [x,y,z] (from nx_faces on the component part) ' +
      'or an axis. NX in batch registers the constraint and reports its status but does not move ' +
      'components — position them with nx_assembly_move.',
    parameters: {
      type: { type: 'string', required: true, description: 'concentric | touch | distance | angle | … (default concentric)' },
      a: {
        type: 'object',
        required: true,
        description: 'first side: {component, face} — face is a point on the face, optional faceSpec picks it '
          + 'precisely ({radius} | {diameter} | {z} | {index} | {area} | {point})',
        properties: {
          component: { type: 'string' },
          face: { type: 'array', items: { type: 'number' } },
          faceSpec: FACE_SPEC,
          usesAxis: { type: 'boolean' },
        },
        additionalProperties: false,
      },
      b: {
        type: 'object',
        required: true,
        description: 'second side: {component, face}',
        properties: {
          component: { type: 'string' },
          face: { type: 'array', items: { type: 'number' } },
          faceSpec: FACE_SPEC,
          usesAxis: { type: 'boolean' },
        },
        additionalProperties: false,
      },
      value: { type: 'number', description: 'distance (mm) or angle (degrees), for those types' },
    },
    timeoutMs: 400_000,
    run: (args) => call('asm_mate', {
      type: args.type ?? 'concentric',
      a: mateSide(args.a),
      b: mateSide(args.b),
      value: args.value,
    }, 360_000),
    render: (_args, value) => text(
      `mate ${value.mate}  ${value.a} ↔ ${value.b}`,
      `status ${value.status}  solved ${value.solved}`,
      value.solverError ? `solver: ${value.solverError}` : '',
      `dof ${triple(value.dofBefore)} → ${triple(value.dofAfter)}`,
    ),
  }));

  tools.push(make({
    name: 'nx_assembly_list',
    description:
      'List the components of the active assembly with their position, orientation and degrees of ' +
      'freedom, plus the constraints already in the assembly.',
    timeoutMs: 300_000,
    run: () => call('asm_list', {}, 260_000),
    render: (_args, value) => text(
      `assembly ${value.assembly ?? ''}  ${value.componentCount} component(s)`,
      ...(value.components ?? []).map((row) => (
        `  ${row.name}  at ${triple(row.position)}  dof ${triple(row.dof)}${row.suppressed ? '  (suppressed)' : ''}`
      )),
      (value.constraints ?? []).length
        ? `constraints: ${value.constraints.map((c) => `${c.type}(${c.status})`).join(', ')}`
        : 'constraints: none',
    ),
  }));

  tools.push(make({
    name: 'nx_assembly_gap',
    description:
      'Minimum distance between two components of the active assembly, in mm — how a fit is verified ' +
      '(0 means the faces touch, a positive value is the gap, a negative one an overlap).',
    parameters: {
      a: { type: 'string', required: true, description: 'first component' },
      b: { type: 'string', required: true, description: 'second component' },
    },
    timeoutMs: 300_000,
    run: (args) => call('asm_gap', { a: args.a, b: args.b }, 260_000),
    render: (_args, value) => text(
      `minimum distance ${value.minimum === null ? '?' : round(value.minimum, 4)} mm`,
      ...(value.pairs ?? []).map((row) => (
        row.error ? `  ${row.a}↔${row.b}: ${row.error}` : `  ${row.a} ↔ ${row.b}: ${round(row.distance, 4)} mm`
      )),
    ),
  }));

  tools.push(make({
    name: 'nx_component_pattern',
    description:
      'Pattern a component into several instances (组件阵列): linear along X/Y with a pitch per axis, ' +
      'or circular about an axis. The new instances are real components, so nx_assembly_bom counts ' +
      'them and nx_assembly_clearance checks them.',
    parameters: {
      component: { type: 'string', description: 'seed component: name or index (default: the first one)' },
      mode: { type: 'string', enum: ['linear', 'circular'], description: 'default linear' },
      countX: { type: 'number', description: 'linear: copies along X (default 1)' },
      pitchX: { type: 'number', description: 'linear: spacing along X, mm' },
      countY: { type: 'number', description: 'linear: copies along Y (default 1)' },
      pitchY: { type: 'number', description: 'linear: spacing along Y, mm' },
      count: { type: 'number', description: 'circular: total copies (default 6)' },
      angle: { type: 'number', description: 'circular: total sweep degrees (default 360)' },
      axis: { ...POINT, description: 'circular: rotation axis [x,y,z] (default 0,0,1)' },
      at: { ...POINT, description: 'circular: a point on the axis (default origin)' },
    },
    timeoutMs: 400_000,
    run: (args) => call('component_pattern', {
      component: args.component,
      mode: args.mode,
      countX: args.countX,
      pitchX: args.pitchX,
      countY: args.countY,
      pitchY: args.pitchY,
      count: args.count,
      angle: args.angle,
      axis: args.axis,
      at: args.at,
    }, 360_000),
    render: (_args, value) => text(
      value.mode === 'circular'
        ? `circular pattern: ${value.count}× over ${round(value.angle, 1)}° about ${(value.axis ?? []).join(',')}`
        : `linear pattern: ${value.countX}× X @ ${round(value.pitchX, 2)} mm, ` +
          `${value.countY}× Y @ ${round(value.pitchY, 2)} mm`,
      `feature ${value.feature ?? '?'}  assembly now ${value.componentCount} component(s)`,
      ...(value.components ?? []).slice(0, 12).map((row) => (
        `  ${row.name}  at ${(row.position ?? []).map((v) => round(v, 2)).join(', ')}`
      )),
    ),
  }));

  tools.push(make({
    name: 'nx_component_op',
    description:
      'Operate on a component of the active assembly: suppress / unsuppress it (all: true hits every ' +
      'component with the same name, e.g. all pattern instances), remove it, or replace it with another ' +
      'part on disk (substitute keeps the position).',
    parameters: {
      action: {
        type: 'string',
        enum: ['suppress', 'unsuppress', 'remove', 'replace'],
        required: true,
        description: 'what to do with the component',
      },
      component: { type: 'string', description: 'component: name or index (default: the first one)' },
      path: { type: 'string', description: 'replace: absolute path of the part to substitute in' },
      name: { type: 'string', description: 'replace: name for the new component' },
      all: { type: 'boolean', description: 'suppress/unsuppress every component with the same name' },
    },
    timeoutMs: 400_000,
    run: (args) => call('component_op', {
      action: args.action,
      component: args.component,
      path: args.path,
      name: args.name,
      all: args.all === true,
    }, 360_000),
    render: (_args, value) => text(
      `${value.action} ${value.target ?? value.removed ?? value.after?.name ?? ''}` +
        (value.action === 'replace' ? ` → ${value.part ?? '?'}` : '') +
        (value.count ? `  (${value.count} component(s))` : ''),
      value.action === 'replace'
        ? `now ${value.after?.name} from ${value.part}  assembly ${value.componentCount} component(s)`
        : value.action === 'remove'
          ? `assembly now ${value.componentCount} component(s)`
          : `${value.active ?? 0} active, ${value.suppressed ?? 0} suppressed`,
    ),
  }));

  tools.push(make({
    name: 'nx_explode',
    description:
      'Explode (爆炸图) or restore the assembly: every leaf component is moved along direction by ' +
      'distance + stagger × index, except the one named in keep. This moves real positions (so a ' +
      'drawing or export shows the exploded arrangement); the start positions are recorded on the part ' +
      'and restore: true puts them back.',
    parameters: {
      direction: { ...POINT, description: 'explosion direction [x,y,z] (default 0,0,1)' },
      distance: { type: 'number', description: 'first offset along direction, mm (default 40)' },
      stagger: { type: 'number', description: 'extra offset per further component, mm (default 0)' },
      keep: { type: 'string', description: 'component that stays in place (default: all move)' },
      restore: { type: 'boolean', description: 'undo a previous explosion instead' },
    },
    timeoutMs: 400_000,
    run: (args) => call('explode', {
      direction: args.direction,
      distance: args.distance,
      stagger: args.stagger,
      keep: args.keep,
      restore: args.restore === true,
    }, 360_000),
    render: (_args, value) => text(
      value.restored !== undefined
        ? `restored ${value.restored} component(s)`
        : `exploded ${value.exploded} component(s) along ${(value.direction ?? []).map((v) => round(v, 2)).join(',')}` +
          `  distance ${round(value.distance, 1)} mm  stagger ${round(value.stagger, 1)} mm` +
          (value.kept ? `  kept ${value.kept}` : ''),
      ...(value.moves ?? []).slice(0, 10).map((row) => (
        `  ${row.name}: ${(row.before ?? []).map((v) => round(v, 1)).join(',')} → ` +
        `${(row.after ?? []).map((v) => round(v, 1)).join(',')}`
      )),
    ),
  }));

  // ---------------------------------------------------------------- BOM / clearance / layers / reference sets
  tools.push(make({
    name: 'nx_assembly_bom',
    description:
      'Bill of materials for the active assembly: components rolled up by part file with quantity, ' +
      'volume and mass, plus a row per component. Suppressed components are left out (an NX parts list ' +
      'does the same); includeSuppressed: true keeps them. Pass csv to also write a UTF-8 spreadsheet CSV.',
    parameters: {
      csv: { type: 'string', description: 'optional CSV file to write, e.g. bom.csv (relative → NX output folder)' },
      fast: { type: 'boolean', description: 'skip volume/mass (names and quantities only)' },
      includeSuppressed: { type: 'boolean', description: 'also count suppressed components (default false)' },
    },
    timeoutMs: 300_000,
    run: (args) => call('asm_bom', {
      csv: args.csv,
      fast: args.fast === true,
      includeSuppressed: args.includeSuppressed === true,
    }, 260_000),
    render: (_args, value) => text(
      `assembly ${value.assembly ?? ''}  ${value.totals?.components ?? 0} component(s), ` +
        `${value.totals?.distinct ?? 0} distinct part(s)` +
        (value.totals?.suppressed ? `  +${value.totals.suppressed} suppressed` : ''),
      ...(value.groups ?? []).map((row) => (
        `  ${row.quantity}× ${row.file ?? row.name}  ${round(row.mass ?? 0, 4)} kg  ${round(row.volume ?? 0, 1)} mm³`
      )),
      `total ${round(value.totals?.mass ?? 0, 4)} kg  ${round(value.totals?.volume ?? 0, 1)} mm³`,
      value.csv ? `csv ${value.csv}  ${value.csvBytes} bytes` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_bom_table',
    description:
      'Draw the bill of materials as a ruled table on the current drawing sheet of the active assembly ' +
      '(real sheet curves plus one text per cell: 序号 / 零件号 / 名称 / 数量 / 单重 kg / 总重 kg, with a 合计 ' +
      'row). Uses the same rollup as nx_assembly_bom, so the numbers cannot disagree. Creates an A3 sheet ' +
      'with top/front/isometric views first when the part has none.',
    parameters: {
      at: { ...POINT_2D, description: 'bottom-left corner of the table on the sheet, mm (default 20,25)' },
      size: { type: 'string', description: 'sheet size when a sheet has to be created (default A3)' },
      rowHeight: { type: 'number', description: 'body row height in mm (default 7)' },
      textSize: { type: 'number', description: 'text height in mm (default 3)' },
      caption: { type: 'string', description: 'line above the table (default: assembly, item count, total mass)' },
      includeSuppressed: { type: 'boolean', description: 'also list suppressed components (default false)' },
      fast: { type: 'boolean', description: 'skip mass columns (names and quantities only)' },
      drawing: { type: 'boolean', description: 'false refuses to create a sheet automatically' },
    },
    timeoutMs: 300_000,
    run: (args) => call('bom_table', {
      at: args.at,
      size: args.size,
      rowHeight: args.rowHeight,
      textSize: args.textSize,
      caption: args.caption,
      includeSuppressed: args.includeSuppressed === true,
      fast: args.fast === true,
      drawing: args.drawing,
    }, 260_000),
    render: (_args, value) => text(
      `bom table on ${value.sheet ?? '?'}: ${value.rows} line(s), ${value.quantity} part(s), ` +
        `${round(value.mass, 4)} kg` +
        (value.suppressed ? `  +${value.suppressed} suppressed` : ''),
      `at ${(value.at ?? []).join(', ')} mm  ${round(value.table?.width, 1)}×` +
        `${round(value.table?.height, 1)} mm  ${value.lines} line(s), ${value.cells} cell(s)`,
      `columns ${(value.columns ?? []).join(' | ')}`,
    ),
  }));

  tools.push(make({
    name: 'nx_batch_drawings',
    description:
      'Batch drafting: for every part of the active assembly (or for the files in paths) open the part, ' +
      'make an A3 sheet with the requested views when it has none, and print it to PDF in outDir. ' +
      'Per-part failures are reported instead of aborting the run; reuse: false forces a fresh sheet.',
    parameters: {
      paths: {
        type: 'array',
        items: { type: 'string' },
        description: 'absolutely named .prt files; omit to take the components of the active assembly',
      },
      outDir: { type: 'string', description: 'folder for the PDFs (default: <NX output>/batch)' },
      size: { type: 'string', description: 'sheet size for parts that need one (default A3)' },
      projection: { type: 'string', description: 'first (default) or third angle' },
      reuse: { type: 'boolean', description: 'keep an existing sheet (default true); false adds a fresh one' },
      fromAssembly: { type: 'boolean', description: 'derive the part list from the assembly (default true)' },
    },
    timeoutMs: 1_800_000,
    run: (args) => call('batch_drawings', {
      paths: Array.isArray(args.paths) ? args.paths : undefined,
      outDir: args.outDir,
      size: args.size,
      projection: args.projection,
      reuse: args.reuse !== false,
      fromAssembly: args.fromAssembly !== false,
    }, 1_740_000),
    render: (_args, value) => text(
      `drew ${value.drawn} of ${value.parts} part(s) into ${value.outDir}`,
      ...(value.rows ?? []).slice(0, 15).map((row) => (
        row.error
          ? `  ${row.part ?? row.path}: ${row.error}`
          : `  ${row.part}  ${row.sheet?.name ?? ''}  ${row.sheet?.views?.length ?? 0} view(s)  ` +
            `${row.pdfBytes ?? 0} bytes  ${row.seconds}s${row.created ? '  new sheet' : ''}`
      )),
    ),
  }));

  tools.push(make({
    name: 'nx_assembly_clearance',
    description:
      'Gap matrix over the components of the active assembly: NX exposes no interference query, so every ' +
      'component pair is measured with the minimum distance (mm). Pairs below tolerance are reported as ' +
      'interference; pass threshold to only list pairs closer than that.',
    parameters: {
      components: {
        type: 'array',
        items: { type: 'string' },
        description: 'optional subset of components (names or indexes); default: all of them',
      },
      threshold: { type: 'number', description: 'only report pairs at or below this distance, mm' },
      tolerance: { type: 'number', description: 'distance below which a pair counts as interference, mm (default 0)' },
    },
    timeoutMs: 600_000,
    run: (args) => call('asm_clearance', {
      components: Array.isArray(args.components) ? args.components : undefined,
      threshold: args.threshold,
      tolerance: args.tolerance,
    }, 560_000),
    render: (_args, value) => text(
      `checked ${value.checked} of ${value.componentCount} component(s)  ${(value.pairs ?? []).length} pair(s)`,
      `minimum distance ${value.minimum === null || value.minimum === undefined ? '?' : round(value.minimum, 4)} mm`,
      ...(value.interference ?? []).map((row) => `  INTERFERENCE ${row.a} ↔ ${row.b}: ${round(row.distance, 4)} mm`),
      ...(value.pairs ?? []).slice(0, 24).map((row) => (
        row.error
          ? `  ${row.a} ↔ ${row.b}: ${row.error}`
          : `  ${row.a} ↔ ${row.b}: ${round(row.distance, 4)} mm`
      )),
      (value.pairs ?? []).length > 24 ? `… ${(value.pairs ?? []).length - 24} more pair(s)` : '',
    ),
  }));

  tools.push(make({
    name: 'nx_layers',
    description:
      'Layer bookkeeping for the active part: list the layers that hold objects (with their state and ' +
      'object count), move the part bodies to a layer, show/hide a layer, or set the work layer.',
    parameters: {
      action: {
        type: 'string',
        description: 'list (default) | move | show | hide | selectable | work',
      },
      layer: { type: 'number', description: 'layer number 1–256 (move/show/hide/selectable/work)' },
      bodies: { type: 'string', description: `optional ${SELECTOR} for action=move` },
    },
    timeoutMs: 300_000,
    run: (args) => call('layers', {
      action: args.action,
      layer: args.layer,
      bodies: selector(args.bodies),
    }, 260_000),
    render: (_args, value) => (
      value.layers
        ? text(
          `work layer ${value.workLayer}  ${value.layers.length} layer(s) in use`,
          ...value.layers.map((row) => `  layer ${row.layer}${row.work ? ' (work)' : ''}  ${row.state}  ${row.objects} object(s)`),
        )
        : text(
          `layer ${value.layer ?? value.workLayer}`,
          value.moved !== undefined ? `moved ${value.moved} body/bodies` : `state ${value.state ?? ''}`,
          value.objectsOnLayer !== undefined ? `objects on layer ${value.objectsOnLayer}` : '',
          value.objects !== undefined ? `objects ${value.objects}` : '',
        )
    ),
  }));

  tools.push(make({
    name: 'nx_refset',
    description:
      'Reference sets of the active part: list them, create one from the part bodies (name + members), ' +
      'or delete one by name. Reference sets are what an assembly component points at.',
    parameters: {
      action: { type: 'string', description: 'list (default) | create | delete' },
      name: { type: 'string', description: 'reference set name (create/delete)' },
      bodies: { type: 'string', description: `optional ${SELECTOR} for action=create (default: all bodies)` },
    },
    timeoutMs: 300_000,
    run: (args) => call('refset', {
      action: args.action,
      name: args.name,
      bodies: selector(args.bodies),
    }, 260_000),
    render: (_args, value) => text(
      value.deleted ? `deleted ${value.deleted}` : value.created ? `created ${value.created.name} (${value.bodies} body/bodies)` : '',
      `${(value.referenceSets ?? []).length} reference set(s)`,
      ...(value.referenceSets ?? []).map((row) => (
        `  ${row.name}  ${row.members === null || row.members === undefined ? '?' : row.members} member(s)`
      )),
    ),
  }));

  tools.push(make({
    name: 'nx_transform',
    description:
      'Move bodies of the active part: translate along a vector, rotate about an axis, or mirror across a ' +
      'plane. Commits a real NX feature (Move Body / Mirror Body) into the model, so it shows up in the ' +
      'part history and survives save.',
    parameters: {
      mode: { type: 'string', description: 'translate (default) | rotate | mirror' },
      bodies: { type: 'string', description: SELECTOR },
      direction: { type: 'array', items: { type: 'number' }, description: 'translate: direction dx,dy,dz (mm)' },
      dx: { type: 'number', description: 'translate: X offset, used when direction is omitted' },
      dy: { type: 'number', description: 'translate: Y offset, used when direction is omitted' },
      dz: { type: 'number', description: 'translate: Z offset, used when direction is omitted' },
      distance: { type: 'number', description: 'translate: distance along direction (default: vector length)' },
      axis: { type: 'array', items: { type: 'number' }, description: 'rotate: axis direction ax,ay,az (default 0,0,1)' },
      axisPoint: { type: 'array', items: { type: 'number' }, description: 'rotate: point on the axis (default origin)' },
      angle: { type: 'number', description: 'rotate: angle in degrees (default 90)' },
      planePoint: { type: 'array', items: { type: 'number' }, description: 'mirror: point on the mirror plane' },
      normal: { type: 'array', items: { type: 'number' }, description: 'mirror: plane normal (default 1,0,0)' },
      keepOriginal: { type: 'boolean', description: 'mirror: keep the source bodies (default true)' },
    },
    timeoutMs: 300_000,
    run: (args) => call('transform', {
      mode: args.mode,
      bodies: selector(args.bodies),
      direction: args.direction,
      dx: args.dx,
      dy: args.dy,
      dz: args.dz,
      distance: args.distance,
      axis: args.axis,
      axisPoint: args.axisPoint,
      angle: args.angle,
      planePoint: args.planePoint,
      normal: args.normal,
      keepOriginal: args.keepOriginal,
    }, 260_000),
    render: (_args, value) => {
      const detail = value.detail ?? {};
      const extra = value.mode === 'translate' ? `direction ${triple(detail.direction)} · ${round(detail.distance, 3)} mm`
        : value.mode === 'rotate' ? `${round(detail.angle, 3)}° about ${triple(detail.axis)} through ${triple(detail.axisPoint)}`
          : `across ${triple(detail.normal)} through ${triple(detail.planePoint)}`;
      return text(
        `${value.mode} → ${value.feature}  (${value.bodies} body/bodies)`,
        extra,
        value.partBodies !== undefined ? `part now has ${value.partBodies} body/bodies` : '',
        ...measureLines(value.measure),
      );
    },
  }));

  tools.push(make({
    name: 'nx_pattern',
    description:
      'Pattern bodies of the active part linearly (count + pitch along a direction) or circularly ' +
      '(count over an angle about an axis). Commits a Pattern Geometry feature.',
    parameters: {
      mode: { type: 'string', description: 'linear (default) | circular' },
      bodies: { type: 'string', description: SELECTOR },
      count: { type: 'number', description: 'total copies including the seed (default 3)' },
      pitch: { type: 'number', description: 'linear: spacing between copies (mm, default 50)' },
      direction: { type: 'array', items: { type: 'number' }, description: 'linear: direction dx,dy,dz (default 1,0,0)' },
      angle: { type: 'number', description: 'circular: total sweep in degrees (default 360)' },
      axis: { type: 'array', items: { type: 'number' }, description: 'circular: rotation axis (default 0,0,1)' },
      axisPoint: { type: 'array', items: { type: 'number' }, description: 'circular: point on the axis' },
    },
    timeoutMs: 300_000,
    run: (args) => call('pattern', {
      mode: args.mode,
      bodies: selector(args.bodies),
      count: args.count,
      pitch: args.pitch,
      direction: args.direction,
      angle: args.angle,
      axis: args.axis,
      axisPoint: args.axisPoint,
    }, 260_000),
    render: (_args, value) => {
      const detail = value.detail ?? {};
      const extra = value.mode === 'linear' ? `${detail.count} × ${round(detail.pitch, 3)} mm along ${triple(detail.direction)}`
        : `${detail.count} copies over ${round(detail.angle, 3)}° about ${triple(detail.axis)}`;
      return text(
        `${value.mode} pattern → ${value.feature}`,
        extra,
        `part now has ${value.bodies} body/bodies`,
        ...measureLines(value.measure),
      );
    },
  }));

  tools.push(make({
    name: 'nx_expression',
    description:
      'Read or drive the part history through NX expressions: list them, change a formula (the model ' +
      'rebuilds and the change is measured), create one, or delete one. This is how you resize a part ' +
      'parametrically instead of rebuilding it.',
    parameters: {
      action: { type: 'string', description: 'list (default) | set | create | delete' },
      name: { type: 'string', description: 'expression name, e.g. p0 or "plate_x"' },
      formula: { type: 'string', description: 'set/create: the new right-hand side, e.g. "160" or "plate_x * 2"' },
      value: { type: 'number', description: 'set: shorthand for a plain number (ignored when formula is given)' },
      filter: { type: 'string', description: 'list: only names containing this text' },
      limit: { type: 'number', description: 'list: stop after this many matches' },
    },
    timeoutMs: 120_000,
    run: (args) => call('expression', {
      action: args.action,
      name: args.name,
      formula: args.formula,
      value: args.value,
      filter: args.filter,
      limit: args.limit,
    }, 110_000),
    render: (_args, value) => {
      if (value.action === 'list') {
        const rows = value.expressions ?? [];
        const lines = rows.map((row) => `${row.name} = ${row.formula || row.value}   [${round(row.value, 4)}]`);
        return text(
          `${value.count} of ${value.total} expression(s)`,
          ...lines.slice(0, 40),
          rows.length > 40 ? `… ${rows.length - 40} more` : '',
        );
      }
      if (value.action === 'set') {
        const before = value.before ?? {};
        const after = value.after ?? {};
        return text(
          `${value.name}: ${before.formula || before.value} → ${after.formula || after.value}`,
          `value ${round(before.value, 4)} → ${round(after.value, 4)}`,
          'model rebuilt:',
          ...measureLines(value.measureAfter),
        );
      }
      if (value.action === 'create') {
        return text(`created ${value.name} = ${value.formula}`, `value ${round(value.value, 4)}`);
      }
      if (value.action === 'delete') {
        return text(`deleted ${value.name}`);
      }
      return text(`${value.action} done`);
    },
  }));

  // ---------------------------------------------------------------- FEA
  tools.push(make({
    name: 'nx_fea_spec',
    description:
      'Propose a linear-statics study for the active part: its bounding box becomes a cantilever bar ' +
      '(length along X, fixed at the -X end, loaded at the +X end) with a structured hexahedral mesh, ' +
      'E = 210 GPa and ν = 0.3. Review the numbers, then run nx_fea_solve with the same object.',
    parameters: {
      bodies: { type: 'string', description: SELECTOR },
      elementSize: { type: 'number', description: 'target element size, mm (default: longest extent / 30)' },
      load: { type: 'number', description: 'total load in N (default 200)' },
      direction: { type: 'string', description: 'load direction x | y | z (default z)' },
      material: {
        type: 'object',
        description: 'material constants: {"E": 210000 (MPa), "nu": 0.3}',
        properties: { E: { type: 'number' }, nu: { type: 'number' } },
        additionalProperties: false,
      },
    },
    timeoutMs: 300_000,
    run: (args) => call('cae_part', {
      bodies: selector(args.bodies),
      elementSize: args.elementSize,
      load: args.load,
      direction: args.direction,
      material: args.material,
    }, 260_000),
    render: (_args, value) => {
      const spec = value.spec ?? {};
      return text(
        `bar ${spec.length} × ${spec.width} × ${spec.height} mm  mesh ${spec.nx}×${spec.ny}×${spec.nz}`,
        `load ${spec.load} N along ${spec.direction}, E ${spec.material?.E} MPa, ν ${spec.material?.nu}`,
        `python ${value.python ?? ''}`,
      );
    },
  }));

  tools.push(make({
    name: 'nx_fea_solve',
    description:
      'Run a linear static analysis with NX Nastran for a cantilever bar: builds a hexahedral mesh ' +
      '(SOL 101, fixed -X face, load spread over the +X face), solves with nastran.exe (about 30 s), and ' +
      'writes a deformation STL, a result PNG report, an SVG and a result.json. Compare the tip deflection ' +
      'with δ = FL³/3EI to judge the model. Pass the spec from nx_fea_spec, or just length/width/height.',
    parameters: {
      workdir: { type: 'string', description: 'folder for the job files (default: a timestamped folder under the NX output dir)' },
      spec: {
        type: 'object',
        description: 'study spec from nx_fea_spec',
        properties: {},
        additionalProperties: false,
      },
      length: { type: 'number', description: 'bar length along X, mm (default: the part bounding box)' },
      width: { type: 'number', description: 'bar width along Y, mm' },
      height: { type: 'number', description: 'bar height along Z, mm' },
      nx: { type: 'number', description: 'elements along X' },
      ny: { type: 'number', description: 'elements along Y' },
      nz: { type: 'number', description: 'elements along Z' },
      load: { type: 'number', description: 'total load, N' },
      direction: { type: 'string', description: 'load direction x | y | z' },
      material: {
        type: 'object',
        description: '{"E": MPa, "nu": dimensionless}',
        properties: { E: { type: 'number' }, nu: { type: 'number' } },
        additionalProperties: false,
      },
    },
    timeoutMs: 1_500_000,
    run: (args) => call('cae_solve', {
      workdir: args.workdir,
      spec: args.spec,
      length: args.length,
      width: args.width,
      height: args.height,
      nx: args.nx,
      ny: args.ny,
      nz: args.nz,
      load: args.load,
      direction: args.direction,
      material: args.material,
    }, 1_440_000),
    render: (_args, value) => {
      const results = value.results ?? {};
      const mesh = value.mesh ?? {};
      return text(
        `solved in ${value.wallSeconds}s  (${value.solver?.name ?? 'NX Nastran'})`,
        `mesh ${mesh.nodes ?? '?'} nodes / ${mesh.elements ?? '?'} elements`,
        `tip deflection ${results.tipDeflection !== undefined ? round(results.tipDeflection, 5) : '?'} mm` +
        `  (analytic ${results.analyticDeflection !== undefined ? round(results.analyticDeflection, 5) : '?'} mm)`,
        `max von Mises ${results.maxVonMises !== undefined ? round(results.maxVonMises, 3) : '?'} MPa` +
        `  (analytic ${results.analyticStress !== undefined ? round(results.analyticStress, 3) : '?'} MPa)`,
        `folder ${value.workdir ?? ''}`,
        value.files
          ? `files\n${Object.entries(value.files)
            .filter(([, entry]) => entry)
            .map(([kind, entry]) => `  ${kind}: ${describeFile(entry)}`)
            .join('\n')}`
          : '',
      );
    },
  }));

  tools.push(make({
    name: 'nx_fea_solid',
    description:
      'Linear FE on the REAL geometry (not a box proxy): the active part is exported to STL, meshed into ' +
      'CHEXA8 elements and solved by NX Nastran. mesh "draped" (default) pushes the boundary nodes onto the ' +
      'real surface so holes and fillets are no longer a staircase; mesh "voxel" keeps the plain grid. ' +
      'Faces are named by side (-x/+x/-y/+y/-z/+z). Loads: force (direction + value N), pressure (MPa), ' +
      'moment (axis + value Nmm), gravity, centrifugal (rpm + axis). solver "statics" gives maxDisplacement ' +
      'mm and maxVonMises MPa plus contour pictures (deflection + von Mises); solver "modal" returns the ' +
      'first modes in Hz (SOL 103). Element size defaults to the largest extent / 40.',
    parameters: {
      elementSize: { type: 'number', description: 'element size, mm (default: largest extent / 40)' },
      mesh: { type: 'string', description: 'draped (default, boundary fitted) | voxel (plain grid)' },
      tolerance: { type: 'number', description: 'STL chordal tolerance of the source mesh, mm (default 0.15)' },
      material: {
        type: 'object',
        description: '{"E": MPa, "nu": dimensionless, "rho": t/mm3} -- rho drives gravity and centrifugal',
        properties: { E: { type: 'number' }, nu: { type: 'number' }, rho: { type: 'number' } },
        additionalProperties: false,
      },
      fix: {
        type: 'array',
        description: 'fixed sides, e.g. ["-x"] or ["-x", "-y"]',
        items: { type: 'string' },
      },
      loads: {
        type: 'array',
        description:
          'load list, e.g. [{"type":"force","face":"+z","direction":[0,0,-1],"value":1000}, ' +
          '{"type":"pressure","face":"+z","value":0.5}, ' +
          '{"type":"moment","face":"+x","axis":[0,1,0],"value":10000}, ' +
          '{"type":"gravity","direction":[0,0,-1]}, ' +
          '{"type":"centrifugal","rpm":3000,"axis":[0,0,1]}]',
        items: { type: 'object', additionalProperties: true },
      },
      load: { type: 'number', description: 'shortcut for one force: total N on the +z face, downward' },
      face: { type: 'string', description: 'shortcut force: the face it acts on (default +z)' },
      direction: {
        type: 'array',
        description: 'shortcut force: direction (default [0,0,-1])',
        items: { type: 'number' },
      },
      solver: { type: 'string', description: 'statics (default) | modal' },
      modes: { type: 'number', description: 'modal: how many frequencies (default 6)' },
      workdir: { type: 'string', description: 'folder for the job files (default: timestamped folder under the NX output dir)' },
    },
    timeoutMs: 1_800_000,
    run: (args) => call('cae_solid', {
      elementSize: args.elementSize,
      mesh: args.mesh,
      tolerance: args.tolerance,
      material: args.material,
      fix: args.fix,
      loads: args.loads,
      load: args.load,
      face: args.face,
      direction: args.direction,
      solver: args.solver,
      modes: args.modes,
      workdir: args.workdir,
    }, 1_740_000),
    render: (args, value) => {
      const mesh = value.mesh ?? {};
      const results = value.results ?? {};
      const isModal = args.solver === 'modal' || Array.isArray(results.frequencies);
      const loadLines = (mesh.loads ?? []).map((entry) => {
        if (entry.load === 'centrifugal') {
          return `  centrifugal ${entry.rpm} rpm about ${JSON.stringify(entry.axis)} / total ${round(entry.totalForce, 1)} N`;
        }
        if (entry.load === 'pressure') {
          return `  pressure ${entry.value} MPa on ${entry.face} over ${round(entry.area, 1)} mm2 / ${round(entry.totalForce, 1)} N`;
        }
        if (entry.load === 'moment') {
          return `  moment ${entry.value} Nmm about ${JSON.stringify(entry.axis)} on ${entry.face}` +
            ` / couple ${round(entry.coupleForce, 1)} N at arm ${round(entry.arm, 2)} mm`;
        }
        if (entry.load === 'gravity') {
          return `  gravity along ${JSON.stringify(entry.direction)} / ${round(entry.totalForce, 1)} N`;
        }
        return `  force ${entry.value} N on ${entry.face} along ${JSON.stringify(entry.direction)}` +
          ` / ${entry.nodes ?? '?'} nodes`;
      });
      const reaction = results.reaction;
      const reactionLine = Array.isArray(reaction)
        ? `reaction ${JSON.stringify(reaction.map((v) => round(v, 1)))} N (should cancel the applied load)`
        : '';
      const fileLine = value.files
        ? Object.entries(value.files)
          .map(([kind, entry]) => (typeof entry === 'string'
            ? `${kind}: ${describeFile(entry)}`
            : `${kind}: ${JSON.stringify(entry)}`))
          .join(', ')
        : '';
      const drape = mesh.drape ?? {};
      const drapeLine = drape.draped
        ? `  ${drape.moved} of ${drape.skinNodes} boundary nodes pulled onto the surface` +
          (drape.errorAfter !== undefined
            ? ` / volume ${round(drape.volumeAfter, 1)} vs CAD ${round(drape.volumeTarget, 1)} mm3 (was ${drape.errorBefore >= 0 ? '+' : ''}${drape.errorBefore}%, now ${drape.errorAfter >= 0 ? '+' : ''}${drape.errorAfter}%)`
            : ` / volume ${round(drape.volumeAfter, 1)} mm3 (voxel ${round(drape.volumeBefore, 1)})`)
        : '';
      const contours = results.contours
        ? `contours ${Object.entries(results.contours).map(([kind, file]) => `${kind}: ${file}`).join(', ')}`
        : '';
      return text(
        `solid FE in ${value.wallSeconds}s (NX Nastran)`,
        `mesh ${mesh.nodes ?? '?'} nodes / ${mesh.elements ?? '?'} elements (grid ${JSON.stringify(mesh.grid ?? [])}, h ${mesh.elementSize ?? '?'} mm, ${drape.draped ? 'draped' : 'voxel'})`,
        drapeLine,
        `fixed ${mesh.fixedNodes ?? '?'} nodes, ${mesh.loadedNodes ?? '?'} loaded`,
        ...loadLines,
        isModal
          ? `first ${results.firstFrequency !== undefined ? round(results.firstFrequency, 1) : '?'} Hz` +
            ` of ${results.modes ?? '?'} mode(s): ${(results.frequencies ?? []).map((f) => round(f, 1)).join(', ')} Hz`
          : `max deflection ${results.maxDisplacement !== undefined ? round(results.maxDisplacement, 5) : '?'} mm` +
            ` / max von Mises ${results.maxVonMises !== undefined ? round(results.maxVonMises, 3) : '?'} MPa`,
        reactionLine,
        contours,
        `folder ${value.workdir ?? ''}`,
        fileLine ? `files ${fileLine}` : '',
      );
    },
  }));

  // ---------------------------------------------------------------- escape hatch
  tools.push(make({
    name: 'nx_python',
    description:
      'Escape hatch: run NXOpen Python inside the live NX session and return what it printed. ' +
      'The globals NXOpen, NX, s (Session), part (active part) and w() (print into the result) are ' +
      'available; assign part = <some part> to change the active part. Use only for what the other ' +
      'tools do not cover.',
    parameters: {
      code: { type: 'string', required: true, description: 'Python source (NXOpen) to execute' },
    },
    timeoutMs: 900_000,
    run: (args) => call('exec', { code: args.code }, 840_000),
    render: (_args, value) => text((value.out ?? []).join('\n') || '(no output)'),
  }));

  return tools;
}
