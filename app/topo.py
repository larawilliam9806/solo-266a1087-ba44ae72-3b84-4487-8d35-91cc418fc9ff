"""宗地图层拓扑构建：共边提取、边号分配、宗地边引用、版本间差异。

边提取规则：
- 逐对宗地求边界交集，只取线状部分 —— 仅点接触不算共边；
- 宗地边界落在区域边界上的部分作为"区域外沿"入表（只挂一块宗地）；
- 同一相邻宗地组合内，共线且首尾相接的线段合并 —— 每条边即
  "相邻宗地组合相同的最长连续直线段"，共边只登记一次。

边号稳定性（跨版一一对应）：
- 边号 = sha256(规范端点 + 相邻宗地组合) 前 16 位十六进制；
- 规范端点按字典序定向，与提交时的环起点、绕向、冗余共线顶点无关 ——
  同一（端点, 组合）在任何版本都得到同一边号。

宗地引用：
- 每块宗地给出沿外环逆时针、从最小边号开始的有序有向边引用；
  客户端按引用把边端点首尾相接即可无损重建边界形状。
- 坐标全部按整数精确计算（输入已校验为整数坐标），无浮点误差。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from shapely.geometry import Polygon
from shapely.ops import linemerge

SNAP_TOL = 1e-9


class TopoError(Exception):
    """拓扑生成失败（本次发布应整体回滚）。"""


# ---------------------------------------------------------------- 基础工具

def _snap(v: float) -> int:
    """把坐标吸附到最近的整数；偏离过远说明几何异常，拓扑生成失败。"""
    r = round(v)
    if abs(v - r) > SNAP_TOL:
        raise TopoError(f"边界交点坐标 {v!r} 不是整数")
    return int(r)


def _snap_point(c: Any) -> tuple[int, int]:
    return (_snap(c[0]), _snap(c[1]))


def _sub(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    return (a[0] - b[0], a[1] - b[1])


def _cross(d1: tuple[int, int], d2: tuple[int, int]) -> int:
    return d1[0] * d2[1] - d1[1] * d2[0]


def _dot(d1: tuple[int, int], d2: tuple[int, int]) -> int:
    return d1[0] * d2[0] + d1[1] * d2[1]


def _on_seg(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> bool:
    """点 p 是否在线段 a-b 上（整数精确判断）。"""
    if _cross(_sub(b, a), _sub(p, a)) != 0:
        return False
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def _lines(geom: Any) -> list:
    """从任意几何中取出所有线状部分（点状接触忽略，不算共边）。"""
    if geom.is_empty:
        return []
    gt = geom.geom_type
    if gt == "LineString":
        return [geom] if geom.length > 0 else []
    if gt == "MultiLineString":
        return [g for g in geom.geoms if g.length > 0]
    if gt == "GeometryCollection":
        out: list = []
        for g in geom.geoms:
            out.extend(_lines(g))
        return out
    return []  # Point / MultiPoint / Polygon 等


def _straight_runs(coords: Any) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """把一条折线切成最长连续直线段：顶点处方向不变（共线）则合并。"""
    pts: list[tuple[int, int]] = []
    for c in coords:
        p = _snap_point(c)
        if not pts or pts[-1] != p:
            pts.append(p)
    if len(pts) < 2:
        return []
    if pts[0] == pts[-1]:
        # 闭合环（如单宗地覆盖整个区域时的外沿）：按环找方向突变点，
        # 避免环起点落在某条直线边中间而把它切成两段。
        pts = pts[:-1]
        n = len(pts)
        if n < 3:
            return []
        dirs = [_sub(pts[(i + 1) % n], pts[i]) for i in range(n)]
        corners = [i for i in range(n)
                   if _cross(dirs[i - 1], dirs[i]) != 0 or _dot(dirs[i - 1], dirs[i]) < 0]
        return [(pts[a], pts[b]) for a, b in zip(corners, corners[1:] + corners[:1])]
    runs = []
    start = pts[0]
    prev = _sub(pts[1], pts[0])
    for i in range(2, len(pts)):
        d = _sub(pts[i], pts[i - 1])
        if _cross(prev, d) != 0 or _dot(prev, d) < 0:
            runs.append((start, pts[i - 1]))
            start = pts[i - 1]
        prev = d
    runs.append((start, pts[-1]))
    return runs


def _edge_id(c0: tuple[int, int], c1: tuple[int, int], combo: tuple[str, ...]) -> str:
    """边号：仅由规范端点与相邻宗地组合决定，跨版本稳定。"""
    key = f"{c0[0]},{c0[1]}:{c1[0]},{c1[1]}|{','.join(combo)}"
    return "E" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class Topology:
    """一个版本的完整拓扑：边表 + 每块宗地的有序有向边引用。"""
    edges: dict[str, dict] = field(default_factory=dict)        # edge_id -> 边对象
    refs: dict[str, list[dict]] = field(default_factory=dict)   # parcel_id -> 引用序列


# ---------------------------------------------------------------- 拓扑构建

def build_topology(parcels: dict[str, Polygon], region: Polygon) -> Topology:
    """从核查通过的宗地集合构建拓扑。任何内部不一致都抛 TopoError。"""
    ids = sorted(parcels)
    pieces: dict[tuple[str, ...], list] = {}

    # 1) 收集边界片段：宗地两两共边 + 区域外沿
    for i, a in enumerate(ids):
        boundary_a = parcels[a].boundary
        for b in ids[i + 1:]:
            for line in _lines(boundary_a.intersection(parcels[b].boundary)):
                pieces.setdefault((a, b), []).append(line)
        for line in _lines(boundary_a.intersection(region.boundary)):
            pieces.setdefault((a,), []).append(line)

    # 2) 同一组合内合并相接片段，再按方向切出最长连续直线段
    edges: dict[str, dict] = {}
    segs_of: dict[str, list] = {}
    for combo, lines in pieces.items():
        merged = linemerge(lines) if len(lines) > 1 else lines[0]
        for line in _lines(merged):
            for p, q in _straight_runs(line.coords):
                c0, c1 = (p, q) if p <= q else (q, p)   # 规范定向：字典序
                eid = _edge_id(c0, c1, combo)
                if eid in edges:
                    raise TopoError(f"边号冲突：{eid} 对应多段边界")
                edges[eid] = {
                    "edge_id": eid,
                    "start": [c0[0], c0[1]],
                    "end": [c1[0], c1[1]],
                    "parcels": list(combo),
                }
                for pid in combo:
                    segs_of.setdefault(pid, []).append((eid, c0, c1))

    # 3) 每块宗地的有序有向边引用
    refs = {pid: _parcel_refs(parcels[pid], segs_of.get(pid, [])) for pid in ids}
    return Topology(edges=edges, refs=refs)


def _parcel_refs(poly: Polygon, segments: list) -> list[dict]:
    """沿外环（统一逆时针）把边界段映射到边号，输出规范化引用序列。

    规范化：逆时针绕向 + 从最小边号开始 —— 环起点、绕向、冗余共线顶点
    的不同写法得到完全相同的引用序列。
    环上一条直线段可能跨越多条边（T 型节点处相邻组合改变），
    因此沿行进方向逐边推进，而不是要求整段匹配同一条边。
    """
    ring = poly.exterior
    coords = [_snap_point(c) for c in ring.coords]
    if not ring.is_ccw:
        coords = coords[::-1]
    pts = coords[:-1]  # 去掉闭合重复点
    clean: list[tuple[int, int]] = []
    for p in pts:
        if not clean or clean[-1] != p:
            clean.append(p)
    if len(clean) > 1 and clean[0] == clean[-1]:
        clean.pop()
    n = len(clean)
    # 剔除冗余共线顶点，只留角点：每段角点间即环上最长直线段；
    # 边可能因 T 型节点在段内继续细分（由下面的逐边推进处理），
    # 但边不会越过角点（角点处环已转向）。
    dirs = [_sub(clean[(i + 1) % n], clean[i]) for i in range(n)]
    corners = [clean[i] for i in range(n)
               if _cross(dirs[i - 1], dirs[i]) != 0 or _dot(dirs[i - 1], dirs[i]) < 0]
    n = len(corners)

    refs: list[dict] = []
    for i in range(n):
        u, v = corners[i], corners[(i + 1) % n]
        if u == v:
            continue
        d = _sub(v, u)
        t_end = _dot(d, d)  # v 在行进方向上的投影（以 u 为原点）
        pos = u
        while pos != v:
            t_pos = _dot(_sub(pos, u), d)
            hit: Optional[tuple] = None
            for eid, c0, c1 in segments:
                if _cross(_sub(c1, c0), d) != 0 or not _on_seg(pos, c0, c1):
                    continue  # 不共线或 pos 不在边上
                t_c0 = _dot(_sub(c0, u), d)
                t_c1 = _dot(_sub(c1, u), d)
                if max(t_c0, t_c1) <= t_pos:
                    continue  # 边在 pos 处已结束，不带来推进
                hit = (eid, c0, c1, c0 if t_c0 > t_c1 else c1, max(t_c0, t_c1))
                break
            if hit is None:
                raise TopoError(f"宗地边界段 {u}->{v} 在 {pos} 处未匹配到任何边")
            eid, c0, c1, far, t_far = hit
            if t_far > t_end:
                raise TopoError(f"边 {eid} 越过宗地顶点 {v}，拓扑不一致")
            direction = "forward" if _dot(d, _sub(c1, c0)) > 0 else "reverse"
            if not refs or refs[-1]["edge_id"] != eid:
                refs.append({"edge_id": eid, "direction": direction})
            pos = far
    if len(refs) > 1 and refs[0]["edge_id"] == refs[-1]["edge_id"]:
        refs.pop()  # 环起点落在某条边中间：首尾是同一条边，合并
    k = min(range(len(refs)), key=lambda j: refs[j]["edge_id"])
    return refs[k:] + refs[:k]


# ---------------------------------------------------------------- 版本间差异

def diff_topology(base: Topology, target: Topology) -> dict:
    """从 base 到 target 的增量：新增边、移除边、变动宗地的新引用。

    客户端把增量应用到手中 base 版拓扑后，结果与 target 整版一致。
    """
    added = [target.edges[e] for e in sorted(target.edges) if e not in base.edges]
    removed = [base.edges[e] for e in sorted(base.edges) if e not in target.edges]
    changed = [
        {"parcel_id": pid, "edges": target.refs[pid]}
        for pid in sorted(target.refs)
        if base.refs.get(pid) != target.refs[pid]
    ]
    return {"added_edges": added, "removed_edges": removed, "changed_parcels": changed}
