/**
 * Host half of the live 3D preview.
 *
 * Two ways out, on purpose:
 *
 *   1. Routes on the DSH web server (`/dsh-nx/live3d/*`) — what the Web build uses.
 *   2. A tiny loopback server of our own, on an OS-assigned port — what the Desktop
 *      build needs. Electron's sidebar webview shows a blank page when it is pointed
 *      at the DSH server's own port (that server sits behind an auth layer and answers
 *      401 on the document root), but a plain second port loads fine.
 *
 * `/dsh-nx/live3d/base` reports the origin the browser should be sent to, so the
 * client does not have to know which build it is running in.
 *
 * Cost model (measured on a 56.9k-triangle flange at 0.05 mm chordal): faceting
 * ~0.3 s, one NX API call per facet at 54 µs -> ~3.4 s, JSON write ~0.2 s. The mesh
 * is cached and only rebuilt when a cheap probe (bodies + volume) moves.
 */

import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

/** Bodies + volume: enough to tell "the model changed" without paying for facets. */
const PROBE_PY = [
    'try:',
    '    _wp = NXOpen.Session.GetSession().Parts.Work',
    '    if _wp is not None: part = _wp',
    '    _bs = list(part.Bodies)',
    '    _pr = NXOpen.Session.GetSession().Measurement.GetBodyProperties(_bs, 0.999, False)',
    "    w('%d|%.4f' % (len(_bs), _pr[0][1]))",
    'except Exception as e:',
    "    w('0|0|%s' % e)",
].join('\n')

function sendHtml(res, html) {
    res.statusCode = 200
    res.setHeader('content-type', 'text/html; charset=utf-8')
    /* no-transform: the compression middleware would otherwise gzip this, and the
       sidebar webview eats a gzipped body. content-length: node falls back to chunked
       without it, which the same webview also refuses to paint. */
    res.setHeader('cache-control', 'no-store, no-transform')
    res.setHeader('content-length', String(Buffer.byteLength(html, 'utf8')))
    res.end(html)
}

function sendJson(res, status, payload) {
    const body = JSON.stringify(payload)
    res.statusCode = status
    res.setHeader('content-type', 'application/json; charset=utf-8')
    res.setHeader('cache-control', 'no-store, no-transform')
    res.setHeader('content-length', String(Buffer.byteLength(body, 'utf8')))
    res.end(body)
}

async function readBody(req) {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    if (!chunks.length) return {}
    try {
        return JSON.parse(Buffer.concat(chunks).toString('utf8'))
    }
    catch {
        return {}
    }
}

/** `w()` output of the push script: "<path>|<tris>|<verts>". */
function parsePushOut(out) {
    const line = (out ?? []).filter((s) => typeof s === 'string').pop() ?? ''
    const at = line.lastIndexOf('|')
    if (at < 0) return null
    const at2 = line.lastIndexOf('|', at - 1)
    if (at2 < 0) return null
    return { path: line.slice(0, at2), tris: Number(line.slice(at2 + 1, at)) || 0 }
}

/**
 * Register the preview: routes on the DSH web server plus a private loopback listener.
 *
 * @param {object} options
 * @param {object} options.webServer  host `ctx.webServer`
 * @param {object} options.bridge     the shared NxBridge
 * @returns {() => void} disposer
 */
