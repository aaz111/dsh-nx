/**
 * Host half of the dsh-nx sidebar panel.
 *
 * The browser cannot talk to the NX bridge directly: the bridge is a file-based
 * request/response channel owned by this process. So this module publishes two
 * plain HTTP routes on the host's web server and the client panel polls them:
 *
 *   GET  /dsh-nx/state    bridge status, open parts, recent artefacts
 *   POST /dsh-nx/action   run one whitelisted bridge command
 *
 * Both are read-mostly: the state route never starts NX (a cold start is ~25 s
 * and a panel opening is not a reason to pay it), it only reports what the
 * bridge already knows. `?probe=1` asks NX for a live answer when the panel
 * explicitly refreshes.
 */

import { spawn } from 'node:child_process'
import { copyFile, mkdir, readdir, readFile, stat } from 'node:fs/promises'
import { basename, dirname, extname, join, resolve } from 'node:path'

/** Commands the panel may fire. Motion and raw `exec` stay out on purpose. */
const PANEL_COMMANDS = new Set([
  'status',
  'open',
  'info',
  'measure',
  'save',
  'export',
  'drawing',
  'pdf',
  'annotate',
  'faces',
  'asm_list',
  'asm_bom',
  'asm_clearance',
  'layers',
  'refset',
  // Drafting and assembly paperwork: the buttons that actually hand the user a file.
  'bom_table',
  'batch_drawings',
  'section_view',
  'note',
  'explode',
])

/** Artefact extensions worth listing in the panel. */
const ARTEFACT = /\.(prt|step|stp|iges|igs|stl|pdf|csv|png|svg|json|bdf|f06)$/i

/** Formats the panel can turn into a mesh preview. */
const PREVIEWABLE = /\.(prt|step|stp|iges|igs|stl)$/i

const MAX_BODY = 64 * 1024

function send(res, status, payload) {
  const body = typeof payload === 'string' ? payload : JSON.stringify(payload)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(body)
}

async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY) throw new Error('request body too large')
    chunks.push(chunk)
  }
  if (!size) return {}
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

/** Folders the artefact scan skips: caches and scratch, never deliverables. */
const SKIP_DIRS = new Set(['preview', '__pycache__', 'node_modules', 'fe', 'fe2', 'cae', 'report'])

/**
 * Newest artefacts under `base`, at most `depth` directories down. Missing
 * directories are not an error: a fresh install has produced nothing yet.
 */
async function recentArtefacts(base, limit = 12, depth = 3) {
  const found = []
  async function walk(dir, level) {
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      const full = join(dir, entry.name)
      if (entry.isDirectory()) {
        if (level < depth && !entry.name.startsWith('.') && !SKIP_DIRS.has(entry.name)) {
          await walk(full, level + 1)
        }
        continue
      }
      // Scratch files (thumbnail renders, probe output) start with an underscore.
      if (entry.name.startsWith('_')) continue
      if (!ARTEFACT.test(entry.name)) continue
      try {
        const info = await stat(full)
        found.push({ path: full, name: entry.name, bytes: info.size, modified: info.mtimeMs })
      } catch {
        // raced with a writer; skip it
      }
    }
  }
  await walk(base, 0)
  found.sort((a, b) => b.modified - a.modified)
  return found.slice(0, limit)
}

/**
 * Register the panel routes.
 *
 * @param {object} options
 * @param {object} options.webServer  host `ctx.webServer` (the browser carrier)
 * @param {object} options.bridge  the NxBridge instance the tools share
 * @param {string} options.outputDir  folder the panel lists artefacts from
 * @returns {() => void} disposer removing both routes
 */
