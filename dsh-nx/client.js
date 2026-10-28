/**
 * Browser half of the dsh-nx panel.
 *
 * Registers one sidebar row and the page it selects. Both talk to the host
 * through the two routes lib/panel.js publishes; nothing here touches the NX
 * bridge directly, and nothing here needs a tool call to stay current.
 */
window.__ModuleLoader__.load({
    id: 'dsh-nx',
    factory(require) {
        const React = require('react');
        const h = React.createElement;

        /** Root-relative URL, anchored on the directory the UI is served from. */
        function api(path) {
            const relative = String(path).replace(/^\/+/, '');
            if (typeof document === 'undefined') return '/' + relative;
            const resolved = new URL(relative, document.baseURI);
            // Keep the query: preview-file carries the cache name in it.
            return resolved.pathname + resolved.search;
        }

        const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        const HAIRLINE = '1px solid rgba(127,127,127,0.28)';
        const CARD = 'rgba(127,127,127,0.07)';

        const css = {
            page: { display: 'flex', flexDirection: 'column', gap: 12, height: '100%', overflowY: 'auto', padding: '12px 14px 20px', fontSize: 13, lineHeight: 1.5 },
            header: { display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' },
            title: { fontSize: 17, fontWeight: 600, letterSpacing: 0.2 },
            dim: { opacity: 0.62 },
            mono: { fontFamily: MONO, fontSize: 12 },
            card: { border: HAIRLINE, background: CARD, borderRadius: 10, padding: '10px 12px' },
            row: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
            group: { display: 'flex', gap: 8, flexWrap: 'wrap' },
            button: { border: HAIRLINE, background: 'transparent', color: 'inherit', borderRadius: 8, padding: '5px 11px', fontSize: 12.5, cursor: 'pointer' },
            chipOn: { border: '1px solid rgba(86,157,255,0.7)', background: 'rgba(86,157,255,0.16)', color: 'inherit', borderRadius: 999, padding: '2px 9px', fontSize: 11.5, cursor: 'pointer' },
            chipOff: { border: HAIRLINE, background: 'transparent', color: 'inherit', borderRadius: 999, padding: '2px 9px', fontSize: 11.5, cursor: 'pointer', opacity: 0.72 },
            buttonBusy: { border: HAIRLINE, background: 'rgba(127,127,127,0.18)', color: 'inherit', borderRadius: 8, padding: '5px 11px', fontSize: 12.5, cursor: 'progress' },
            input: { border: HAIRLINE, background: 'transparent', color: 'inherit', borderRadius: 8, padding: '5px 9px', fontSize: 12, fontFamily: MONO, flex: '1 1 260px', minWidth: 180 },
            table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
            th: { textAlign: 'left', fontWeight: 600, padding: '3px 8px 3px 0', opacity: 0.7, borderBottom: HAIRLINE, whiteSpace: 'nowrap' },
            td: { padding: '3px 8px 3px 0', borderBottom: '1px solid rgba(127,127,127,0.12)', verticalAlign: 'top' },
            sectionTitle: { fontSize: 11.5, letterSpacing: 0.6, textTransform: 'uppercase', opacity: 0.55, marginBottom: 6 },
            canvas: { display: 'block', width: '100%', height: 320, borderRadius: 8, border: HAIRLINE, background: 'rgba(127,127,127,0.06)', touchAction: 'none' },
            bad: { color: '#c34a3f', marginTop: 4 },
            good: { color: '#3fa34d' },
        };

        function dot(state) {
            const colour = state === 'ok' ? '#3fa34d' : state === 'warn' ? '#d49b28' : state === 'off' ? '#9a9a9a' : '#c34a3f';
            return h('span', {
                style: { display: 'inline-block', width: 9, height: 9, borderRadius: '50%', background: colour, flex: '0 0 auto' },
            });
        }

        function bytes(value) {
            const number = Number(value);
            if (!Number.isFinite(number)) return '?';
            if (number >= 1024 * 1024) return (number / (1024 * 1024)).toFixed(1) + ' MB';
            if (number >= 1024) return (number / 1024).toFixed(1) + ' kB';
            return number + ' B';
        }

        /** Alpha channel of a computed CSS colour, 0 when it cannot be parsed. */
        function colourAlpha(text) {
            const match = /rgba?\(([^)]+)\)/.exec(String(text || ''));
            if (!match) return String(text || '') === 'transparent' ? 0 : 1;
            const parts = match[1].split(/[,\s/]+/).filter(Boolean);
            if (parts.length < 4) return 1;
            const alpha = Number(parts[3]);
            return Number.isFinite(alpha) ? alpha : 1;
        }

        /**
         * First opaque background found walking up from the panel, so the preview page paints the
         * same colour as its surroundings instead of letting whatever is behind it show through.
         */
        function pageBackground() {
            try {
                let node = document.body;
                while (node) {
                    const colour = getComputedStyle(node).backgroundColor;
                    if (colour && colourAlpha(colour) > 0.9) return colour;
                    node = node.parentElement;
                }
            } catch { /* no document in this context */ }
            try {
                return matchMedia('(prefers-color-scheme: dark)').matches ? '#1b1d1f' : '#f7f8fa';
            } catch { return '#f7f8fa'; }
        }

        function shortPath(path) {
            const text = String(path ?? '');
            const parts = text.split(/[\\/]/).filter(Boolean);
            if (parts.length <= 3) return text;
            return '…\\' + parts.slice(-2).join('\\');
        }

        /**
         * Parse a binary (or ASCII) STL into a flat triangle list centred and scaled to ±1,
         * plus the measurements the info card shows (taken from the mm coordinates, before
         * the mesh is normalised, so the numbers stay honest).
         */
        function readStl(buffer) {
            const view = new DataView(buffer);
            let points = null;
            if (buffer.byteLength >= 84) {
                const declared = view.getUint32(80, true);
                if (declared > 0 && 84 + declared * 50 <= buffer.byteLength) {
                    points = new Float32Array(declared * 9);
                    let offset = 84;
                    for (let i = 0; i < declared; i += 1) {
                        offset += 12;
                        for (let v = 0; v < 9; v += 1) {
                            points[i * 9 + v] = view.getFloat32(offset, true);
                            offset += 4;
                        }
                        offset += 2;
                    }
                }
            }
            if (!points) {
                const text = new TextDecoder().decode(buffer);
                const found = [];
                const pattern = /vertex\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)/g;
                let match = pattern.exec(text);
                while (match) {
                    found.push(Number(match[1]), Number(match[2]), Number(match[3]));
                    match = pattern.exec(text);
                }
                if (!found.length) return null;
                points = new Float32Array(found);
            }
            let minX = Infinity, minY = Infinity, minZ = Infinity;
            let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
            for (let i = 0; i < points.length; i += 3) {
                if (points[i] < minX) minX = points[i];
                if (points[i] > maxX) maxX = points[i];
                if (points[i + 1] < minY) minY = points[i + 1];
                if (points[i + 1] > maxY) maxY = points[i + 1];
                if (points[i + 2] < minZ) minZ = points[i + 2];
                if (points[i + 2] > maxZ) maxZ = points[i + 2];
            }

            // Area and signed volume straight off the triangles: for a closed mesh these add
            // up to the same numbers NX reports, which is what the info card is imitating.
            let area = 0;
            let volume = 0;
            // Watertight test: a closed solid has every triangle edge shared by exactly two
            // triangles. A sheet body does not, and its "volume" is a signed tetrahedron sum
            // that means nothing -- the info card reports the body type instead.
            const edges = new Map();
            const key = (x, y, z) => `${Math.round(x * 1000)},${Math.round(y * 1000)},${Math.round(z * 1000)}`;
            for (let i = 0; i + 8 < points.length; i += 9) {
                const ax = points[i], ay = points[i + 1], az = points[i + 2];
                const bx = points[i + 3], by = points[i + 4], bz = points[i + 5];
                const cx = points[i + 6], cy = points[i + 7], cz = points[i + 8];
                const ux = bx - ax, uy = by - ay, uz = bz - az;
                const vx = cx - ax, vy = cy - ay, vz = cz - az;
                const nx = uy * vz - uz * vy;
                const ny = uz * vx - ux * vz;
                const nz = ux * vy - uy * vx;
                area += Math.sqrt(nx * nx + ny * ny + nz * nz) / 2;
                volume += (ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz) + az * (bx * cy - by * cx)) / 6;
                const ka = key(ax, ay, az), kb = key(bx, by, bz), kc = key(cx, cy, cz);
                for (const pair of [[ka, kb], [kb, kc], [kc, ka]]) {
                    const edge = pair[0] < pair[1] ? `${pair[0]}|${pair[1]}` : `${pair[1]}|${pair[0]}`;
                    edges.set(edge, (edges.get(edge) || 0) + 1);
                }
            }
            let open = 0;
            for (const count of edges.values()) if (count !== 2) open += 1;
            const closed = open === 0 && edges.size > 0;

            const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2, midZ = (minZ + maxZ) / 2;
            const extent = Math.max(maxX - minX, maxY - minY, maxZ - minZ) || 1;
            const factor = 2 / extent;
            // Centre, scale to ±1, and turn Z-up (NX) into Y-up (the viewer): (x, y, z) ->
            // (x, z, -y). A rotation (determinant +1), not a swap, so the winding keeps its
            // meaning -- and +Z really ends up on top instead of the part being shown upside down.
            for (let i = 0; i < points.length; i += 3) {
                const x = points[i], y = points[i + 1], z = points[i + 2];
                points[i] = (x - midX) * factor;
                points[i + 1] = (z - midZ) * factor;
                points[i + 2] = -(y - midY) * factor;
            }
            return {
                points,
                metrics: {
                    triangles: Math.floor(points.length / 9),
                    area,
                    volume: Math.abs(volume),
                    closed,
                    openEdges: open,
                    size: [maxX - minX, maxY - minY, maxZ - minZ],
                    min: [minX, minY, minZ],
                    max: [maxX, maxY, maxZ],
                },
            };
        }

        // Key light in VIEW space: up and to the left of the camera, so the lit side of a boss
        // reads as raised instead of flat.
        const LIGHT = [-0.35, 0.86, 0.38];

        const VIEW_DEFAULTS = { rot: null, zoom: 1, panX: 0, panY: 0 };
        const SURFACE_EDGE = '#5b6b7a';

        /** Row-major 3x3 helpers. The viewer keeps orientation as a matrix so a drag can spin
         * the model about one chosen axis (like NX's drag handles) instead of only two Euler
         * angles, which cannot express a rotation about an arbitrary axis. */
        function rotAxis(axis, angle) {
            const c = Math.cos(angle), s = Math.sin(angle);
            if (axis === 'x') return [1, 0, 0, 0, c, -s, 0, s, c];
            if (axis === 'y') return [c, 0, s, 0, 1, 0, -s, 0, c];
            return [c, -s, 0, s, c, 0, 0, 0, 1];
        }

        function rotMul(a, b) {
            const out = new Array(9);
            for (let row = 0; row < 3; row += 1) {
                for (let col = 0; col < 3; col += 1) {
                    out[row * 3 + col] =
                        a[row * 3] * b[col] + a[row * 3 + 1] * b[3 + col] + a[row * 3 + 2] * b[6 + col];
                }
            }
            return out;
        }

        function rotEuler(rx, ry) {
            return rotMul(rotAxis('x', rx), rotAxis('y', ry));
        }

        /** Rotation by `angle` about an arbitrary unit axis (Rodrigues). Needed because a drag
         * handle spins the part about ITS axis, which lives wherever the current rotation puts
         * it — rotating about a fixed view axis would tilt the handle itself. */
        function rotAbout(axis, angle) {
            const length = Math.sqrt(axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2]) || 1;
            const x = axis[0] / length, y = axis[1] / length, z = axis[2] / length;
            const c = Math.cos(angle), s = Math.sin(angle), t = 1 - c;
            return [
                t * x * x + c, t * x * y - s * z, t * x * z + s * y,
                t * x * y + s * z, t * y * y + c, t * y * z - s * x,
                t * x * z - s * y, t * y * z + s * x, t * z * z + c,
            ];
        }

        function rotApply(matrix, vector) {
            return [
                matrix[0] * vector[0] + matrix[1] * vector[1] + matrix[2] * vector[2],
                matrix[3] * vector[0] + matrix[4] * vector[1] + matrix[5] * vector[2],
                matrix[6] * vector[0] + matrix[7] * vector[1] + matrix[8] * vector[2],
            ];
        }

        const VIEW_ISO = rotEuler(0.45, 0.72);

        /** The three part axes as drag handles, NX colours: X red, Y green, Z blue. `dir` is the
         * axis in MODEL space (readStl turned the part's Z into the viewer's Y, so the part Y
         * points at the viewer's −Z); `drag` picks which pointer delta spins it. */
        const AXIS_HANDLES = [
            { id: 'x', label: 'X', dir: [1, 0, 0], sign: -1, drag: 'dy', color: '#d1495b' },
            { id: 'y', label: 'Y', dir: [0, 0, -1], sign: 1, drag: 'dx', color: '#3f9d4d' },
            { id: 'z', label: 'Z', dir: [0, 1, 0], sign: 1, drag: 'dx', color: '#2f7dd1' },
        ];

        const AXIS_REACH = 1.15; // handle length in model units (the mesh is scaled to ±1)
        const AXIS_HIT_PX = 18;

        /** Distance from a point to a segment, for picking an axis handle under the pointer. */
        function distanceToSegment(px, py, ax, ay, bx, by) {
            const vx = bx - ax, vy = by - ay;
            const lengthSquared = vx * vx + vy * vy;
            const t = lengthSquared > 0
                ? Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / lengthSquared))
                : 0;
            const dx = px - (ax + vx * t), dy = py - (ay + vy * t);
            return Math.sqrt(dx * dx + dy * dy);
        }


        /** Sutherland–Hodgman against one axis-aligned plane, keeping p[axis] <= limit. */
        function clipPolygon(poly, axis, limit) {
            const out = [];
            for (let i = 0; i < poly.length; i += 1) {
                const a = poly[i];
                const b = poly[(i + 1) % poly.length];
                const da = a[axis] - limit;
                const db = b[axis] - limit;
                if (da <= 0) out.push(a);
                if ((da < 0 && db > 0) || (da > 0 && db < 0)) {
                    const t = da / (da - db);
                    out.push([
                        a[0] + (b[0] - a[0]) * t,
                        a[1] + (b[1] - a[1]) * t,
                        a[2] + (b[2] - a[2]) * t,
                    ]);
                }
            }
            return out;
        }

        /**
         * Orthographic shaded render of a triangle soup onto a 2D canvas, painter-sorted.
         * options: rot (row-major 3x3), zoom, panX, panY, background, wireframe, transparent,
         *          outline, flat, bbox, clip {axis: 0|1|2, value}, axes (null | active handle id)
         */
        function paint(canvas, tris, options) {
            const opts = options || {};
            const rot = Array.isArray(opts.rot) && opts.rot.length === 9 ? opts.rot : VIEW_ISO;
            const zoom = typeof opts.zoom === 'number' ? opts.zoom : VIEW_DEFAULTS.zoom;
            const panX = typeof opts.panX === 'number' ? opts.panX : 0;
            const panY = typeof opts.panY === 'number' ? opts.panY : 0;
            if (!canvas || !tris) return;
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const cssWidth = canvas.clientWidth || 640;
            const cssHeight = canvas.clientHeight || 320;
            const width = Math.round(cssWidth * dpr);
            const height = Math.round(cssHeight * dpr);
            if (canvas.width !== width) canvas.width = width;
            if (canvas.height !== height) canvas.height = height;
            const ctx = canvas.getContext('2d');
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.globalAlpha = 1;
            ctx.clearRect(0, 0, width, height);
            ctx.fillStyle = opts.background || '#eef1f4';
            ctx.fillRect(0, 0, width, height);

            const total = Math.floor(tris.length / 9);
            // Skipping every other triangle hollows the surface out — it reads as debris, not
            // a model — so only a truly enormous mesh is thinned, and then never while clipping.
            const step = total > 200000 && !opts.clip ? 2 : 1;
            const count = Math.floor(total / step);
            const scale = Math.min(width, height) * 0.45 * zoom;
            const midX = width / 2 + panX * dpr, midY = height / 2 - panY * dpr;

            // rotate + project one model-space point to canvas pixels, filling screen/depth
            function project(x0, y0, z0) {
                const x1 = rot[0] * x0 + rot[1] * y0 + rot[2] * z0;
                const y2 = rot[3] * x0 + rot[4] * y0 + rot[5] * z0;
                const z2 = rot[6] * x0 + rot[7] * y0 + rot[8] * z0;
                return [midX + x1 * scale, midY - y2 * scale, z2];
            }

            const clip = opts.clip || null;
            const faces = [];
            let vote = 0;
            for (let k = 0; k < count; k += 1) {
                const base = k * step * 9;
                let poly = [
                    [tris[base], tris[base + 1], tris[base + 2]],
                    [tris[base + 3], tris[base + 4], tris[base + 5]],
                    [tris[base + 6], tris[base + 7], tris[base + 8]],
                ];
                if (clip && clip.axis >= 0) {
                    // Cut in model space so the slider follows the part axes, not the camera.
                    poly = clipPolygon(poly, clip.axis, clip.value);
                    if (poly.length < 3) continue;
                }
                const pts = [];
                let sumZ = 0;
                for (let i = 0; i < poly.length; i += 1) {
                    const point = project(poly[i][0], poly[i][1], poly[i][2]);
                    sumZ += point[2];
                    pts.push(point);
                }
                // Centroid depth rather than the nearest corner: two triangles of one curved
                // face otherwise tie and flicker, which smears stripes across a bore.
                const deep = sumZ / poly.length;
                // Flat shading from the MODEL-space normal rotated into view space. Computing
                // the normal from the projected points mixed pixel units with depth units, so
                // every face came out at the same brightness and the part looked like one flat
                // silhouette.
                const mux = poly[1][0] - poly[0][0], muy = poly[1][1] - poly[0][1], muz = poly[1][2] - poly[0][2];
                const mvx = poly[2][0] - poly[0][0], mvy = poly[2][1] - poly[0][1], mvz = poly[2][2] - poly[0][2];
                const mnx = muy * mvz - muz * mvy;
                const mny = muz * mvx - mux * mvz;
                const mnz = mux * mvy - muy * mvx;
                const mlen = Math.sqrt(mnx * mnx + mny * mny + mnz * mnz) || 1;
                const unx = mnx / mlen, uny = mny / mlen, unz = mnz / mlen;
                const vnx = rot[0] * unx + rot[1] * uny + rot[2] * unz;
                const vny = rot[3] * unx + rot[4] * uny + rot[5] * unz;
                const vnz = rot[6] * unx + rot[7] * uny + rot[8] * unz;
                const dot = vnx * LIGHT[0] + vny * LIGHT[1] + vnz * LIGHT[2];
                // Two-sided lighting: a translator does not have to hand the triangles back wound
                // consistently (NX writes the two flanks of a detailed thread both ways), and a
                // face wound against the camera used to drop to the darkest step and merge into a
                // black smudge. |dot| lights the surface either way round.
                const shade = opts.flat ? 0.72 : 0.26 + 0.74 * Math.abs(dot);
                // Which way round this mesh is wound overall. Only the section cap needs it: to
                // fill a cut with material we have to know whether the face the eye lands on is
                // the outside of the solid or its inside.
                let mx = 0, my = 0, mz = 0;
                for (let i = 0; i < poly.length; i += 1) { mx += poly[i][0]; my += poly[i][1]; mz += poly[i][2]; }
                const outward = mnx * mx + mny * my + mnz * mz;
                if (outward > 0) vote += 1;
                else if (outward < 0) vote -= 1;
                faces.push([deep, shade, vnz, pts, [vnx, vny, vnz], poly]);
            }
            const flip = vote < 0;
            const cutAxis = clip && clip.axis >= 0 ? clip.axis : -1;
            const cutLimit = clip ? clip.value : 0;

            // No back-face culling. The depth buffer already resolves occlusion, and culling a
            // mesh whose winding came back inconsistently punches through the model: a detailed
            // thread lost half of its flanks and showed the inside of the plate instead.
            const keep = faces.map((_, index) => index);
            ctx.lineJoin = 'round';
            if (opts.wireframe) {
                for (let i = 0; i < keep.length; i += 1) {
                    const pts = faces[keep[i]][3];
                    ctx.beginPath();
                    ctx.moveTo(pts[0][0], pts[0][1]);
                    for (let v = 1; v < pts.length; v += 1) ctx.lineTo(pts[v][0], pts[v][1]);
                    ctx.closePath();
                    ctx.strokeStyle = SURFACE_EDGE;
                    ctx.lineWidth = Math.max(1, dpr * 0.7);
                    ctx.stroke();
                }
            } else {
                // Z-buffered rasteriser. The painter's algorithm cannot separate a boss wall
                // from the plate behind it when they sit a fraction of a millimetre apart, which
                // is what smeared stripes across the bore and let far faces show through near
                // faces. Per-pixel depth is exact and needs no sort.
                const image = ctx.getImageData(0, 0, width, height);
                const data = image.data;
                const zbuf = new Float32Array(width * height);
                zbuf.fill(-Infinity);
                // Which face won each pixel: neighbouring pixels that belong to different faces
                // are where a part's own edges run, which is what makes the shape readable.
                const owner = new Int32Array(width * height);
                owner.fill(-1);
                const alpha = opts.transparent ? 0.55 : 1;

                const raster = (slot, p0, p1, p2, r, g, b) => {
                    const minX = Math.max(0, Math.floor(Math.min(p0[0], p1[0], p2[0])));
                    const maxX = Math.min(width - 1, Math.ceil(Math.max(p0[0], p1[0], p2[0])));
                    const minY = Math.max(0, Math.floor(Math.min(p0[1], p1[1], p2[1])));
                    const maxY = Math.min(height - 1, Math.ceil(Math.max(p0[1], p1[1], p2[1])));
                    if (minX > maxX || minY > maxY) return;
                    const area = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (p1[1] - p0[1]);
                    if (Math.abs(area) < 1e-9) return;
                    const inv = 1 / area;
                    for (let y = minY; y <= maxY; y += 1) {
                        const py = y + 0.5;
                        const row = y * width;
                        for (let x = minX; x <= maxX; x += 1) {
                            const px = x + 0.5;
                            const w0 = ((p1[0] - px) * (p2[1] - py) - (p2[0] - px) * (p1[1] - py)) * inv;
                            const w1 = ((p2[0] - px) * (p0[1] - py) - (p0[0] - px) * (p2[1] - py)) * inv;
                            const w2 = 1 - w0 - w1;
                            if (w0 < 0 || w1 < 0 || w2 < 0) continue;
                            const depth = w0 * p0[2] + w1 * p1[2] + w2 * p2[2];
                            const idx = row + x;
                            if (depth <= zbuf[idx]) continue;
                            zbuf[idx] = depth;
                            owner[idx] = slot;
                            const o = idx * 4;
                            if (alpha >= 1) {
                                data[o] = r;
                                data[o + 1] = g;
                                data[o + 2] = b;
                            } else {
                                data[o] += (r - data[o]) * alpha;
                                data[o + 1] += (g - data[o + 1]) * alpha;
                                data[o + 2] += (b - data[o + 2]) * alpha;
                            }
                            data[o + 3] = 255;
                        }
                    }
                };

                for (let i = 0; i < keep.length; i += 1) {
                    const slot = keep[i];
                    const face = faces[slot];
                    // Wider grey band than before: the old 96..212 range made a plate face and a
                    // boss wall look like the same flat sheet in a screenshot.
                    const level = Math.max(0, Math.min(1, face[1]));
                    const grey = Math.round(70 + 168 * level);
                    const pts = face[3];
                    // A clipped polygon has four or five corners. Rasterising only the first
                    // triangle (what this used to do) silently deleted the rest of every cut face,
                    // which is what turned a section into a fan of slivers and holes.
                    for (let v = 1; v + 1 < pts.length; v += 1) {
                        raster(slot, pts[0], pts[v], pts[v + 1], grey, grey + 1, grey + 4);
                    }
                }

                // The cut is a planar cross-section, so fill it. Without a cap the eye looks
                // straight into the hollow inside of the solid and a section reads as an empty
                // tray with a floating bore instead of as material. The cap boundary is the set
                // of clipped triangles' on-plane corners, filled even-odd in screen space and
                // depth-tested against what the faces already wrote.
                if (cutAxis >= 0) {
                    const segs = [];
                    for (let i = 0; i < faces.length; i += 1) {
                        const poly = faces[i][5];
                        if (!poly) continue;
                        const on = [];
                        for (let v = 0; v < poly.length; v += 1) {
                            if (Math.abs(poly[v][cutAxis] - cutLimit) < 1e-7) on.push(poly[v]);
                        }
                        if (on.length === 2) {
                            const a = project(on[0][0], on[0][1], on[0][2]);
                            const b = project(on[1][0], on[1][1], on[1][2]);
                            segs.push([a[0], a[1], b[0], b[1]]);
                        }
                    }
                    if (segs.length) {
                        // Depth of the plane is affine in screen space: fit it from three points.
                        const base = [0, 0, 0];
                        base[cutAxis] = cutLimit;
                        const unit1 = [0, 0, 0];
                        unit1[(cutAxis + 1) % 3] = 1;
                        const unit2 = [0, 0, 0];
                        unit2[(cutAxis + 2) % 3] = 1;
                        const q0 = project(base[0], base[1], base[2]);
                        const q1 = project(base[0] + unit1[0], base[1] + unit1[1], base[2] + unit1[2]);
                        const q2 = project(base[0] + unit2[0], base[1] + unit2[1], base[2] + unit2[2]);
                        const det = (q1[0] - q0[0]) * (q2[1] - q0[1]) - (q2[0] - q0[0]) * (q1[1] - q0[1]);
                        if (Math.abs(det) > 1e-9) {
                            const dz1 = q1[2] - q0[2];
                            const dz2 = q2[2] - q0[2];
                            const planeDx = ((q2[1] - q0[1]) * dz1 - (q1[1] - q0[1]) * dz2) / det;
                            const planeDy = ((q1[0] - q0[0]) * dz2 - (q2[0] - q0[0]) * dz1) / det;
                            const capSlot = faces.length;
                            const planeNormal = [rot[cutAxis], rot[3 + cutAxis], rot[6 + cutAxis]];
                            faces.push([0, 0, planeNormal[2], [], planeNormal, null]);
                            for (let y = 0; y < height; y += 1) {
                                const py = y + 0.5;
                                const xs = [];
                                for (let s = 0; s < segs.length; s += 1) {
                                    const g = segs[s];
                                    if ((g[1] > py) !== (g[3] > py)) {
                                        xs.push(g[0] + ((py - g[1]) * (g[2] - g[0])) / (g[3] - g[1]));
                                    }
                                }
                                if (xs.length < 2) continue;
                                xs.sort((left, right) => left - right);
                                for (let s = 0; s + 1 < xs.length; s += 2) {
                                    const from = Math.max(0, Math.ceil(xs[s] - 0.5));
                                    const to = Math.min(width - 1, Math.floor(xs[s + 1] - 0.5));
                                    for (let x = from; x <= to; x += 1) {
                                        const depth = q0[2]
                                            + planeDx * (x + 0.5 - q0[0])
                                            + planeDy * (py - q0[1]);
                                        const idx = y * width + x;
                                        if (depth <= zbuf[idx]) continue;
                                        zbuf[idx] = depth;
                                        owner[idx] = capSlot;
                                        const o = idx * 4;
                                        data[o] = 158;
                                        data[o + 1] = 162;
                                        data[o + 2] = 168;
                                        data[o + 3] = 255;
                                    }
                                }
                            }
                        }
                    }
                }
                ctx.putImageData(image, 0, 0);

                {
                    // Part edges first (creasings between surfaces whose normals disagree), then
                    // silhouettes (neighbouring pixels whose depths disagree). Both land in one
                    // pass over the buffers so the outline stays a pixel or two thick.
                    const edges = ctx.getImageData(0, 0, width, height);
                    const out = edges.data;
                    // A crease is two surfaces meeting at a real angle, not the seam between two
                    // facets of one curved surface. A detailed thread is dozens of flanks barely
                    // 60 degrees apart; outlining those painted the whole thread into a blot.
                    const crease = opts.outline ? 0.75 : 0.5;
                    const jump = opts.outline ? 0.02 : 0.05;
                    const shadeDark = opts.outline ? [64, 80, 96] : [120, 132, 146];
                    const dot = (left, right) => (
                        left[0] * right[0] + left[1] * right[1] + left[2] * right[2]
                    );
                    const paint = (idx, colour) => {
                        const o = idx * 4;
                        out[o] = colour[0];
                        out[o + 1] = colour[1];
                        out[o + 2] = colour[2];
                    };
                    for (let y = 0; y < height; y += 1) {
                        const row = y * width;
                        for (let x = 0; x < width; x += 1) {
                            const idx = row + x;
                            const here = owner[idx];
                            if (here < 0) continue;
                            const z = zbuf[idx];
                            let isEdge = false;
                            if (x + 1 < width) {
                                const next = idx + 1;
                                const other = owner[next];
                                if (other < 0) isEdge = true;
                                else if (other !== here) {
                                    const zn = zbuf[next];
                                    if (Math.abs(z - zn) > jump) isEdge = true;
                                    else if (Math.abs(dot(faces[here][4], faces[other][4])) < crease) isEdge = true;
                                }
                            }
                            if (!isEdge && y + 1 < height) {
                                const next = idx + width;
                                const other = owner[next];
                                if (other < 0) isEdge = true;
                                else if (other !== here) {
                                    const zn = zbuf[next];
                                    if (Math.abs(z - zn) > jump) isEdge = true;
                                    else if (Math.abs(dot(faces[here][4], faces[other][4])) < crease) isEdge = true;
                                }
                            }
                            if (isEdge) paint(idx, shadeDark);
                        }
                    }
                    ctx.putImageData(edges, 0, 0);
                }
            }

            if (opts.bbox) {
                const min = opts.bbox.min;
                const max = opts.bbox.max;
                const center = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
                const span = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2]) || 1;
                const unit = 2 / span;
                const corner = (i, j, k) => project(
                    ((i ? max[0] : min[0]) - center[0]) * unit,
                    ((j ? max[1] : min[1]) - center[1]) * unit,
                    ((k ? max[2] : min[2]) - center[2]) * unit,
                );
                const edges = [
                    [[0,0,0],[1,0,0]], [[0,0,1],[1,0,1]], [[0,1,0],[1,1,0]], [[0,1,1],[1,1,1]],
                    [[0,0,0],[0,0,1]], [[1,0,0],[1,0,1]], [[0,1,0],[0,1,1]], [[1,1,0],[1,1,1]],
                    [[0,0,0],[0,1,0]], [[1,0,0],[1,1,0]], [[0,0,1],[0,1,1]], [[1,0,1],[1,1,1]],
                ];
                ctx.save();
                ctx.setLineDash([5 * dpr, 4 * dpr]);
                ctx.strokeStyle = '#2f7dd1';
                ctx.lineWidth = Math.max(1, dpr * 0.9);
                for (const [from, to] of edges) {
                    const p0 = corner(from[0], from[1], from[2]);
                    const p1 = corner(to[0], to[1], to[2]);
                    ctx.beginPath();
                    ctx.moveTo(p0[0], p0[1]);
                    ctx.lineTo(p1[0], p1[1]);
                    ctx.stroke();
                }
                ctx.restore();
            }

            // Drag handles: the three world axes through the origin. Grabbing one spins the model
            // about that single axis, the way NX's component handles do.
            if (opts.axes) {
                const active = typeof opts.axes === 'string' ? opts.axes : null;
                const origin = project(0, 0, 0);
                ctx.save();
                ctx.lineCap = 'round';
                for (let i = 0; i < AXIS_HANDLES.length; i += 1) {
                    const handle = AXIS_HANDLES[i];
                    const tip = project(
                        handle.dir[0] * AXIS_REACH, handle.dir[1] * AXIS_REACH, handle.dir[2] * AXIS_REACH);
                    const on = active === handle.id;
                    ctx.strokeStyle = handle.color;
                    ctx.globalAlpha = on ? 1 : 0.55;
                    ctx.lineWidth = (on ? 3.4 : 2) * dpr;
                    ctx.beginPath();
                    ctx.moveTo(origin[0], origin[1]);
                    ctx.lineTo(tip[0], tip[1]);
                    ctx.stroke();
                    ctx.globalAlpha = on ? 1 : 0.75;
                    ctx.fillStyle = handle.color;
                    ctx.beginPath();
                    ctx.arc(tip[0], tip[1], (on ? 5.5 : 4) * dpr, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.fillStyle = '#ffffff';
                    ctx.font = (10 * dpr) + 'px ui-monospace, Menlo, Consolas, monospace';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(handle.label, tip[0], tip[1] + 0.5 * dpr);
                }
                ctx.restore();
            }
        }

        /** Canvas viewer: fetch the STL, then rotate/zoom it with the pointer. */
        const VIEW_PRESETS = [
            { id: 'iso', label: 'ISO', rot: rotEuler(0.45, 0.72) },
            { id: 'front', label: 'FRONT', rot: rotEuler(0, 0) },
            { id: 'right', label: 'RIGHT', rot: rotEuler(0, -Math.PI / 2) },
            { id: 'top', label: 'TOP', rot: rotEuler(Math.PI / 2, 0) },
        ];

        const viewerCss = {
            frame: {
                position: 'relative', width: '100%', height: 'clamp(320px, 58vh, 680px)', borderRadius: 10, overflow: 'hidden',
                border: '1px solid rgba(127,127,127,0.28)', background: '#eef1f4',
            },
            canvas: {
                position: 'absolute', left: 0, top: 0, width: '100%', height: '100%',
                display: 'block', touchAction: 'none',
            },
            cube: {
                position: 'absolute', top: 10, right: 10, width: 54, height: 54, borderRadius: 8,
                background: 'rgba(255,255,255,0.9)', border: '1px solid rgba(0,0,0,0.08)',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                gap: 1, cursor: 'pointer', boxShadow: '0 1px 4px rgba(0,0,0,0.08)', userSelect: 'none',
            },
            cubeLabel: { fontSize: 8, letterSpacing: 0.4, color: '#5b6b7a' },
            bar: {
                position: 'absolute', left: 10, right: 10, bottom: 10, display: 'flex', gap: 2,
                padding: '5px 8px', background: 'rgba(255,255,255,0.94)',
                border: '1px solid rgba(0,0,0,0.08)', borderRadius: 999, overflowX: 'auto',
                boxShadow: '0 2px 10px rgba(0,0,0,0.10)', alignItems: 'center',
            },
            barButton: {
                border: 'none', background: 'transparent', padding: '6px 10px', borderRadius: 999,
                fontSize: 12, cursor: 'pointer', whiteSpace: 'nowrap', color: '#2b3a48',
            },
            barButtonOn: { background: '#e2ecfb', color: '#1b5fb0', fontWeight: 600 },
            info: {
                position: 'absolute', right: 10, bottom: 58, width: 236, padding: '10px 12px',
                background: 'rgba(255,255,255,0.96)', border: '1px solid rgba(0,0,0,0.08)',
                borderRadius: 10, fontSize: 12, boxShadow: '0 2px 10px rgba(0,0,0,0.10)',
                color: '#26333f',
            },
            infoRow: {
                display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
                padding: '3px 0',
            },
            infoKey: { color: '#6b7a88', whiteSpace: 'nowrap' },
            infoValue: { fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 11, flex: 1, textAlign: 'right' },
            copy: { border: 'none', background: 'transparent', cursor: 'pointer', color: '#7d8b98', padding: 0, fontSize: 12 },
            clip: {
                position: 'absolute', left: 12, bottom: 58, width: 210, padding: '8px 10px',
                background: 'rgba(255,255,255,0.96)', border: '1px solid rgba(0,0,0,0.08)',
                borderRadius: 10, fontSize: 12, boxShadow: '0 2px 10px rgba(0,0,0,0.10)', color: '#26333f',
            },
            slider: { width: '100%', margin: '6px 0 0' },
            hint: { position: 'absolute', left: 12, top: 10, fontSize: 11, color: '#7d8b98' },
            lockBar: {
                position: 'absolute', top: 74, right: 10, display: 'flex', flexDirection: 'column',
                alignItems: 'center', gap: 4, padding: '6px 5px', borderRadius: 9,
                background: 'rgba(255,255,255,0.92)', border: '1px solid rgba(0,0,0,0.08)',
            },
            lockHint: { fontSize: 9, color: '#8a99a8', letterSpacing: 0.4 },
            lockButton: {
                width: 26, height: 22, borderRadius: 6, border: '1px solid rgba(0,0,0,0.12)',
                background: '#ffffff', fontSize: 11, fontWeight: 600, cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
            },
        };

        function formatNumber(value) {
            const number = Number(value);
            if (!Number.isFinite(number)) return '—';
            if (Math.abs(number) >= 1000) return number.toFixed(2);
            if (Math.abs(number) >= 1) return number.toFixed(3);
            return number.toFixed(4);
        }

        /** Model viewer in the shape of a JLC-style preview: grey stage, view cube, info card, tool bar. */
        function NxViewer(props) {
            const [canvas, setCanvas] = React.useState(null);
            const [frame, setFrame] = React.useState(null);
            const [mesh, setMesh] = React.useState(null);
            const [error, setError] = React.useState(null);
            const [view, setView] = React.useState({ rot: VIEW_ISO, zoom: 1, panX: 0, panY: 0 });
            const [preset, setPreset] = React.useState('iso');
            const [flags, setFlags] = React.useState({ wireframe: false, transparent: false, outline: false, flat: false, bbox: false, axes: true, info: true, toolbar: true });
            const [mode, setMode] = React.useState('rotate');
            const [clipMode, setClipMode] = React.useState(-1);
            const [clipValue, setClipValue] = React.useState(1);
            const [drag, setDrag] = React.useState(null);
            const [hoverAxis, setHoverAxis] = React.useState(null);
            const [lockAxis, setLockAxis] = React.useState(null);
            const [canvasSize, setCanvasSize] = React.useState(0);
            const [copied, setCopied] = React.useState(null);
            const [debug, setDebug] = React.useState('');

            React.useEffect(() => {
                let cancelled = false;
                setMesh(null);
                setError(null);
                fetch(props.url, { cache: 'no-store' })
                    .then((response) => response.arrayBuffer())
                    .then((buffer) => {
                        if (cancelled) return;
                        const parsed = readStl(buffer);
                        if (!parsed) setError('STL 解析失败');
                        else setMesh(parsed);
                    })
                    .catch((failure) => { if (!cancelled) setError(String(failure && failure.message ? failure.message : failure)); });
                return () => { cancelled = true; };
            }, [props.url]);

            React.useEffect(() => {
                if (!mesh || !canvas) return;
                try {
                    paint(canvas, mesh.points, {
                        rot: view.rot, zoom: view.zoom, panX: view.panX, panY: view.panY,
                        background: '#eef1f4',
                        wireframe: flags.wireframe,
                        transparent: flags.transparent,
                        outline: flags.outline,
                        flat: flags.flat,
                        bbox: flags.bbox ? mesh.metrics : null,
                        axes: flags.axes ? (drag && drag.axis ? drag.axis : (hoverAxis || lockAxis)) : null,
                        clip: clipMode >= 0 ? { axis: clipMode, value: clipValue } : null,
                    });
                    setDebug(canvas.width + '×' + canvas.height + ' · css ' + canvas.clientWidth + '×'
                        + canvas.clientHeight + ' · ' + Math.floor(mesh.points.length / 9) + ' 面');
                    if (props.onDebug) {
                        props.onDebug(canvas.width + '×' + canvas.height + ' · css ' + canvas.clientWidth + '×'
                            + canvas.clientHeight + ' · ' + Math.floor(mesh.points.length / 9) + ' 面');
                    }
                } catch (failure) {
                    setError(String(failure && failure.message ? failure.message : failure));
                }
            }, [mesh, canvas, view, flags, clipMode, clipValue, drag, hoverAxis, canvasSize]);

            // A canvas keeps the bitmap it was last painted into, so resizing the panel only
            // stretches those pixels. Repaint at the new size instead of letting CSS scale it.
            React.useEffect(() => {
                if (!canvas || typeof ResizeObserver === 'undefined') return undefined;
                const observer = new ResizeObserver((entries) => {
                    const rect = entries[0] && entries[0].contentRect;
                    if (!rect) return;
                    setCanvasSize(Math.round(rect.width) * 4096 + Math.round(rect.height));
                });
                observer.observe(canvas);
                return () => observer.disconnect();
            }, [canvas]);

            function toggle(name) {
                setFlags((current) => Object.assign({}, current, { [name]: !current[name] }));
            }

            function applyPreset(next) {
                setPreset(next.id);
                setView({ rot: next.rot, zoom: view.zoom, panX: 0, panY: 0 });
            }

            function resetView() {
                setPreset('iso');
                setView({ rot: VIEW_ISO, zoom: 1, panX: 0, panY: 0 });
            }

            /** Screen-space axis handles, so a pointer can be tested against the same lines the
             * renderer drew (model space →canvas pixels, no hit geometry of its own). */
            function axisTips() {
                if (!canvas || !flags.axes) return [];
                const width = canvas.clientWidth || 640;
                const height = canvas.clientHeight || 320;
                const scale = Math.min(width, height) * 0.45 * view.zoom;
                const midX = width / 2 + view.panX, midY = height / 2 - view.panY;
                const rot = view.rot || VIEW_ISO;
                const tips = [];
                for (let i = 0; i < AXIS_HANDLES.length; i += 1) {
                    const handle = AXIS_HANDLES[i];
                    const x = handle.dir[0] * AXIS_REACH, y = handle.dir[1] * AXIS_REACH, z = handle.dir[2] * AXIS_REACH;
                    const px = rot[0] * x + rot[1] * y + rot[2] * z;
                    const py = rot[3] * x + rot[4] * y + rot[5] * z;
                    tips.push({ id: handle.id, x: midX + px * scale, y: midY - py * scale, depth: rot[6] * x + rot[7] * y + rot[8] * z });
                }
                tips.push({ id: null, x: midX, y: midY, depth: 0 });
                return tips;
            }

            function hitAxis(event) {
                if (!flags.axes) return null;
                const rect = canvas.getBoundingClientRect();
                const px = event.clientX - rect.left;
                const py = event.clientY - rect.top;
                let best = null;
                let bestDistance = AXIS_HIT_PX;
                const tips = axisTips();
                const origin = tips[tips.length - 1];
                for (let i = 0; i < tips.length - 1; i += 1) {
                    const tip = tips[i];
                    const distance = distanceToSegment(px, py, origin.x, origin.y, tip.x, tip.y);
                    if (distance < bestDistance) { bestDistance = distance; best = tip.id; }
                }
                return best;
            }

            function onPointerDown(event) {
                try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* ignore */ }
                const axis = hitAxis(event);
                setDrag({ x: event.clientX, y: event.clientY, panX: view.panX, panY: view.panY, axis });
            }
            function onPointerMove(event) {
                if (!drag) {
                    if (flags.axes) setHoverAxis(hitAxis(event));
                    return;
                }
                const dx = event.clientX - drag.x;
                const dy = event.clientY - drag.y;
                if (mode === 'pan') {
                    setView((current) => Object.assign({}, current, { panX: drag.panX + dx, panY: drag.panY - dy }));
                    return;
                }
                setPreset('free');
                if (drag.axis) {
                    // Spin about the handle's own axis: take the axis into view space with the
                    // current rotation, then pre-multiply by a rotation about exactly that
                    // direction. The handle therefore stays put while the part turns.
                    const handle = AXIS_HANDLES.find((item) => item.id === drag.axis);
                    const delta = handle.drag === 'dy' ? dy : dx;
                    const angle = handle.sign * delta * 0.0048;
                    setView((current) => {
                        const base = current.rot || VIEW_ISO;
                        const axisInView = rotApply(base, handle.dir);
                        return Object.assign({}, current, { rot: rotMul(rotAbout(axisInView, angle), base) });
                    });
                } else {
                    setView((current) => Object.assign({}, current, {
                        rot: rotMul(rotAxis('x', dy * 0.004), rotMul(rotAxis('y', dx * 0.004), current.rot || VIEW_ISO)),
                    }));
                }
            }
            function stopDrag() { setDrag(null); }
            function onWheel(event) {
                event.preventDefault();
                const direction = event.deltaY > 0 ? 0.92 : 1.08;
                setView((current) => Object.assign({}, current, {
                    zoom: Math.max(0.35, Math.min(6, current.zoom * direction)),
                }));
            }

            // React registers wheel handlers as passive, so preventDefault() there is ignored and
            // the page scrolls while the model zooms. Bind natively instead.
            React.useEffect(() => {
                if (!canvas) return undefined;
                canvas.addEventListener('wheel', onWheel, { passive: false });
                return () => canvas.removeEventListener('wheel', onWheel);
            }, [canvas]);

            function copyText(label, text) {
                try {
                    void navigator.clipboard.writeText(text);
                    setCopied(label);
                    window.setTimeout(() => setCopied(null), 1200);
                } catch { /* clipboard blocked */ }
            }

            function snapshot() {
                if (!canvas) return;
                try {
                    const link = document.createElement('a');
                    link.href = canvas.toDataURL('image/png');
                    link.download = String(props.name || 'nx-model').replace(/\.[^.]+$/, '') + '.png';
                    link.click();
                } catch (failure) {
                    setError(String(failure && failure.message ? failure.message : failure));
                }
            }

            function toggleFullscreen() {
                try {
                    if (!document.fullscreenElement) void frame.requestFullscreen();
                    else void document.exitFullscreen();
                } catch { /* fullscreen refused */ }
            }

            const metrics = mesh ? mesh.metrics : null;
            const presetLabel = (VIEW_PRESETS.find((item) => item.id === preset) || { label: 'FREE' }).label;
            const clipNames = ['', 'X', 'Y', 'Z'];

            const barButtons = [
                ['回到原点', false, resetView, '恢复到等轴测视角与 100% 缩放'],
                ['旋转', mode === 'rotate', () => setMode('rotate'), '拖拽空白处自由旋转；按住红/绿/蓝轴手柄则只绕那一根轴转'],
                ['平移', mode === 'pan', () => setMode('pan'), '拖拽平移视图'],
                ['轴手柄', flags.axes, () => toggle('axes'), '显示 / 隐藏 X Y Z 单轴旋转手柄'],
                ['剖切', clipMode >= 0, () => setClipMode((current) => (current >= 2 ? -1 : current + 1)), '沿 X / Y / Z 依次剖切，再点一次关闭'],
                ['包围盒', flags.bbox, () => toggle('bbox'), '显示包围盒与尺寸'],
                ['线框', flags.wireframe, () => toggle('wireframe'), '只看三角面边线'],
                ['透明', flags.transparent, () => toggle('transparent'), '半透明显示'],
                ['轮廓线', flags.outline, () => toggle('outline'), '加深面边界'],
                ['无阴影', flags.flat, () => toggle('flat'), '关闭明暗过渡'],
                ['基本信息', flags.info, () => toggle('info'), '模型名称 / 体积 / 表面积 / 包围盒'],
                ['截图', false, snapshot, '把当前视图存成 PNG'],
                ['全屏', false, toggleFullscreen, '全屏查看'],
                ['收起', !flags.toolbar, () => toggle('toolbar'), '收起工具条'],
            ];

            // `height: 100%` collapses to zero whenever the panel does not constrain its
            // height, and the canvas is absolutely positioned inside this box, so the viewer
            // would render nothing at all. Give the full-page mode a real height instead.
            const frameStyle = props.fill
                ? Object.assign({}, viewerCss.frame, { height: 'min(72vh, 760px)', minHeight: 380 })
                : viewerCss.frame;

            return h('div', { ref: setFrame, style: frameStyle },
                h('canvas', {
                    ref: setCanvas,
                    onPointerDown,
                    onPointerMove,
                    onPointerUp: stopDrag,
                    onPointerLeave: () => { stopDrag(); setHoverAxis(null); },
                    style: Object.assign({}, viewerCss.canvas, {
                        overscrollBehavior: 'contain',
                        cursor: drag ? 'grabbing' : (hoverAxis ? 'grab' : (mode === 'pan' ? 'move' : 'grab')),
                    }),
                }),
                mesh && !error ? null : h('div', { style: Object.assign({}, viewerCss.hint, { top: '46%', left: 0, right: 0, textAlign: 'center' }) },
                    error ? '渲染失败：' + error : '正在载入网格…'),
                debug
                    ? h('div', {
                        style: Object.assign({}, viewerCss.hint, { left: 8, bottom: 4, fontSize: 10, opacity: 0.55, pointerEvents: 'none' }),
                    }, debug)
                    : null,
                mesh && flags.axes && !error
                    ? h('div', { style: Object.assign({}, viewerCss.hint, { left: 12, bottom: 58, maxWidth: '58%' }) },
                        hoverAxis || drag && drag.axis
                            ? '已锁定 ' + String(hoverAxis || drag.axis).toUpperCase() + ' 轴 · 拖动只绕该轴旋转'
                            : (lockAxis ? '已锁定 ' + String(lockAxis).toUpperCase() + ' 轴 · 拖动任意位置只绕该轴旋转（再点一次右上按钮取消）' : '拖动空白处自由旋转 · 按住 X / Y / Z 轴手柄只绕该轴旋转 · 右上角可锁定单轴 · 滚轮缩放'))
                    : null,
                h('div', {
                    style: viewerCss.cube,
                    title: '点击切换视角',
                    onClick: () => {
                        const index = VIEW_PRESETS.findIndex((item) => item.id === preset);
                        applyPreset(VIEW_PRESETS[(index + 1) % VIEW_PRESETS.length]);
                    },
                },
                h('svg', { width: 26, height: 26, viewBox: '0 0 32 32', 'aria-hidden': true },
                    h('path', { d: 'M16 4 L28 10 L28 22 L16 28 L4 22 L4 10 Z', fill: '#f2f5f9', stroke: '#8a99a8', strokeWidth: 1.2 }),
                    h('path', { d: 'M16 4 L28 10 L16 16 L4 10 Z', fill: '#dde5ee', stroke: '#8a99a8', strokeWidth: 1.1 }),
                    h('path', { d: 'M16 16 L28 10 L28 22 L16 28 Z', fill: '#eef2f7', stroke: '#8a99a8', strokeWidth: 1.1 }),
                    h('path', { d: 'M16 16 L4 10 L4 22 L16 28 Z', fill: '#e6ecf4', stroke: '#8a99a8', strokeWidth: 1.1 }),
                ),
                h('span', { style: viewerCss.cubeLabel }, presetLabel),
                ),
                mesh && flags.axes && !error
                    ? h('div', { style: viewerCss.lockBar },
                        h('div', { style: viewerCss.lockHint }, lockAxis ? '锁定中' : '锁定轴'),
                        AXIS_HANDLES.map((handle) => h('button', {
                            key: handle.id,
                            title: lockAxis === handle.id ? '取消锁定，恢复自由旋转' : '锁定后拖动任意位置都只绕 ' + handle.id.toUpperCase() + ' 轴旋转',
                            onClick: () => setLockAxis((current) => (current === handle.id ? null : handle.id)),
                            style: Object.assign({}, viewerCss.lockButton, lockAxis === handle.id
                                ? { color: '#ffffff', background: handle.color, borderColor: handle.color }
                                : { color: handle.color, borderColor: 'rgba(0,0,0,0.12)' }),
                        }, handle.label)),
                    )
                    : null,
                flags.info && metrics
                    ? h('div', { style: viewerCss.info },
                        h('div', { style: { fontWeight: 600, marginBottom: 6 } }, '基本信息'),
                        h('div', { style: viewerCss.infoRow },
                            h('span', { style: viewerCss.infoKey }, '模型名称'),
                            h('span', { style: viewerCss.infoValue }, String(props.name || '')),
                            h('button', { style: viewerCss.copy, title: '复制', onClick: () => copyText('name', String(props.name || '')) }, copied === 'name' ? '✓' : '⧉'),
                        ),
                        h('div', { style: viewerCss.infoRow },
                            h('span', { style: viewerCss.infoKey }, metrics.closed ? '体积' : '类型'),
                            h('span', { style: viewerCss.infoValue },
                                metrics.closed
                                    ? formatNumber(metrics.volume) + ' mm³'
                                    : '片体 / 开放网格（无体积）'),
                            metrics.closed
                                ? h('button', { style: viewerCss.copy, title: '复制', onClick: () => copyText('volume', formatNumber(metrics.volume)) }, copied === 'volume' ? '✓' : '⧉')
                                : null,
                        ),
                        h('div', { style: viewerCss.infoRow },
                            h('span', { style: viewerCss.infoKey }, '表面积'),
                            h('span', { style: viewerCss.infoValue }, formatNumber(metrics.area) + ' mm²'),
                            h('button', { style: viewerCss.copy, title: '复制', onClick: () => copyText('area', formatNumber(metrics.area)) }, copied === 'area' ? '✓' : '⧉'),
                        ),
                        h('div', { style: viewerCss.infoRow },
                            h('span', { style: viewerCss.infoKey }, '包围盒'),
                            h('span', { style: viewerCss.infoValue },
                                metrics.size.map((value) => Number(value).toFixed(2)).join(' × ') + ' mm'),
                        ),
                        h('div', { style: viewerCss.infoRow },
                            h('span', { style: viewerCss.infoKey }, '三角面'),
                            h('span', { style: viewerCss.infoValue }, String(metrics.triangles)),
                        ))
                    : null,
                clipMode >= 0
                    ? h('div', { style: viewerCss.clip },
                        h('div', null, '剖切 · ' + clipNames[clipMode + 1] + ' 轴'),
                        h('input', {
                            type: 'range', min: -1, max: 1, step: 0.01, value: clipValue,
                            style: viewerCss.slider,
                            onChange: (event) => setClipValue(Number(event.target.value)),
                        }))
                    : null,
                flags.toolbar
                    ? h('div', { style: viewerCss.bar },
                        barButtons.map(([label, active, handler, title]) => h('button', {
                            key: label,
                            type: 'button',
                            title,
                            style: active ? Object.assign({}, viewerCss.barButton, viewerCss.barButtonOn) : viewerCss.barButton,
                            onClick: handler,
                        }, label)))
                    : h('button', {
                        type: 'button',
                        style: Object.assign({}, viewerCss.barButton, viewerCss.barButtonOn, {
                            position: 'absolute', right: 12, bottom: 12, background: 'rgba(255,255,255,0.94)',
                            border: '1px solid rgba(0,0,0,0.08)', boxShadow: '0 2px 10px rgba(0,0,0,0.10)',
                        }),
                        onClick: () => toggle('toolbar'),
                    }, '展开工具条'));
        }

        function NxIcon(props) {
            const size = Number(props && props.size) || 18;
            return h('svg', {
                viewBox: '0 0 24 24', width: size, height: size, 'aria-hidden': true,
                style: { display: 'block', pointerEvents: 'none', flex: '0 0 auto' },
            },
            h('rect', { x: 1, y: 1, width: 22, height: 22, rx: 6, fill: '#2f7dd1' }),
            h('path', {
                d: 'M6.4 16.8V7.2L10.6 16.8V7.2',
                fill: 'none', stroke: '#ffffff', strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round',
            }),
            h('path', {
                d: 'M14.2 7.2L19.4 16.8M19.4 7.2L14.2 16.8',
                fill: 'none', stroke: '#ffffff', strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round',
            }));
        }

        /** Shows the viewer's crash instead of an empty white box. */
        class ViewerBoundary extends React.Component {
            constructor(props) {
                super(props);
                this.state = { err: null };
            }

            static getDerivedStateFromError(err) {
                return { err };
            }

            render() {
                if (this.state.err) {
                    return h('div', { style: { padding: 12, fontSize: 12, color: '#c34a3f', whiteSpace: 'pre-wrap' } },
                        '查看器崩溃：' + String((this.state.err && this.state.err.message) || this.state.err));
                }
                return this.props.children;
            }
        }

        /**
         * The workbench buttons, grouped by what the user is trying to get. Every entry maps to
         * one whitelisted bridge command; `file: true` marks the ones that hand back a document,
         * whose path the panel then shows as a chip instead of leaving the user to hunt for it.
         */
        /** Sheet sizes the 建图纸 button offers; the same choice is passed to PDF / 明细表. */
        const SHEET_SIZE_CHOICES = ['A4', 'A3', 'A2', 'A1'];

        /** BOM columns the 明细表 + CSV button can draw (ids match nxworker BOM_FIELDS). */
        const BOM_COLUMN_CHOICES = [
            { id: 'no', zh: '序号', en: 'No.' },
            { id: 'part', zh: '零件号', en: 'Part No.' },
            { id: 'name', zh: '名称', en: 'Name' },
            { id: 'qty', zh: '数量', en: 'Qty' },
            { id: 'unit', zh: '单重', en: 'Unit kg' },
            { id: 'total', zh: '总重', en: 'Total kg' },
            { id: 'volume', zh: '体积', en: 'Vol mm3' },
        ];
        const BOM_DEFAULT_COLUMNS = ['no', 'part', 'name', 'qty', 'unit', 'total'];

        const ACTION_GROUPS = [
            {
                title: '出图',
                items: [
                    { label: '建图纸', command: 'drawing', needsSize: true, title: '没有图纸时新建；已有带视图的图纸则直接复用' },
                    { label: '打印 PDF', command: 'pdf', file: true, needsSize: true, title: '打印有内容的图纸（没有图纸时自动建一张）' },
                    { label: '剖视图', command: 'section_view', args: { parent: 'TOP', start: [-10, 40, 10], end: [130, 40, 10] }, title: '在俯视图上沿 y=40 剖一刀，放不下时自动换剖切类型' },
                    { label: '技术要求', command: 'note', args: { text: '技术要求\n1. 未注圆角 R2。\n2. 去毛刺锐边。', at: [18, 268] }, title: '在当前图纸左上角写一段中文技术要求' },
                ],
            },
            {
                title: '文件',
                items: [
                    { label: 'STEP', command: 'export', args: { format: 'step' }, file: true },
                    { label: 'IGES', command: 'export', args: { format: 'iges' }, file: true },
                    { label: 'STL', command: 'export', args: { format: 'stl' }, file: true },
                    { label: '保存零件', command: 'save', title: '保存到零件自己的路径' },
                ],
            },
            {
                title: '装配',
                items: [
                    { label: '装配清单 + CSV', command: 'asm_list', file: true, title: '列出组件并写出 _components.csv' },
                    { label: '明细表 + CSV', command: 'bom_table', file: true, needsSize: true, needsBom: true, title: '在图纸上画明细表，同时写出同名的 _bom.csv' },
                    { label: '间隙检查', command: 'asm_clearance', args: { tolerance: 0.01 } },
                    { label: '爆炸图', command: 'explode', args: { distance: 20, stagger: 6 }, title: '把组件沿 Z 逐级炸开（可再用命令还原）' },
                    { label: '批量出图', command: 'batch_drawings', file: true, needsSize: true, title: '给装配里每个零件出一张图纸并打印 PDF' },
                ],
            },
        ];

        function NxPanel() {
            const [state, setState] = React.useState(null);
            const [busy, setBusy] = React.useState('');
            const [failure, setFailure] = React.useState(null);
            const [log, setLog] = React.useState([]);
            const [partPath, setPartPath] = React.useState('');
            const [preview, setPreview] = React.useState(null);
            const [pageView, setPageView] = React.useState(false);
            const [viewerDebug, setViewerDebug] = React.useState('');
            /** Outcome of the last action: { ok, command, ms, text, file }. */
            const [outcome, setOutcome] = React.useState(null);
            /** Drawing sheet size used by 建图纸 / 打印 PDF / 明细表 / 批量出图. */
            const [sheetSize, setSheetSize] = React.useState('A3');
            /** BOM table options: header language and which columns to draw. */
            const [bomLang, setBomLang] = React.useState('zh');
            const [bomColumns, setBomColumns] = React.useState(BOM_DEFAULT_COLUMNS);
            /** Wall clock of the first byte of the running action, for the elapsed label. */
            const [busySince, setBusySince] = React.useState(0);
            const [now, setNow] = React.useState(Date.now());

            React.useEffect(() => {
                if (!busySince) return undefined;
                const timer = window.setInterval(() => { setNow(Date.now()); }, 500);
                return () => { window.clearInterval(timer); };
            }, [busySince]);

            const refresh = React.useCallback(async (probe) => {
                try {
                    const response = await fetch(api('/dsh-nx/state' + (probe ? '?probe=1' : '')), { cache: 'no-store' });
                    const body = await response.json();
                    setState(body);
                    setFailure(null);
                }
                catch (error) {
                    setFailure(String(error && error.message ? error.message : error));
                }
            }, []);

            React.useEffect(() => {
                void refresh(false);
                const timer = window.setInterval(() => { void refresh(false); }, 5000);
                return () => { window.clearInterval(timer); };
            }, [refresh]);

            const run = React.useCallback(async (command, args) => {
                const label = command + ' ' + (args && args.format ? args.format : '');
                setBusy(label);
                setBusySince(Date.now());
                try {
                    const response = await fetch(api('/dsh-nx/action'), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json' },
                        body: JSON.stringify({ command, args: args || {}, path: partPath.trim() }),
                    });
                    const body = await response.json();
                    const record = {
                        at: Date.now(),
                        command: label,
                        ok: body.ok === true,
                        text: body.ok === true ? String(body.summary || (body.ms ? body.ms + ' ms' : 'ok')) : String(body.error ?? 'failed'),
                        file: body.ok === true && body.file ? body.file : null,
                        ms: body.ms || 0,
                    };
                    setOutcome(record);
                    setLog((previous) => [record].concat(previous).slice(0, 18));
                    await refresh(true);
                }
                catch (error) {
                    const record = {
                        at: Date.now(), command: label, ok: false, file: null,
                        text: String(error && error.message ? error.message : error),
                    };
                    setOutcome(record);
                    setLog((previous) => [record].concat(previous).slice(0, 18));
                }
                finally {
                    setBusy('');
                    setBusySince(0);
                }
            }, [refresh, partPath]);

            /**
             * Button arguments plus the live options: the sheet size the user picked and, for the
             * BOM table, the header language and the ticked columns. Kept out of ACTION_GROUPS so
             * a button's static args stay readable.
             */
            const argsFor = React.useCallback((item) => {
                const base = Object.assign({}, item.args || {});
                if (item.needsSize) base.size = sheetSize;
                if (item.needsBom) {
                    base.language = bomLang;
                    base.columns = bomColumns.slice();
                }
                return base;
            }, [sheetSize, bomLang, bomColumns]);

            const openPreview = React.useCallback(async (file) => {                setPreview({ name: file.name, loading: true });
                try {
                    const response = await fetch(api('/dsh-nx/preview'), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json' },
                        body: JSON.stringify({ path: file.path }),
                    });
                    const body = await response.json();
                    if (body && body.ok === true) {
                        const entry = {
                            name: file.name,
                            url: body.stl,
                            triangles: body.triangles,
                            bytes: body.bytes,
                            cached: body.cached === true,
                            ms: body.ms,
                            // The server may have adopted a part that is already open in NX when the
                            // path did not resolve (a URL-encoded or workspace-relative name):
                            // say so instead of silently previewing a different file.
                            note: body.note || '',
                        };
                        setPreview(entry);
                        setPageView(true);
                    } else {
                        setPreview({ name: file.name, error: String(body && body.error ? body.error : '预览失败') });
                    }
                }
                catch (error) {
                    setPreview({ name: file.name, error: String(error && error.message ? error.message : error) });
                }
            }, []);

            /** Open the file's folder in Explorer with the file selected. */
            const revealFile = React.useCallback(async (file) => {
                try {
                    const response = await fetch(api('/dsh-nx/reveal'), {
                        method: 'POST',
                        headers: { 'content-type': 'application/json' },
                        body: JSON.stringify({ path: file.path }),
                    });
                    const body = await response.json();
                    if (!body || body.ok !== true) setFailure(String(body && body.error ? body.error : '打开文件夹失败'));
                }
                catch (error) {
                    setFailure(String(error && error.message ? error.message : error));
                }
            }, []);

            const bridge = state && state.bridge ? state.bridge : null;
            const probe = state && state.probe ? state.probe : null;
            const parts = probe && Array.isArray(probe.displayParts) ? probe.displayParts : [];
            const running = Boolean(bridge && bridge.running);
            const artefacts = state && Array.isArray(state.artefacts) ? state.artefacts : [];
            const actionLog = state && Array.isArray(state.actions) ? state.actions : [];

            const pageBg = pageBackground();

            /* Preview page: the viewer replaces the console inside the panel, so it never has to
               punch through the panel's own stacking context, and the wheel belongs to the model
               rather than to the conversation behind it. */
            if (pageView && preview && preview.url) {
                return h('div', {
                    style: {
                        display: 'flex', flexDirection: 'column',
                        width: '100%', height: '100%', minHeight: 360, overflowY: 'auto',
                        background: pageBg, color: 'inherit',
                    },
                },
                    h('div', {
                        style: {
                            display: 'flex', alignItems: 'center', gap: 8,
                            padding: '6px 10px', flex: '0 0 auto',
                            background: pageBg,
                            borderBottom: '1px solid rgba(127,127,127,0.22)',
                        },
                    },
                        h('button', {
                            style: Object.assign({}, css.button, { flex: '0 0 auto', whiteSpace: 'nowrap' }),
                            onClick: () => setPageView(false),
                        }, '\u2190 返回'),
                        h('span', {
                            style: {
                                flex: 1, minWidth: 0, fontSize: 12.5, fontWeight: 600, overflow: 'hidden',
                                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            },
                        }, preview.name),
                        h('span', { style: { flex: '0 0 auto', fontSize: 11, opacity: 0.65, whiteSpace: 'nowrap' } },
                            String(preview.triangles || 0) + ' 面 · ' + bytes(preview.bytes)
                            + ' · ' + (viewerDebug || '未渲染')),
                    ),
                    h('div', { style: { flex: '0 0 auto', padding: 6 } },
                        // The full-height ("fill") mode depended on the panel constraining its
                        // height, and in this sidebar tab it does not: the canvas collapsed to
                        // nothing. Use the same fixed-height frame the inline viewer has always
                        // used, and let the page scroll instead.
                        h(ViewerBoundary, null,
                            h(NxViewer, { url: preview.url, name: preview.name, onDebug: setViewerDebug }))),
                );
            }

            /** Path of the file an action wrote, as a compact chip with the two useful buttons. */
            const fileChip = (file) => {
                if (!file || !file.path) return null;
                const name = String(file.path).split(/[\\/]/).pop();
                return h('div', {
                    style: {
                        display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
                        marginTop: 8, padding: '6px 9px', borderRadius: 8,
                        border: '1px solid rgba(63,163,77,0.45)', background: 'rgba(63,163,77,0.10)',
                    },
                },
                    h('span', { style: css.mono }, name),
                    file.note ? h('span', { style: css.dim }, file.note) : null,
                    h('span', { style: Object.assign({}, css.dim, { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }) }, file.path),
                    PREVIEWABLE.test(name)
                        ? h('button', {
                            style: css.button,
                            title: '在这个面板里旋转查看',
                            onClick: () => { void openPreview({ path: file.path, name }); },
                        }, '预览')
                        : null,
                    h('button', { style: css.button, onClick: () => { void revealFile({ path: file.path }); } }, '文件夹'),
                );
            };

            const busySeconds = busySince ? Math.max(0, Math.round((now - busySince) / 1000)) : 0;
            const recentCut = Date.now() - 120000;

            return h('div', { style: css.page },
                h('div', { style: css.header },
                    h('span', { style: Object.assign({}, css.row, { gap: 6, flex: 1, minWidth: 0 }) },
                        dot(running ? 'ok' : 'off'),
                        h('span', { style: css.title }, 'NX 工作台'),
                        h('span', { style: Object.assign({}, css.dim, { fontSize: 11.5 }) },
                            running
                                ? (probe ? (probe.release ?? '') + (probe.isBatch ? ' · batch' : '') + (probe.uptimeSeconds ? ' · 在线 ' + Math.round(probe.uptimeSeconds) + ' s' : '') : '会话运行中')
                                : '会话未启动（首个操作会启动，约 25 秒）'),
                    ),
                    h('button', {
                        style: busy ? css.buttonBusy : css.button,
                        onClick: () => { void refresh(true); },
                    }, busy ? '运行中 ' + busySeconds + ' s' : '刷新'),
                    h('button', {
                        style: css.button,
                        title: '在资源管理器里打开产物目录',
                        onClick: () => { if (state && state.outputDir) void revealFile({ path: state.outputDir }); },
                    }, '产物目录'),
                ),
                h('div', { style: css.card },
                    h('div', { style: css.sectionTitle }, '会话'),
                    h('div', { style: Object.assign({}, css.row, { gap: 16 }) },
                        h('span', null, '进程 ', h('b', { style: css.mono }, bridge && bridge.pid ? String(bridge.pid) : '—')),
                        h('span', null, 'NX ', h('span', { style: css.mono }, shortPath(bridge && bridge.nxHome))),
                        h('span', null, '命令 ', h('b', { style: css.mono }, String(bridge && bridge.commands ? bridge.commands : 0))),
                        h('span', null, '桥目录 ', h('span', { style: css.mono }, shortPath(bridge && bridge.root))),
                    ),
                    h('div', { style: Object.assign({}, css.row, { marginTop: 6 }) },
                        h('span', { style: css.dim }, '已打开零件 '),
                        parts.length
                            ? h('span', { style: css.mono }, parts.join('、'))
                            : h('span', { style: css.dim }, '无'),
                    ),
                    h('div', { style: Object.assign({}, css.dim, { marginTop: 4, fontSize: 11 }) }, '注册表 ' + gate),
                    state && state.probeError
                        ? h('div', { style: Object.assign({}, css.row, { marginTop: 6, color: '#c34a3f' }) }, '取状态失败：' + state.probeError)
                        : null,
                ),
                h('div', { style: css.card },
                    h('div', { style: css.sectionTitle }, '打开零件 / 图纸'),
                    h('div', { style: css.row },
                        h('input', {
                            style: css.input,
                            placeholder: 'D:\\path\\part.prt  或  step/igs',
                            value: partPath,
                            onChange: (event) => setPartPath(event.target.value),
                            onKeyDown: (event) => {
                                if (event.key === 'Enter' && partPath.trim()) void run('open', { path: partPath.trim() });
                            },
                        }),
                        h('button', {
                            style: busy ? css.buttonBusy : css.button,
                            onClick: () => { if (partPath.trim()) void run('open', { path: partPath.trim() }); },
                        }, '打开'),
                        h('button', {
                            style: css.button,
                            title: '把这个模型转成 STL，在面板里旋转查看',
                            onClick: () => {
                                const target = partPath.trim();
                                if (!target) return;
                                void openPreview({ path: target, name: target.split(/[\\/]/).pop() });
                            },
                        }, '预览'),
                    ),
                    parts.length
                        ? h('div', { style: Object.assign({}, css.row, { marginTop: 6, gap: 6 }) },
                            h('span', { style: css.dim }, '快速切换：'),
                            parts.slice(0, 6).map((name) => h('button', {
                                key: name,
                                style: css.button,
                                title: '把这个零件的路径填进输入框',
                                onClick: () => setPartPath(name),
                            }, name)))
                        : null,
                ),
                h('div', { style: css.card },
                    ACTION_GROUPS.map((group) => h('div', { key: group.title, style: { marginBottom: 10 } },
                        h('div', { style: css.sectionTitle }, group.title),
                        group.items.some((item) => item.needsSize)
                            ? h('div', { style: Object.assign({}, css.row, { gap: 6, marginBottom: 7 }) },
                                h('span', { style: Object.assign({}, css.dim, { fontSize: 11 }) }, '图纸'),
                                SHEET_SIZE_CHOICES.map((size) => h('button', {
                                    key: size,
                                    style: sheetSize === size ? css.chipOn : css.chipOff,
                                    title: '图纸尺寸 ' + size,
                                    onClick: () => setSheetSize(size),
                                }, size)),
                            )
                            : null,
                        h('div', { style: css.group },
                            group.items.map((item) => h('button', {
                                key: item.label,
                                style: busy ? css.buttonBusy : css.button,
                                title: item.title || item.command,
                                onClick: () => { void run(item.command, argsFor(item)); },
                            }, item.label)),
                        ),
                        group.items.some((item) => item.needsBom)
                            ? h('div', { style: { marginTop: 8 } },
                                h('div', { style: Object.assign({}, css.row, { gap: 6 }) },
                                    h('span', { style: Object.assign({}, css.dim, { fontSize: 11 }) }, '表头'),
                                    h('button', {
                                        style: bomLang === 'zh' ? css.chipOn : css.chipOff,
                                        onClick: () => setBomLang('zh'),
                                    }, '中文'),
                                    h('button', {
                                        style: bomLang === 'en' ? css.chipOn : css.chipOff,
                                        onClick: () => setBomLang('en'),
                                    }, 'EN'),
                                    h('span', { style: Object.assign({}, css.dim, { fontSize: 11, marginLeft: 6 }) }, '列'),
                                    BOM_COLUMN_CHOICES.map((column) => {
                                        const on = bomColumns.indexOf(column.id) >= 0;
                                        return h('button', {
                                            key: column.id,
                                            style: on ? css.chipOn : css.chipOff,
                                            title: on ? '从明细表里去掉这一列' : '把这一列加进明细表',
                                            onClick: () => setBomColumns(on
                                                ? bomColumns.filter((id) => id !== column.id)
                                                : bomColumns.concat([column.id])),
                                        }, bomLang === 'en' ? column.en : column.zh);
                                    }),
                                ),
                                bomColumns.length
                                    ? null
                                    : h('div', { style: Object.assign({}, css.bad, { fontSize: 11, marginTop: 4 }) }, '至少勾一列'),
                            )
                            : null,
                    )),
                ),
                outcome
                    ? h('div', { style: css.card },
                        h('div', { style: css.sectionTitle }, '上一步'),
                        h('div', { style: Object.assign({}, css.row, { gap: 8 }) },
                            dot(outcome.ok ? 'ok' : 'bad'),
                            h('span', { style: css.mono }, outcome.command),
                            h('span', { style: outcome.ok ? css.dim : css.bad }, outcome.text),
                            outcome.ms ? h('span', { style: Object.assign({}, css.dim, { marginLeft: 'auto', whiteSpace: 'nowrap' }) }, Math.round(outcome.ms) + ' ms') : null,
                        ),
                        outcome.ok ? fileChip(outcome.file) : null,
                    )
                    : null,
                h('div', { style: css.card },
                    h('div', { style: Object.assign({}, css.row, { gap: 8 }) },
                        h('span', { style: Object.assign({}, css.sectionTitle, { flex: 1, marginBottom: 0 }) }, '最近产物'),
                        state && state.outputDir
                            ? h('span', { style: Object.assign({}, css.dim, { fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 260 }) }, shortPath(state.outputDir))
                            : null,
                    ),
                    artefacts.length
                        ? h('table', { style: css.table },
                            h('tbody', null, artefacts.map((file) => h('tr', { key: file.path },
                                h('td', { style: Object.assign({}, css.td, css.mono) },
                                    file.modified > recentCut ? h('span', { style: css.good }, '● ') : null,
                                    file.name),
                                h('td', { style: Object.assign({}, css.td, { whiteSpace: 'nowrap', opacity: 0.7 }) }, bytes(file.bytes)),
                                h('td', { style: Object.assign({}, css.td, { whiteSpace: 'nowrap', textAlign: 'right' }) },
                                    h('button', {
                                        type: 'button',
                                        style: PREVIEWABLE.test(file.name)
                                            ? css.button
                                            : Object.assign({}, css.button, { opacity: 0.45, cursor: 'default' }),
                                        disabled: !PREVIEWABLE.test(file.name),
                                        title: PREVIEWABLE.test(file.name)
                                            ? '在面板里旋转查看这个模型'
                                            : '这个格式不能预览，用「文件夹」看原文件',
                                        onClick: PREVIEWABLE.test(file.name) ? () => { void openPreview(file); } : undefined,
                                    }, preview && preview.name === file.name ? '查看中' : '预览'),
                                    ' ',
                                    h('button', {
                                        type: 'button',
                                        style: css.button,
                                        title: '在资源管理器里打开所在文件夹',
                                        onClick: () => { void revealFile(file); },
                                    }, '文件夹'),
                                ),
                            ))))
                        : h('div', { style: css.dim }, '还没有产物。点上面的「STEP / 打印 PDF / BOM CSV」任一按钮，文件会落在产物目录并出现在这里。'),
                ),
                preview
                    ? h('div', { style: css.card },
                        h('div', { style: css.row },
                            h('span', { style: Object.assign({}, css.sectionTitle, { flex: 1 }) }, '预览 · ' + preview.name),
                            preview.url
                                ? h('button', {
                                    style: Object.assign({}, css.button, { flex: '0 0 auto', whiteSpace: 'nowrap' }),
                                    title: '在面板里全屏查看这个模型',
                                    onClick: () => { setPageView(true); setFailure(null); },
                                }, '预览')
                                : null,
                            h('button', { style: css.button, onClick: () => setPreview(null) }, '关闭')),
                        preview.loading
                            ? h('div', { style: css.dim }, '正在用 NX 转成 STL 预览…（首次 10–40 秒，之后走缓存）')
                            : null,
                        preview.error ? h('div', { style: css.bad }, '预览失败：' + preview.error) : null,
                        preview.note
                            ? h('div', { style: Object.assign({}, css.dim, { marginTop: 6 }) }, '※ ' + preview.note)
                            : null,
                        preview.url
                            ? h('div', { style: Object.assign({}, css.dim, { marginTop: 6 }) },
                                String(preview.triangles || 0) + ' 三角面 · ' + bytes(preview.bytes)
                                + (preview.cached ? ' · 缓存命中' : ' · 转换 ' + Math.round((preview.ms || 0) / 1000) + ' s')
                                + ' · 已在「NX 预览」标签页打开')
                            : null)
                    : null,
                actionLog.length
                    ? h('div', { style: css.card },
                        h('div', { style: css.sectionTitle }, '操作记录'),
                        h('table', { style: css.table },
                            h('tbody', null, actionLog.map((entry, index) => h('tr', { key: String(entry.at) + index },
                                h('td', { style: css.td }, dot(entry.ok ? 'ok' : 'bad')),
                                h('td', { style: Object.assign({}, css.td, css.mono) }, entry.command),
                                h('td', { style: Object.assign({}, css.td, css.dim) }, String(entry.summary ?? '')),
                                h('td', { style: Object.assign({}, css.td, { whiteSpace: 'nowrap', opacity: 0.55 }) }, entry.ms ? Math.round(entry.ms) + ' ms' : ''),
                            )))))
                    : null,
                failure ? h('div', { style: Object.assign({}, css.card, { color: '#c34a3f' }) }, '面板请求失败：' + failure) : null,
            );
        }

        /** Formats the host can turn into a mesh preview. */
        const PREVIEWABLE = /\.(prt|step|stp|iges|igs|stl)$/i;

        /** The right Sidebar mints a tab per implementation id; the same id keys its body. */
        const TAB_ID = 'dsh-nx/console';
        const TAB_KIND = 'nx-console';
        /** Second tab type: the model viewer, opened on its own so it owns the whole column. */
        const VIEW_TAB_ID = 'dsh-nx/viewer';
        const VIEW_KIND = 'nx-viewer';

        /** Entry the viewer tab should show; set right before the tab is opened. */
        let pendingView = null;

        /** Document-type id for .prt files (the second channel into the preview tab). */
        const PRT_DOCUMENT_ID = 'dsh-nx/prt';

        /** Which registries this build actually offers — shown on the console so a miss is visible. */
        let gate = 'viewer ? · docpreview ?';

        /** Set by apply(): switches the right column to a tab kind. Returns false when it cannot. */
        let openTabRef = null;

        /** Show an entry in the dedicated viewer tab; false when this build has no such tab. */
        function openViewerPage(entry) {
            pendingView = entry || null;
            if (!openTabRef) return false;
            return openTabRef(VIEW_KIND) === true;
        }

        /** Model viewer page — a full-height tab of its own, no page inside it to scroll. */
        function NxViewerPage() {
            const [entry, setEntry] = React.useState(pendingView);
            React.useEffect(() => { setEntry(pendingView); }, []);
            if (!entry || !entry.url) {
                return h('div', { style: { padding: '18px 16px', fontSize: 12.5, opacity: 0.7 } },
                    '没有待预览的模型。回到「NX 控制台」，在最近产物里点「预览」。');
            }
            return h('div', { style: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, padding: '10px 10px 8px' } },
                h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 } },
                    h('span', { style: { flex: 1, fontSize: 12.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, entry.name),
                    h('span', { style: { fontSize: 11, opacity: 0.65 } },
                        String(entry.triangles || 0) + ' 面 · ' + bytes(entry.bytes)),
                ),
                h('div', { style: { flex: '0 0 auto' } },
                    h(ViewerBoundary, null,
                        h(NxViewer, { url: entry.url, name: entry.name }))),
            );
        }

        /**
         * A file link inside the conversation arrives as a dsh-resource address, not a Windows
         * path: dsh-resource://file/session/session-<id>/<path relative to the workspace>.
         * NX can only open a real file, so rebuild the absolute path from the workspace root
         * the document-preview props carry.
         */
        function prtSourcePath(props) {
            const raw = (props && (props.path
                || (props.customData && props.customData.path)
                || props.resourceAddress
                || props.name)) || '';
            const text = String(raw).trim();
            if (!text) return '';
            if (!/^dsh-resource:/i.test(text)) return text;
            const parts = text.replace(/^dsh-resource:\/*/i, '').split(/[\\/]+/).filter(Boolean);
            // parts = ["file", "session", "session-<id>", ...relative segments]
            const tail = parts.slice(3).join('\\');
            if (!tail) return '';
            const roots = [
                props && props.scope && props.scope.cwd,
                props && props.workspace && props.workspace.cwd,
                props && props.cwd,
            ].filter((item) => typeof item === 'string' && item.trim());
            if (!roots.length) return tail;
            return roots[0].replace(/[\\/]+$/, '') + '\\' + tail;
        }

        /** Viewer for a .prt opened from a conversation file link: NX turns it into an STL, we draw it. */
        function NxFileViewer(props) {
            const source = prtSourcePath(props);
            const [state, setState] = React.useState({ phase: 'loading', url: '', error: '' });

            React.useEffect(() => {
                if (!source) {
                    setState({ phase: 'error', url: '', error: '这一页没有拿到文件路径' });
                    return undefined;
                }
                let alive = true;
                setState({ phase: 'loading', url: '', error: '' });
                fetch(api('/dsh-nx/preview'), {
                    method: 'POST',
                    headers: { 'content-type': 'application/json' },
                    body: JSON.stringify({ path: source }),
                })
                    .then((response) => response.json())
                    .then((data) => {
                        if (!alive) return;
                        if (!data || !data.ok) {
                            setState({ phase: 'error', url: '', error: (data && data.error) || '预览失败' });
                            return;
                        }
                        setState({ phase: 'ready', url: api(data.stl), error: '' });
                    })
                    .catch((error) => {
                        if (alive) setState({ phase: 'error', url: '', error: String((error && error.message) || error) });
                    });
                return () => { alive = false; };
            }, [source]);

            if (state.phase === 'loading') {
                return h('div', { style: { padding: '18px 16px', fontSize: 12.5, opacity: 0.75 } },
                    '正在用 NX 把模型转成预览…（首次 2–40 秒，之后走缓存）');
            }
            if (state.phase === 'error') {
                return h('div', { style: { padding: '18px 16px', fontSize: 12.5, color: '#c34a3f', whiteSpace: 'pre-wrap' } },
                    'NX 预览失败：' + state.error);
            }
            return h('div', { style: { padding: '10px 10px 8px' } },
                h(ViewerBoundary, null, h(NxViewer, { url: state.url, name: source })));
        }

        /** Document-preview body: same viewer, addressed by a file address instead of a path. */
        function NxDocumentBody(props) {
            return h(NxFileViewer, {
                path: prtSourcePath(props),
                scope: props && props.scope,
                workspace: props && props.workspace,
            });
        }

        return {
            inject: ['slots'],
            apply(ctx) {
                const disposers = [];

                /** Read a client service without tripping over one this build does not declare. */
                function service(name) {
                    try { return (typeof ctx.get === 'function' ? ctx.get(name) : null) || ctx[name]; }
                    catch { return null; }
                }

                function openConsole() {
                    const navigation = service('sidebarRight');
                    if (navigation && typeof navigation.openTab === 'function') {
                        try { navigation.openTab(TAB_KIND); return; }
                        catch { /* tab registry missing: fall through to the raw column */ }
                    }
                    try {
                        const layout = service('layout');
                        if (layout && typeof layout.openRightbar === 'function') layout.openRightbar(true, false);
                    } catch { /* layout unavailable in this client build */ }
                }

                function NxHeaderButton() {
                    return h('button', {
                        type: 'button',
                        onClick: openConsole,
                        title: '打开 NX 控制台',
                        style: {
                            display: 'inline-flex', alignItems: 'center', height: '26px', padding: '0 9px',
                            borderRadius: '7px', cursor: 'pointer', font: 'inherit', fontSize: '12px',
                            lineHeight: 1, color: '#ffffff', border: '1px solid rgba(47,125,209,0.5)',
                            background: '#2f7dd1',
                        },
                    }, 'NX');
                }

                /** Inject into one slot; a missing slot must not take the others down with it. */
                function mount(slotName, registration, component) {
                    try {
                        const off = ctx.slots.inject(slotName, () => {
                            const inner = ctx.slots.register(registration, component);
                            return typeof inner === 'function' ? inner : () => {};
                        });
                        if (typeof off === 'function') disposers.push(off);
                    } catch { /* this client build has no such slot */ }
                }

                mount('sidebar.right.pane.tab', { name: 'sidebar.right.pane.tab', key: TAB_ID }, () => h(NxPanel, null));
                mount('sidebar.right.pane.tab', { name: 'sidebar.right.pane.tab', key: VIEW_TAB_ID }, () => h(NxViewerPage, null));

                /** Opening the viewer page is just a tab switch; the entry rides in pendingView. */
                openTabRef = (kind) => {
                    const navigation = service('sidebarRight');
                    if (navigation && typeof navigation.openTab === 'function') {
                        try { navigation.openTab(kind, { replaceTab: true }); return true; }
                        catch (error) {
                            try { navigation.openTab(kind); return true; }
                            catch (inner) {
                                if (typeof console !== 'undefined') console.warn('dsh-nx: openTab failed', kind, error, inner);
                            }
                        }
                    }
                    try {
                        const layout = service('layout');
                        if (layout && typeof layout.openRightbar === 'function') layout.openRightbar(true, false);
                    } catch { /* layout unavailable in this client build */ }
                    return false;
                };

                /* The type itself: a page kind, offered as a card on the right Sidebar's guide.
                   inject() defers this until the tab registry exists, so the registration is
                   never dropped because apply() ran first. */
                try {
                    ctx.inject(['sidebarRightTabs'], (scope) => {
                        scope.effect(() => scope.sidebarRightTabs.register({
                            id: TAB_ID,
                            kind: TAB_KIND,
                            title: () => 'NX 控制台',
                            guide: [{
                                id: 'console',
                                title: () => 'NX 控制台',
                                description: () => '建模 · 制图 · 装配 · 有限元',
                            }],
                        }), 'dsh-nx: right sidebar tab type');
                        scope.effect(() => scope.sidebarRightTabs.register({
                            id: VIEW_TAB_ID,
                            kind: VIEW_KIND,
                            title: () => (pendingView && pendingView.name) || 'NX 预览',
                            guide: [{
                                id: 'viewer',
                                title: () => 'NX 预览',
                                description: () => '模型 3D 预览（从控制台打开）',
                            }],
                        }), 'dsh-nx: viewer tab type');
                    });
                } catch { /* this client build has no tab registry */ }

                /* A .prt opened from a file link should land in the NX viewer, not the generic
                   file preview: claim the extension in the right Sidebar's file-viewer registry. */
                try {
                    ctx.inject(['betterSidebar'], (scope) => {
                        gate = gate.replace('viewer ?', 'viewer ok');
                        scope.effect(() => scope.betterSidebar.registerFileViewer({
                            id: 'dsh-nx/prt-viewer',
                            title: 'NX 预览',
                            exts: ['prt'],
                            priority: 200,
                            fetchStrategy: 'custom',
                            load: async (path) => ({ path, controller: new AbortController() }),
                            component: NxFileViewer,
                        }), 'dsh-nx: .prt file viewer');
                    });
                } catch { /* this build has no file-viewer registry */ }

                /* Second channel: the document-preview registry, which is what decides the body of a
                   preview tab when a file is opened. Registering both keeps either build happy. */
                try {
                    ctx.inject(['documentPreviews', 'slots'], (scope) => {
                        gate = gate.replace('docpreview ?', 'docpreview ok');
                        scope.effect(() => scope.documentPreviews.register({
                            id: PRT_DOCUMENT_ID,
                            extensions: ['prt'],
                            binaryExtensions: ['prt'],
                            priority: 'builtin',
                            title: () => 'NX 预览',
                            loading: 'bytes-complete',
                            wrap: false,
                        }), 'dsh-nx: .prt document type');
                        scope.effect(() => scope.slots.inject('sidebar.right.tab.document',
                            () => scope.slots.register({ name: 'sidebar.right.tab.document', key: PRT_DOCUMENT_ID }, NxDocumentBody),
                        ), 'dsh-nx: .prt document body');
                    });
                } catch { /* this build has no document-preview registry */ }

                mount('conversation.session.header.utilities', {
                    name: 'conversation.session.header.utilities', id: 'dsh-nx-open', order: 50, label: () => 'NX',
                }, () => h(NxHeaderButton, null));

                ctx.effect(() => () => {
                    for (const dispose of disposers) {
                        try { dispose(); }
                        catch { /* slot already gone */ }
                    }
                }, 'dsh-nx.panel.slots');
            },
        };
    },
});