export function registerPreviewRoutes({ webServer, bridge }) {
    const state = {
        tol: 0.05,
        flat: false,
        seq: 0,
        key: null,
        mesh: null,
        ms: 0,
        error: null,
        busy: false,
        lastProbeAt: 0,
        lastSig: '',
    }

    const outFile = join(tmpdir(), 'dsh-nx-preview.json')
    let privateOrigin = ''

    async function probe() {
        const r = await bridge.request('exec', { code: PROBE_PY }, { timeoutMs: 60000 })
        const line = (r?.out ?? []).filter((s) => typeof s === 'string').pop() ?? '0|0'
        return line.split('|')[0] + '|' + line.split('|')[1]
    }

    async function build(sig) {
        const tpl = await readFile(join(HERE, 'preview-push.py'), 'utf8')
        /* preview-push.py names each placeholder twice (once in the header comment),
           so these have to be global replacements. */
        const code = tpl
            .replace(/__TOL__/g, String(state.tol || 0))
            .replace(/__FLAT__/g, state.flat ? 'True' : 'False')
            .replace(/__OUT__/g, outFile.replace(/\\/g, '/'))
        const t0 = Date.now()
        const r = await bridge.request('exec', { code }, { timeoutMs: 600000 })
        const info = parsePushOut(r?.out)
        if (!info) throw new Error('preview push produced no output: ' + JSON.stringify(r?.out ?? []).slice(0, 200))
        const raw = await readFile(info.path, 'utf8')
        const mesh = JSON.parse(raw)
        state.mesh = mesh
        state.ms = Date.now() - t0
        state.seq += 1
        state.key = sig
        state.error = null
        return mesh
    }

    async function meshFor() {
        if (state.busy) {
            for (let i = 0; i < 600 && state.busy; i += 1) {
                await new Promise((r) => setTimeout(r, 100))
            }
            if (state.mesh) return state.mesh
        }
        let sig = state.lastSig
        if (Date.now() - state.lastProbeAt > 700 || !sig) {
            try {
                sig = await probe()
                state.lastSig = sig
                state.lastProbeAt = Date.now()
            }
            catch (error) {
                if (state.mesh) return state.mesh
                throw error
            }
        }
        const key = `${state.tol}|${state.flat ? 1 : 0}|${sig}`
        if (state.key === key && state.mesh) return state.mesh
        state.busy = true
        try {
            return await build(key)
        }
        finally {
            state.busy = false
        }
    }

    /* ---- handlers, shared by both front doors ---- */

    async function onPage(req, res) {
        // 每次请求都从磁盘读，改页面不用重启 dsh
        const pagePath = join(HERE, 'preview.html')
        let page = ''
        try { page = await readFile(pagePath, 'utf8') }
        catch (e) { page = '<h1>preview.html 缺失</h1>' }
        sendHtml(res, page)
    }

    async function onBase(req, res) {
        let origin = ''
        try {
            if (typeof webServer.getOrigin === 'function') origin = webServer.getOrigin()
            else if (webServer.port) origin = 'http://127.0.0.1:' + String(webServer.port)
        }
        catch {
            origin = ''
        }
        sendJson(res, 200, { origin: privateOrigin || origin })
    }

    async function onMesh(req, res) {
        /* 客户端报的 seq 等于本地 seq 时不能直接回 unchanged：seq 只在 build() 里递增，
           模型可能在两次轮询之间被改过。这里永远先跑一次探针（700 ms 节流保护 NX），
           由 meshFor() 决定是否重建，再看 seq 有没有涨。 */
        try {
            const before = state.seq
            const mesh = await meshFor()
            if (state.seq === before) {
                return sendJson(res, 200, { seq: state.seq, unchanged: true })
            }
            return sendJson(res, 200, {
                seq: state.seq,
                name: mesh.name ?? '',
                vertices: mesh.vertices,
                indices: mesh.indices,
                ms: state.ms,
                tris: mesh.tris ?? Math.floor((mesh.indices?.length ?? 0) / 3),
            })
        }
        catch (error) {
            state.error = String(error?.message ?? error)
            return sendJson(res, 503, { seq: state.seq, error: state.error })
        }
    }

    async function onQuality(req, res) {
        if ((req.method ?? 'GET').toUpperCase() === 'POST') {
            const body = await readBody(req)
            if (body && typeof body.tol === 'number' && body.tol > 0) state.tol = body.tol
            if (body && typeof body.flat === 'boolean') state.flat = body.flat
        }
        sendJson(res, 200, { tol: state.tol, flat: state.flat, seq: state.seq, busy: state.busy })
    }

    function dispatch(req, res) {
        const path = new URL(req.url ?? '/', 'http://localhost').pathname
        if (path === '/' || path === '/live3d' || path === '/live3d/' || path === '/dsh-nx/live3d' || path === '/dsh-nx/live3d/') return onPage(req, res)
        if (path.endsWith('/live3d/base')) return onBase(req, res)
        if (path.endsWith('/live3d/mesh')) return onMesh(req, res)
        if (path.endsWith('/live3d/quality')) return onQuality(req, res)
        res.statusCode = 404
        res.setHeader('content-length', '9')
        res.end('not found')
    }

    /* ---- door 1: the DSH web server ---- */
    const routes = [
        { kind: 'exact', path: '/dsh-nx/live3d', handler: onPage },
        { kind: 'exact', path: '/dsh-nx/live3d/', handler: onPage },
        { kind: 'exact', path: '/dsh-nx/live3d/mesh', handler: onMesh },
        { kind: 'exact', path: '/dsh-nx/live3d/base', handler: onBase },
        { kind: 'exact', path: '/dsh-nx/live3d/quality', handler: onQuality },
    ]
    const disposers = routes.map((route) => webServer.register(route))

    /* ---- door 2: a private loopback listener on its own port ---- */
    const privateServer = createServer((req, res) => {
        Promise.resolve(dispatch(req, res)).catch((error) => {
            try {
                sendJson(res, 500, { error: String(error?.message ?? error) })
            }
            catch {
                /* headers already sent */
            }
        })
    })
    /* 想固定端口，省得每次翻 /base 去找 OS 分配的那个号；占了就退回任意端口。 */
    const WANT_PORT = 8890
    let portTried = false
    privateServer.on('listening', () => {
        const addr = privateServer.address()
        if (addr && typeof addr === 'object') {
            privateOrigin = `http://127.0.0.1:${String(addr.port)}`
            console.log(`[dsh-nx] 实时预览: ${privateOrigin}/dsh-nx/live3d/`)
        }
    })
    privateServer.on('error', (error) => {
        if (error?.code === 'EADDRINUSE' && !portTried) {
            portTried = true
            console.warn(`[dsh-nx] 端口 ${WANT_PORT} 被占，改用任意端口`)
            privateServer.listen(0, '127.0.0.1')
        }
    })
    privateServer.listen(WANT_PORT, '127.0.0.1')

    return () => {
        for (const dispose of disposers) {
            try {
                dispose()
            }
            catch {
                // the server is already gone
            }
        }
        try {
            privateServer.close()
        }
        catch {
            // already closed
        }
    }
}