export function registerPanelRoutes({ webServer, bridge, outputDir }) {
  const lastActions = []

  const routes = []

  routes.push({
    kind: 'exact',
    path: '/dsh-nx/state',
    async handler(req, res) {
      const url = new URL(req.url ?? '/dsh-nx/state', 'http://localhost')
      const probe = url.searchParams.get('probe') === '1'
      const status = bridge.status()
      const payload = {
        bridge: {
          running: status.running === true,
          pid: status.pid ?? null,
          root: status.root ?? null,
          nxHome: status.nxHome ?? null,
          commands: Array.isArray(status.commands) ? status.commands.length : 0,
        },
        nx: null,
        probe: null,
        probeError: null,
        artefacts: await recentArtefacts(outputDir),
        outputDir,
        actions: lastActions.slice(0, 12),
      }
      if (probe && status.running) {
        try {
          const live = await bridge.request('status', {}, { timeoutMs: 30000 })
          payload.probe = {
            release: live.release ?? null,
            fullRelease: live.fullRelease ?? null,
            isBatch: live.isBatch === true,
            uptimeSeconds: live.uptimeSeconds ?? null,
            displayParts: Array.isArray(live.displayParts) ? live.displayParts : [],
          }
        } catch (error) {
          payload.probeError = String(error?.message ?? error)
        }
      }
      send(res, 200, payload)
    },
  })

  routes.push({
    kind: 'exact',
    path: '/dsh-nx/action',
    async handler(req, res) {
      if (req.method !== 'POST') {
        send(res, 405, { ok: false, error: 'POST required' })
        return
      }
      let body
      try {
        body = await readBody(req)
      } catch (error) {
        send(res, 400, { ok: false, error: String(error?.message ?? error) })
        return
      }
      const command = String(body.command ?? '')
      if (!PANEL_COMMANDS.has(command)) {
        send(res, 400, { ok: false, error: `command "${command}" is not available from the panel` })
        return
      }
      const requested = body.args && typeof body.args === 'object' ? body.args : {}
      const timeoutMs = Number(body.timeoutMs) > 0 ? Number(body.timeoutMs) : 240000
      const started = Date.now()
      const record = { command, at: started, ok: false, ms: 0, summary: '', file: null }

      // The panel's "打开零件" field names the part every button works on. Without this the
      // action silently hit whatever NX happened to display, which is how a PDF ended up
      // named after the wrong part.
      const targetPart = String(body.path ?? '').trim()
      let leafFromPath = ''
      if (targetPart && command !== 'open' && command !== 'new') {
        const resolved = await resolveRequested(targetPart)
        if (!resolved) {
          lastActions.unshift(Object.assign({}, record, {
            ms: 0, summary: `file not found: ${targetPart}`, at: started,
          }))
          lastActions.length = Math.min(lastActions.length, 20)
          send(res, 200, { ok: false, error: `file not found: ${targetPart}` })
          return
        }
        try {
          await bridge.request('open', { path: resolved }, { timeoutMs: 180000 })
          if (requested.path === resolved || requested.path === targetPart) delete requested.path
        } catch (error) {
          const message = `cannot open ${resolved}: ${String(error?.message ?? error)}`
          lastActions.unshift(Object.assign({}, record, { ms: 0, summary: message }))
          lastActions.length = Math.min(lastActions.length, 20)
          send(res, 200, { ok: false, error: message })
          return
        }
        leafFromPath = basename(resolved, extname(resolved)).replace(/[\\/:*?"<>|]/g, '_')
      }
      let args
      try {
        args = await withDefaults(bridge, outputDir, command, requested, { leaf: leafFromPath })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message ?? error) })
        return
      }
      try {
        const result = await bridge.request(command, args, { timeoutMs })
        record.ok = true
        record.ms = Date.now() - started
        record.summary = summarise(command, result)
        record.file = artefactOf(command, result)
        lastActions.unshift(record)
        lastActions.length = Math.min(lastActions.length, 20)
        send(res, 200, { ok: true, result, ms: record.ms, summary: record.summary, file: record.file })
      } catch (error) {
        record.ms = Date.now() - started
        record.summary = String(error?.message ?? error)
        lastActions.unshift(record)
        lastActions.length = Math.min(lastActions.length, 20)
        send(res, 200, { ok: false, error: record.summary, ms: record.ms })
      }
    },
  })

  // A file link inside the conversation is a dsh-resource address rather than a Windows path:
  // dsh-resource://file/session/session-<id>/<path relative to the workspace>. Rebuild it against
  // every plausible workspace root (the plugin runs with the profile as its cwd, not the
  // workspace), and fall back to those roots for plain relative paths too.
  function workspaceRoots() {
    const roots = [process.cwd()]
    // outputDir is normally <workspace>/work/nx/out, so walk up from it as well.
    let current = resolve(outputDir)
    for (let step = 0; step < 4; step += 1) {
      current = resolve(current, '..')
      roots.push(current)
    }
    roots.push(resolve(outputDir))
    return roots.filter((item, index) => roots.indexOf(item) === index)
  }

  async function firstExisting(candidate) {
    try {
      if ((await stat(candidate)).isFile()) return candidate
    } catch {
      // not there
    }
    return ''
  }

  // Callers sometimes hand over a URL-escaped path: a Chinese file name arrives as
  // "...\enclosure\%E4%B8%8A%E5%A3%B3.prt" and no such file exists, which used to read as
  // "the part is gone" when the part was right there. Only unescape when the literal path is
  // missing and the decoded one is a real file, so a genuine "%" in a name keeps working.
  async function unescapePath(text) {
    if (!/%[0-9a-fA-F]{2}/.test(text)) return text
    if (await firstExisting(text)) return text
    let decoded
    try {
      decoded = decodeURIComponent(text)
    } catch {
      return text
    }
    if (decoded === text) return text
    const hit = await firstExisting(decoded)
    return hit || text
  }

  // Path of the part the NX session has on display, when its file name matches `leaf`. Used as a
  // fallback for callers that hand over a relative or otherwise stale path for a part that is
  // already open: previewing what the session actually has beats a "no such file" dead end.
  async function openPartNamed(leaf) {
    const wanted = String(leaf ?? '').trim().toLowerCase()
    if (!wanted) return ''
    try {
      const info = await bridge.request('info')
      const fullPath = String(info?.fullPath ?? '')
      if (fullPath && basename(fullPath).toLowerCase() === wanted) return fullPath
    } catch {
      // no session, nothing to adopt
    }
    return ''
  }

  async function resolveRequested(raw) {
    const text = await unescapePath(String(raw ?? '').trim())
    if (!text) return text
    const roots = workspaceRoots()

    if (/^dsh-resource:/i.test(text)) {
      const parts = text
        .replace(/^dsh-resource:\/*/i, '')
        .split(/[\\/]+/)
        .filter(Boolean)
      // ["file", "session", "session-<id>", ...relative segments]
      const tail = parts.slice(3)
      if (!tail.length) return text
      for (const root of roots) {
        const candidate = resolve(root, ...tail)
        const found = await firstExisting(candidate)
        if (found) return found
      }
      return resolve(roots[0], ...tail)
    }

    const direct = resolve(text)
    const hit = await firstExisting(direct)
    if (hit) return hit
    // An absolute path that is missing: report it as-is, the message is the useful part.
    if (/^[a-zA-Z]:[\\/]/.test(text) || text.startsWith('\\\\')) return direct
    const tail = text.split(/[\\/]+/).filter(Boolean)
    if (!tail.length) return direct
    for (const root of roots) {
      const candidate = resolve(root, ...tail)
      const found = await firstExisting(candidate)
      if (found) return found
    }
    return direct
  }

  routes.push({
    kind: 'exact',
    path: '/dsh-nx/preview',
    async handler(req, res) {
      if (req.method !== 'POST') {
        send(res, 405, { ok: false, error: 'POST required' })
        return
      }
      let body
      try {
        body = await readBody(req)
      } catch (error) {
        send(res, 400, { ok: false, error: String(error?.message ?? error) })
        return
      }
      const requested = String(body.path ?? '')
      if (!PREVIEWABLE.test(requested)) {
        send(res, 400, { ok: false, error: 'preview needs a .prt, .step, .stp, .iges, .igs or .stl file' })
        return
      }
      const root = resolve(outputDir)
      // Any part on this machine can be previewed: the panel only ever reads the file through NX
      // and writes the mesh into its own cache folder, so the source stays untouched.
      let sourcePath = await resolveRequested(String(body.path ?? ''))
      let info
      let adopted = ''
      try {
        info = await stat(sourcePath)
      } catch {
        // The part may already be open in NX under that exact file name (agents often hand over a
        // workspace-relative path such as "enclosure/上壳.prt"). Ask the session which part it has
        // and preview that one instead of telling the user the file is gone. The name may still be
        // percent-encoded here, so try the raw and the decoded leaf.
        let decoded = requested
        try {
          decoded = decodeURIComponent(requested)
        } catch {
          // keep the raw text
        }
        for (const name of [basename(sourcePath), basename(decoded)]) {
          const open = await openPartNamed(name)
          if (!open) continue
          try {
            info = await stat(open)
            sourcePath = open
            adopted = open
            break
          } catch {
            info = undefined
          }
        }
        if (!info) {
          // Say what was actually looked for: a percent-encoded name is the common false alarm,
          // and a bare "no such file" reads as if the panel lost the part.
          const hint = decoded !== requested ? ` (the name arrived URL-encoded; also tried ${decoded})` : ''
          send(res, 404, { ok: false, error: `no such file: ${sourcePath}${hint}`, requested, resolved: sourcePath })
          return
        }
      }
      if (!info.isFile()) {
        send(res, 400, { ok: false, error: `not a file: ${sourcePath}` })
        return
      }

      // Only a part or an already-tessellated mesh can be previewed. Handing NX an IGES or
      // STEP file made it create a part of that name (File already exists) and NX cannot open
      // an STL as a part at all (First parameter is invalid) - both used to surface as
      // "预览失败" on files that were perfectly fine.
      if (!/\.(prt|stl)$/i.test(sourcePath)) {
        send(res, 200, {
          ok: false,
          error: `只能预览 .prt 或 .stl（${extname(sourcePath) || '无扩展名'} 请用「文件夹」打开）`,
          source: sourcePath,
        })
        return
      }

      const folder = join(root, 'panel', 'preview')
      const leaf = basename(sourcePath, extname(sourcePath)).replace(/[\\/:*?"<>|]/g, '_')
      // The v3 prefix retires meshes cached before the renderer was fixed: v2 previews were
      // exported at NX's default 0.058 mm / 18 degrees, coarse enough that a thread became a
      // stack of flat rings. Cache keys carry the version so the finer mesh is actually used.
      const cacheName = `v4-${leaf}-${Math.round(info.mtimeMs)}-${info.size}.stl`
      const cachePath = join(folder, cacheName)
      const url = `/dsh-nx/preview-file?name=${encodeURIComponent(cacheName)}`
      const adoptedNote = adopted ? `previewed the part already open in NX (${adopted})` : ''

      try {
        const cached = await stat(cachePath)
        send(res, 200, {
          ok: true,
          cached: true,
          triangles: await countTriangles(cachePath),
          bytes: cached.size,
          ms: 0,
          stl: url,
          source: sourcePath,
          note: adoptedNote,
        })
        return
      } catch {
        // not converted yet
      }

      const started = Date.now()
      try {
        await mkdir(folder, { recursive: true })
        if (/\.stl$/i.test(sourcePath)) {
          // Already a mesh: hand it straight to the viewer, no NX round trip needed.
          await copyFile(sourcePath, cachePath)
        } else {
          await bridge.request('open', { path: sourcePath }, { timeoutMs: 180000 })
          await bridge.request('export', {
            format: 'stl',
            path: cachePath,
            // Every body, not the exporter's default (the last one): a sheet-metal style part
            // is often several unstitched faces, and a preview showing one of them is a lie.
            bodies: 'all',
            // Finer than the translator's default so threads and fillets survive into the mesh.
            tolerance: 0.02,
            angle: 8,
          }, { timeoutMs: 300000 })
        }
        const written = await stat(cachePath)
        const triangles = await countTriangles(cachePath)
        if (triangles === 0) {
          // A zero-triangle mesh is a broken preview, not a picture: say so instead of
          // handing the viewer a file it will fail to parse.
          send(res, 200, {
            ok: false,
            error: '这个网格是空的（0 个三角面）：重新导出一次，或先在 NX 里打开该零件/装配',
            source: sourcePath,
          })
          return
        }
        send(res, 200, {
          ok: true,
          cached: false,
          triangles,
          bytes: written.size,
          ms: Date.now() - started,
          stl: url,
          source: sourcePath,
          note: adoptedNote,
        })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message ?? error), source: sourcePath })
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/dsh-nx/reveal',
    async handler(req, res) {
      if (req.method !== 'POST') {
        send(res, 405, { ok: false, error: 'POST required' })
        return
      }
      let body
      try {
        body = await readBody(req)
      } catch (error) {
        send(res, 400, { ok: false, error: String(error?.message ?? error) })
        return
      }
      const target = resolve(await unescapePath(String(body.path ?? '')))
      if (!target) {
        send(res, 400, { ok: false, error: 'path required' })
        return
      }
      let info
      try {
        info = await stat(target)
      } catch {
        send(res, 404, { ok: false, error: `no such file: ${target}` })
        return
      }
      try {
        // explorer /select,<file> opens the containing folder with the file highlighted.
        const child = info.isDirectory()
          ? spawn('explorer.exe', [target], { detached: true, stdio: 'ignore' })
          : spawn('explorer.exe', [`/select,${target}`], { detached: true, stdio: 'ignore' })
        child.unref()
        send(res, 200, { ok: true, path: target, folder: dirname(target) })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message ?? error), path: target })
      }
    },
  })

  routes.push({
    kind: 'exact',
    path: '/dsh-nx/preview-file',
    async handler(req, res) {
      const url = new URL(req.url ?? '/dsh-nx/preview-file', 'http://localhost')
      const name = basename(String(url.searchParams.get('name') ?? ''))
      const folder = join(resolve(outputDir), 'panel', 'preview')
      if (!name || name !== String(url.searchParams.get('name') ?? '')) {
        send(res, 400, { ok: false, error: 'bad preview name' })
        return
      }
      let buffer
      try {
        buffer = await readFile(join(folder, name))
      } catch {
        send(res, 404, { ok: false, error: 'no preview for that name' })
        return
      }
      res.statusCode = 200
      res.setHeader('content-type', 'application/octet-stream')
      res.setHeader('cache-control', 'no-store')
      res.end(buffer)
    },
  })

  const disposers = routes.map((route) => webServer.register(route))
  return () => {
    for (const dispose of disposers) {
      if (typeof dispose === 'function') dispose()
    }
  }
}

/** Export formats and the extension each one wants. */
const EXTENSION = { step: 'stp', iges: 'igs', stl: 'stl' }

/**
 * Fill in the output paths a panel button cannot ask the user for.
 *
 * The panel's one-click buttons name a command, not a destination, so the
 * artefact lands under `<outputDir>/panel/<current part>` and shows up in the
 * panel's own list afterwards. Nothing is invented when the caller did pass a
 * path: tool calls stay authoritative.
 */
async function withDefaults(bridge, outputDir, command, args, options) {
  const next = { ...args }
  const wantsPath = (command === 'export' && !next.path)
    || (command === 'pdf' && !next.path)
    || (command === 'asm_bom' && !next.csv)
    || (command === 'asm_list' && !next.csv)
    || (command === 'bom_table' && !next.csv)
    || (command === 'batch_drawings' && !next.outDir)
  if (!wantsPath) return next

  // The part the user typed into "打开零件" wins over whatever NX displays: outputs must
  // be named after the part the panel is pointed at.
  let leaf = options && options.leaf ? options.leaf : ''
  if (!leaf) {
    try {
      const info = await bridge.request('info', {}, { timeoutMs: 60000 })
      if (info && typeof info.leaf === 'string' && info.leaf) {
        leaf = info.leaf.replace(/[\\/:*?"<>|]/g, '_')
      }
    } catch {
      // no part open yet: the bridge will answer with its own error
    }
  }
  if (!leaf) leaf = 'part'

  const folder = join(outputDir, 'panel')
  if (command === 'export') {
    next.format = String(next.format ?? 'step').toLowerCase()
    const extension = EXTENSION[next.format] ?? next.format
    next.path = join(folder, `${leaf}.${extension}`)
    // Repeated clicks must overwrite the previous export instead of failing on it.
    if (next.overwrite === undefined) next.overwrite = true
  } else if (command === 'pdf') {
    next.path = join(folder, `${leaf}.pdf`)
    if (next.overwrite === undefined) next.overwrite = true
  } else if (command === 'bom_table') {
    next.csv = join(folder, `${leaf}_bom.csv`)
  } else if (command === 'asm_list') {
    next.csv = join(folder, `${leaf}_components.csv`)
  } else if (command === 'batch_drawings') {
    // One folder per run so a second batch never overwrites the first.
    next.outDir = join(folder, 'batch', `${leaf}-${stamp()}`)
  } else {
    next.csv = join(folder, `${leaf}_bom.csv`)
    if (next.overwrite === undefined) next.overwrite = true
  }
  await mkdir(dirname(next.path ?? next.csv ?? next.outDir), { recursive: true })
  return next
}

/** yyyyMMdd-HHmmss, for folders a run should not reuse. */
function stamp() {
  const now = new Date()
  const pad = (value) => String(value).padStart(2, '0')
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
}

/** One line the panel can show after an action, without shipping the payload. */
function summarise(command, result) {
  if (!result || typeof result !== 'object') return String(result ?? '')
  switch (command) {
    case 'open':
    case 'save':
      return `${command} ${result.leaf ?? ''} ${result.fullPath ?? ''}`.trim()
    case 'info': {
      const bodies = Array.isArray(result.bodies) ? result.bodies.length : Number(result.bodies ?? 0)
      const sheets = Array.isArray(result.sheets) ? result.sheets.length : 0
      const what = bodies > 0 ? `${bodies} body/bodies` : 'no body (assembly)'
      return `part ${result.leaf ?? ''}, ${what}, ${sheets} sheet(s)`
    }
    case 'measure':
      return result.assembly === true
        ? `assembly, ${result.components ?? 0} component(s)`
        : `volume ${round(result.volume)} mm³, mass ${round(result.mass)} kg`
    case 'export':
      return `${result.format ?? 'export'} ${result.exists ? 'written' : 'missing'} ${result.bytes ?? 0} bytes`
    case 'drawing': {
      const name = result.sheet?.name ?? result.sheet ?? ''
      const views = Array.isArray(result.views) ? result.views.length : 0
      return `sheet ${name}${result.reused ? ' (reused)' : ''}, ${views} view(s)`
    }
    case 'pdf':
      return `pdf ${result.exists ? 'written' : 'missing'} ${result.bytes ?? 0} bytes`
    case 'asm_bom':
      return `bom ${result.totals?.distinct ?? 0} distinct part(s)`
    case 'asm_clearance':
      return `clearance ${round(result.minimum)} mm over ${result.checked ?? 0} component(s)`
    case 'asm_list':
      return `assembly ${result.componentCount ?? 0} component(s)`
    case 'layers':
      return `layers: ${Array.isArray(result.layers) ? result.layers.length : 'ok'}`
    case 'refset':
      return `reference sets: ${Array.isArray(result.referenceSets) ? result.referenceSets.length : 'ok'}`
    case 'bom_table':
      return `table ${result.rows ?? 0} row(s), ${result.quantity ?? 0} part(s), ${round(result.mass)} kg`
    case 'batch_drawings':
      return `batch ${result.drawn ?? 0}/${result.parts ?? 0} drawing(s) printed`
    case 'section_view':
      return `section ${result.view?.name ?? ''} of ${result.parent ?? ''} (${result.type ?? ''})`
    case 'note':
      return `note ${result.lines ?? 0} line(s) at ${Array.isArray(result.at) ? result.at.join(',') : ''}`
    case 'explode': {
      const restoring = result.restored !== undefined
      const count = restoring
        ? result.restored
        : (result.exploded ?? (Array.isArray(result.moves) ? result.moves.length : 0))
      const where = result.distance ? ` by ${result.distance} mm` : ''
      return `${restoring ? 'restored' : 'exploded'} ${count} component(s)${restoring ? '' : where}`
    }
    case 'faces':
      return `${Array.isArray(result.faces) ? result.faces.length : 0} face(s)`
    case 'annotate':
      return `${result.notes ?? 0} note(s)`
    default:
      return command
  }
}

/**
 * The file (or folder) an action just produced, so the panel can offer it as a chip instead of
 * asking the user to guess where the artefact went. `null` for actions that write nothing.
 */
function artefactOf(command, result) {
  if (!result || typeof result !== 'object') return null
  const file = (path, note) => (path ? { path, note: note ?? '' } : null)
  switch (command) {
    case 'export':
      return file(result.path, result.exists ? `${result.bytes ?? 0} B` : '未写出')
    case 'pdf':
      return file(result.path, result.exists ? `${result.bytes ?? 0} B` : '未写出')
    case 'save':
      return file(result.fullPath, result.bytes ? `${result.bytes} B` : '已保存')
    case 'asm_bom':
      return file(result.csv, `${result.csvBytes ?? result.bytes ?? 0} B`)
    case 'bom_table':
      // The merged button draws the table and writes the CSV in the same call.
      return file(result.csv, result.csvBytes ? `${result.csvBytes} B` : '仅表格')
    case 'asm_list':
      return file(result.csv, result.csvBytes ? `${result.csvBytes} B` : '清单')
    case 'batch_drawings':
      return file(result.outDir, `${result.drawn ?? 0}/${result.parts ?? 0} 份`)
    default:
      return null
  }
}

function round(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '?'
  return Math.abs(number) >= 100 ? number.toFixed(0) : number.toFixed(3)
}

/** Triangle count from a binary STL header (84 bytes in) so the panel can label the mesh. */
async function countTriangles(file) {
  try {
    const buffer = await readFile(file)
    if (buffer.byteLength < 84) return 0
    const declared = buffer.readUInt32LE(80)
    const fits = 84 + declared * 50 <= buffer.byteLength
    return fits ? declared : Math.floor((buffer.byteLength - 84) / 50)
  } catch {
    return 0
  }
}
