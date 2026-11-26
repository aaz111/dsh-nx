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


def _xf(comp):
    """Component placement as (origin, 3x3 rows); None means "already in part space"."""
    if comp is None:
        return None
    try:
        pos, mat = comp.GetPosition()
        return ((float(pos.X), float(pos.Y), float(pos.Z)),
                ((float(mat.Xx), float(mat.Xy), float(mat.Xz)),
                 (float(mat.Yx), float(mat.Yy), float(mat.Yz)),
                 (float(mat.Zx), float(mat.Zy), float(mat.Zz))))
    except Exception:
        return None


def _tp(pt, xf):
    if xf is None:
        return (pt[0], pt[1], pt[2])
    o, m = xf
    x, y, z = pt[0], pt[1], pt[2]
    # Matrix3x3 把三个轴按**行**给回来（Xx Xy Xz / Yx Yy Yz / Zx Zy Zz），所以把一个零件点
    # 映射进装配空间是 origin + M^T * p。按行乘会把旋转反过来，整套装配就散了。
    return (o[0] + m[0][0] * x + m[1][0] * y + m[2][0] * z,
            o[1] + m[0][1] * x + m[1][1] * y + m[2][1] * z,
            o[2] + m[0][2] * x + m[1][2] * y + m[2][2] * z)


def _tn(vec, xf):
    if xf is None:
        return vec
    m = xf[1]
    x, y, z = vec
    return (m[0][0] * x + m[1][0] * y + m[2][0] * z,
            m[0][1] * x + m[1][1] * y + m[2][1] * z,
            m[0][2] * x + m[1][2] * y + m[2][2] * z)


def collect():
    """(body, transform) pairs: the part's own bodies, or every component's for an assembly.

    An assembly has no bodies of its own and its components open lightweight, so each
    prototype has to be loaded before it has anything to facet -- otherwise the viewer got an
    empty buffer and sat on "等待 NX…" for an assembly.
    """
    own = list(part.Bodies)
    if own:
        return [(b, None) for b in own]
    asm = getattr(part, 'ComponentAssembly', None)
    if asm is None:
        return []
    try:
        stack = list(asm.RootComponent.GetChildren())
    except Exception:
        return []
    pairs = []
    guard = 0
    while stack and guard < 4000:
        guard += 1
        comp = stack.pop()
        try:
            stack.extend(list(comp.GetChildren()))
        except Exception:
            pass
        proto = getattr(comp, 'Prototype', None)
        if proto is None:
            continue
        try:
            proto.LoadFully()
        except Exception:
            pass
        try:
            found = list(proto.Bodies)
        except Exception:
            found = []
        xf = _xf(comp)
        for b in found:
            pairs.append((b, xf))
    return pairs


def unify_winding(start):
    """统一这个 body 的三角形绕序：整体看有向体积，为负就全部翻过来。

    质心法在这里是错的 —— 泵体是空心筒，它的内壁法向本来就指向轴心（那是正确的
    "实体外法向"），按"法向背离质心"去判会把内壁翻成朝外，壳体立刻不闭合，模板
    正反面计数就废了。有向体积对闭合网格是整体量，内壁/外壁一起翻或不翻，不会拆散。
    """
    count = len(idx) - start
    if count < 9:
        return 0
    # 以该 body 的包围盒中心为参考点，避免大坐标带来的数值误差
    lo = [1e30, 1e30, 1e30]
    hi = [-1e30, -1e30, -1e30]
    for i in range(start, len(idx), 3):
        for k in range(3):
            p = pos[idx[i + k] * 3: idx[i + k] * 3 + 3]
            for c in range(3):
                if p[c] < lo[c]:
                    lo[c] = p[c]
                if p[c] > hi[c]:
                    hi[c] = p[c]
    ox = (lo[0] + hi[0]) * 0.5
    oy = (lo[1] + hi[1]) * 0.5
    oz = (lo[2] + hi[2]) * 0.5

    vol = 0.0
    for i in range(start, len(idx), 3):
        a, b2, c2 = idx[i], idx[i + 1], idx[i + 2]
        ax, ay, az = pos[a * 3] - ox, pos[a * 3 + 1] - oy, pos[a * 3 + 2] - oz
        bx, by, bz = pos[b2 * 3] - ox, pos[b2 * 3 + 1] - oy, pos[b2 * 3 + 2] - oz
        ex, ey, ez = pos[c2 * 3] - ox, pos[c2 * 3 + 1] - oy, pos[c2 * 3 + 2] - oz
        vol += (ax * (by * ez - bz * ey)
                + ay * (bz * ex - bx * ez)
                + az * (bx * ey - by * ex))
    if vol >= 0.0:
        return 0
    flipped = 0
    for i in range(start, len(idx), 3):
        idx[i + 1], idx[i + 2] = idx[i + 2], idx[i + 1]
        flipped += 1
    return flipped


