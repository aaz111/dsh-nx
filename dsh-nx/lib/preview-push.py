# dsh-nx 实时预览：取当前部件显示三角网格，写成一个 JSON 文件。
#
# 由 lib/preview.js 通过 bridge 的 exec 命令送进来执行，三个占位符在发送前替换：
#   __TOL__   弦高容差 mm（0 表示沿用 NX 当前缓存）
#   __FLAT__  True/False，平面着色（每个三角形独立顶点 + 面法向）
#   __OUT__   输出文件绝对路径
#
# 环境里的 part / NXOpen / w 由 nxworker 的 exec 命令提供。
# 顶点按 (位置, 面法向) 合并：同一平面共用、棱边两侧裂开。

import json
import NXOpen.UF   # NX 会话重启后子模块不会自动挂上，缺这句 NXOpen.UF 是 MISSING

TOL = __TOL__
FLAT = __FLAT__
OUT = r"__OUT__"

# exec 环境的 part 是会话启动时定的活动部件，用户在 GUI 里换部件后不会跟着变，
# 所以每次都重新问一次工作部件。
try:
    _work = NXOpen.Session.GetSession().Parts.Work
    if _work is not None:
        part = _work
except Exception:
    pass

ufs = NXOpen.UF.UFSession.GetUFSession()

p = ufs.Facet.AskDefaultParameters()
if TOL:
    p.SpecifySurfaceTolerance = True
    p.SurfaceDistTolerance = float(TOL)
    p.SurfaceAngularTolerance = max(0.02, min(0.6, float(TOL) * 3.0))
    p.SpecifyMaxFacetSize = True
    p.MaxFacetSize = int(max(1, round(float(TOL) * 40)))
try:
    ufs.Facet.SetDefaultParameters(p)
except Exception:
    pass

pos = []
nrm = []
idx = []
vmap = {}
fn0 = []


def vert(pt, fn):
    k = (round(pt[0], 4), round(pt[1], 4), round(pt[2], 4),
         round(fn[0], 1), round(fn[1], 1), round(fn[2], 1))
    j = vmap.get(k)
    if j is None:
        j = len(pos) // 3
        vmap[k] = j
        pos.extend((pt[0], pt[1], pt[2]))
        nrm.extend((0.0, 0.0, 0.0))
        fn0.append(fn)
    return j


tris = 0
bodies = list(part.Bodies)
for b in bodies:
    # 精度改过就必须重面片化，否则 NX 会把旧缓存给回来
    if TOL:
        try:
            ufs.Facet.FacetSolid(b.Tag, p)
        except Exception:
            pass
    try:
        _nm, models = ufs.Facet.AskModelsOfSolid(b.Tag)
    except Exception:
        continue
    for m in models:
        total = 0
        try:
            for f in range(ufs.Facet.AskNumFaces(m)):
                total += ufs.Facet.AskNumFacetsInFace(m, f)
        except Exception:
            total = 0
        fid = 0
        while True:
            if total:
                if fid >= total:
                    break
            elif fid > 4000000:
                break
            try:
                nv, vs = ufs.Facet.AskVerticesOfFacet(m, fid)
            except Exception:
                if not total:
                    break
                fid += 1
                continue
            if not nv:
                if not total:
                    break
                fid += 1
                continue

            try:
                p0, p1, p2 = vs[0], vs[1], vs[2]
                e1x, e1y, e1z = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
                e2x, e2y, e2z = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
                fx = e1y * e2z - e1z * e2y
                fy = e1z * e2x - e1x * e2z
                fz = e1x * e2y - e1y * e2x
                fl = (fx * fx + fy * fy + fz * fz) ** 0.5
                fn = (fx / fl, fy / fl, fz / fl) if fl > 1e-12 else (0.0, 0.0, 1.0)
            except Exception:
                fn = (0.0, 0.0, 1.0)

            if FLAT:
                base = len(pos) // 3
                for k in range(nv):
                    q = vs[k]
                    pos.extend((q[0], q[1], q[2]))
                    nrm.extend(fn)
                ids = list(range(base, base + nv))
            else:
                ids = [vert(vs[k], fn) for k in range(nv)]

            if nv == 3:
                fan = ((ids[0], ids[1], ids[2]),)
            elif nv == 4:
                fan = ((ids[0], ids[1], ids[2]), (ids[0], ids[2], ids[3]))
            else:
                fan = tuple((ids[0], ids[k], ids[k + 1]) for k in range(1, nv - 1))

            for a, c, d in fan:
                if not FLAT:
                    ax, ay, az = pos[a * 3], pos[a * 3 + 1], pos[a * 3 + 2]
                    bx, by, bz = pos[c * 3], pos[c * 3 + 1], pos[c * 3 + 2]
                    cx, cy, cz = pos[d * 3], pos[d * 3 + 1], pos[d * 3 + 2]
                    ux, uy, uz = bx - ax, by - ay, bz - az
                    wx, wy, wz = cx - ax, cy - ay, cz - az
                    nx_, ny_, nz_ = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
                    for t in (a, c, d):
                        # 反向三角形不参与：NX 有些面绕序不统一，直接相加会让法向抵消
                        if (nrm[t * 3] * nx_ + nrm[t * 3 + 1] * ny_
                                + nrm[t * 3 + 2] * nz_) < 0.0:
                            continue
                        nrm[t * 3] += nx_
                        nrm[t * 3 + 1] += ny_
                        nrm[t * 3 + 2] += nz_
                idx.extend((a, c, d))
                tris += 1
            fid += 1

if not FLAT:
    for i in range(0, len(nrm), 3):
        x, y, z = nrm[i], nrm[i + 1], nrm[i + 2]
        L = (x * x + y * y + z * z) ** 0.5
        if L > 1e-12:
            nrm[i], nrm[i + 1], nrm[i + 2] = x / L, y / L, z / L
        else:
            f = fn0[i // 3]
            nrm[i], nrm[i + 1], nrm[i + 2] = f[0], f[1], f[2]

inter = []
for i in range(0, len(pos), 3):
    inter.extend((pos[i], pos[i + 1], pos[i + 2], nrm[i], nrm[i + 1], nrm[i + 2]))

with open(OUT, 'w', encoding='utf-8') as fh:
    _pn = str(getattr(part, 'LeafName', None) or getattr(part, 'Name', None)
         or getattr(part, 'FullPath', None) or 'part')
    _pn = _pn.replace(chr(92), '/').split('/')[-1]
    json.dump({'name': _pn, 'vertices': inter, 'indices': idx,
               'tris': tris}, fh, separators=(',', ':'))

w('%s|%d|%d' % (OUT, tris, len(pos) // 3))