def recompute_normals(start):
    """按统一后的绕序重算这个 body 的顶点法向。

    unify_winding 只换了顶点顺序，法向还是按旧绕序累加的 —— 两者不一致时光照会跳。
    这里把该 body 用到的顶点法向清零重算，绕序、法向、模板计数就都对上了。
    """
    if len(idx) - start < 9:
        return
    verts = set()
    for i in range(start, len(idx), 3):
        verts.add(idx[i])
        verts.add(idx[i + 1])
        verts.add(idx[i + 2])
    for v_ in verts:
        nrm[v_ * 3] = 0.0
        nrm[v_ * 3 + 1] = 0.0
        nrm[v_ * 3 + 2] = 0.0
    for i in range(start, len(idx), 3):
        a, b2, c2 = idx[i], idx[i + 1], idx[i + 2]
        ax, ay, az = pos[a * 3], pos[a * 3 + 1], pos[a * 3 + 2]
        bx, by, bz = pos[b2 * 3], pos[b2 * 3 + 1], pos[b2 * 3 + 2]
        ex, ey, ez = pos[c2 * 3], pos[c2 * 3 + 1], pos[c2 * 3 + 2]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = ex - ax, ey - ay, ez - az
        gx, gy, gz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        nrm[a * 3] += gx
        nrm[a * 3 + 1] += gy
        nrm[a * 3 + 2] += gz
        nrm[b2 * 3] += gx
        nrm[b2 * 3 + 1] += gy
        nrm[b2 * 3 + 2] += gz
        nrm[c2 * 3] += gx
        nrm[c2 * 3 + 1] += gy
        nrm[c2 * 3 + 2] += gz
    for v_ in verts:
        x, y, z = nrm[v_ * 3], nrm[v_ * 3 + 1], nrm[v_ * 3 + 2]
        L = (x * x + y * y + z * z) ** 0.5
        if L > 1e-12:
            nrm[v_ * 3] = x / L
            nrm[v_ * 3 + 1] = y / L
            nrm[v_ * 3 + 2] = z / L
        else:
            nrm[v_ * 3] = 0.0
            nrm[v_ * 3 + 1] = 0.0
            nrm[v_ * 3 + 2] = 1.0


groups = []
tris = 0
for b, _xfv in collect():
    _idx_start = len(idx)
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
                q0, q1, q2 = _tp(vs[0], _xfv), _tp(vs[1], _xfv), _tp(vs[2], _xfv)
                e1x, e1y, e1z = q1[0] - q0[0], q1[1] - q0[1], q1[2] - q0[2]
                e2x, e2y, e2z = q2[0] - q0[0], q2[1] - q0[1], q2[2] - q0[2]
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
                    q = _tp(vs[k], _xfv)
                    pos.extend((q[0], q[1], q[2]))
                    nrm.extend(fn)
                ids = list(range(base, base + nv))
            else:
                ids = [vert(_tp(vs[k], _xfv), fn) for k in range(nv)]

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
    unify_winding(_idx_start)
    if not FLAT:
        recompute_normals(_idx_start)
    if len(idx) > _idx_start:
        groups.append([_idx_start // 3, len(idx) // 3])

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
               'tris': tris, 'groups': groups}, fh, separators=(',', ':'))

w('%s|%d|%d' % (OUT, tris, len(pos) // 3))
