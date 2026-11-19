"""拓扑供数测试：共边提取、边号稳定、增量 diff、发布原子性。"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from app.main import app


# ----------------------------------------------------------------  fixtures

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每个用例一个全新数据库文件；返回 (client, db_path)。"""
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db


def square(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


REGION = square(0, 0, 100, 100)
P1 = square(0, 0, 50, 100)     # 左半
P2 = square(50, 0, 100, 100)   # 右半（与 P1 共边 x=50）


def init_two(client):
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P2", "geometry": P2}],
    })
    assert r.status_code == 201, r.text
    return r.json()


def move_boundary_to(x):
    return [{"id": "P1", "geometry": square(0, 0, x, 100)},
            {"id": "P2", "geometry": square(x, 0, 100, 100)}]


def topo(client, rev, **params):
    r = client.get(f"/v1/topology/{rev}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def edges_by_key(payload):
    """(start, end, parcels) -> edge，便于按几何与组合断言。"""
    return {(tuple(e["start"]), tuple(e["end"]), tuple(e["parcels"])): e
            for e in payload["edges"]}


def refs_by_parcel(payload):
    return {p["parcel_id"]: p["edges"] for p in payload["parcels"]}


def reconstruct(refs, edges):
    """模拟 GIS 客户端：按有序有向引用把边端点首尾相接，重建环坐标。"""
    by_id = {e["edge_id"]: e for e in edges}
    ring = []
    for ref in refs:
        e = by_id[ref["edge_id"]]
        pts = [e["start"], e["end"]] if ref["direction"] == "forward" \
            else [e["end"], e["start"]]
        if not ring:
            ring.append(pts[0])
        else:
            assert ring[-1] == pts[0], "引用序列必须首尾相接"
        ring.append(pts[1])
    assert ring[0] == ring[-1], "引用序列必须闭合"
    return ring


# ---------------------------------------------------------------- 整版拓扑

def test_topology_full_two_parcels(env):
    client, _ = env
    init_two(client)
    t = topo(client, 1)
    assert t["revision"] == 1

    keys = edges_by_key(t)
    # 共边只登记一次，且带相邻两块宗地编号
    assert ((50, 0), (50, 100), ("P1", "P2")) in keys
    # 区域外沿也入表（只挂一块宗地）
    assert ((0, 0), (50, 0), ("P1",)) in keys
    assert ((0, 0), (0, 100), ("P1",)) in keys
    assert ((0, 100), (50, 100), ("P1",)) in keys
    assert ((50, 0), (100, 0), ("P2",)) in keys
    assert ((100, 0), (100, 100), ("P2",)) in keys
    assert ((50, 100), (100, 100), ("P2",)) in keys
    assert len(t["edges"]) == 7
    # 边号确定且唯一
    assert all(e["edge_id"].startswith("E") for e in t["edges"])
    assert len({e["edge_id"] for e in t["edges"]}) == 7
    # 每块宗地 4 条有序有向引用
    refs = refs_by_parcel(t)
    assert set(refs) == {"P1", "P2"}
    assert len(refs["P1"]) == 4 and len(refs["P2"]) == 4


def test_reconstruct_matches_original_shape(env):
    """客户端凭边表 + 引用可无损失重建边界形状。"""
    client, _ = env
    init_two(client)
    t = topo(client, 1)
    refs = refs_by_parcel(t)
    for pid, original_gj in {"P1": P1, "P2": P2}.items():
        ring = reconstruct(refs[pid], t["edges"])
        rebuilt = Polygon(ring)
        original = Polygon(original_gj["coordinates"][0])
        assert rebuilt.is_valid
        assert rebuilt.equals(original)   # 形状一致（与顶点写法无关）


def test_t_junction_splits_and_maximal_straight_edges(env):
    """T 型节点按组合切分；冗余共线顶点不拆断最长直线段。"""
    client, _ = env
    # P1 左半（左边带一个冗余共线顶点）；右侧上 P2、下 P3
    p1_redundant = {"type": "Polygon", "coordinates": [[
        [0, 0], [50, 0], [50, 100], [0, 100], [0, 50], [0, 0],
    ]]}
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [
            {"id": "P1", "geometry": p1_redundant},
            {"id": "P2", "geometry": square(50, 50, 100, 100)},
            {"id": "P3", "geometry": square(50, 0, 100, 50)},
        ],
    })
    assert r.status_code == 201, r.text
    t = topo(client, 1)
    keys = edges_by_key(t)

    # P1 右边 x=50 在 T 型节点 (50,50) 处按相邻组合切成两条边
    assert ((50, 50), (50, 100), ("P1", "P2")) in keys
    assert ((50, 0), (50, 50), ("P1", "P3")) in keys
    # P2/P3 共边
    assert ((50, 50), (100, 50), ("P2", "P3")) in keys
    # P1 左边虽有冗余顶点 (0,50)，仍是最长连续直线段：一条 (0,0)-(0,100)
    assert ((0, 0), (0, 100), ("P1",)) in keys
    assert not any(k[2] == ("P1",) and k[0][0] == k[1][0] == 0 and k[0] != (0, 0)
                   for k in keys)
    # P1 的引用：左、上、右上、右下、下 共 5 条
    assert len(refs_by_parcel(t)["P1"]) == 5
    # 重建仍然形状无损
    refs = refs_by_parcel(t)
    for pid, original_gj in [("P1", p1_redundant), ("P2", square(50, 50, 100, 100)),
                             ("P3", square(50, 0, 100, 50))]:
        ring = reconstruct(refs[pid], t["edges"])
        assert Polygon(ring).equals(Polygon(original_gj["coordinates"][0]))


def test_point_contact_is_not_shared_edge(env):
    """田字格四宗地：对角两块仅点接触，不算共边。"""
    client, _ = env
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [
            {"id": "A", "geometry": square(0, 0, 50, 50)},
            {"id": "B", "geometry": square(50, 0, 100, 50)},
            {"id": "C", "geometry": square(50, 50, 100, 100)},
            {"id": "D", "geometry": square(0, 50, 50, 100)},
        ],
    })
    assert r.status_code == 201, r.text
    t = topo(client, 1)
    combos = {tuple(e["parcels"]) for e in t["edges"]}
    assert ("A", "C") not in combos   # 仅中心点接触
    assert ("B", "D") not in combos
    # 真正的共边都在，且长度正确
    keys = edges_by_key(t)
    assert ((50, 0), (50, 50), ("A", "B")) in keys
    assert ((50, 50), (100, 50), ("B", "C")) in keys
    assert ((50, 50), (50, 100), ("C", "D")) in keys
    assert ((0, 50), (50, 50), ("A", "D")) in keys
    # 没有退化边（每条边都有正长度）
    assert all(e["start"] != e["end"] for e in t["edges"])


def test_single_parcel_covering_region(env):
    """单宗地覆盖整个区域：外沿成环也能正确切出 4 条边。"""
    client, _ = env
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "ONLY", "geometry": REGION}],
    })
    assert r.status_code == 201, r.text
    t = topo(client, 1)
    keys = edges_by_key(t)
    assert len(t["edges"]) == 4
    assert ((0, 0), (100, 0), ("ONLY",)) in keys
    assert ((0, 0), (0, 100), ("ONLY",)) in keys
    assert ((0, 100), (100, 100), ("ONLY",)) in keys
    assert ((100, 0), (100, 100), ("ONLY",)) in keys
    ring = reconstruct(refs_by_parcel(t)["ONLY"], t["edges"])
    assert Polygon(ring).equals(Polygon(REGION["coordinates"][0]))


def test_diagonal_shared_edge(env):
    """对角线共边：非轴向整数几何同样精确入表。"""
    client, _ = env
    tri1 = {"type": "Polygon", "coordinates": [[[0, 0], [100, 0], [0, 100], [0, 0]]]}
    tri2 = {"type": "Polygon",
            "coordinates": [[[100, 0], [100, 100], [0, 100], [100, 0]]]}
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "T1", "geometry": tri1}, {"id": "T2", "geometry": tri2}],
    })
    assert r.status_code == 201, r.text
    t = topo(client, 1)
    keys = edges_by_key(t)
    # 对角线共边只登记一次：规范端点 (0,100)-(100,0)
    assert ((0, 100), (100, 0), ("T1", "T2")) in keys
    assert len(t["edges"]) == 5   # 1 条对角共边 + 4 条外沿
    refs = refs_by_parcel(t)
    for pid, gj in [("T1", tri1), ("T2", tri2)]:
        ring = reconstruct(refs[pid], t["edges"])
        assert Polygon(ring).equals(Polygon(gj["coordinates"][0]))


def test_l_shaped_shared_boundary_splits_at_turn(env):
    """L 形共边在拐角处切成两条边（同一组合、方向改变）。"""
    client, _ = env
    p1_l = {"type": "Polygon", "coordinates": [[
        [0, 0], [100, 0], [100, 50], [50, 50], [50, 100], [0, 100], [0, 0],
    ]]}
    p2_sq = square(50, 50, 100, 100)
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": p1_l}, {"id": "P2", "geometry": p2_sq}],
    })
    assert r.status_code == 201, r.text
    t = topo(client, 1)
    keys = edges_by_key(t)
    # 同一组合 (P1,P2) 的 L 形共边 → 两条直边
    assert ((50, 50), (100, 50), ("P1", "P2")) in keys
    assert ((50, 50), (50, 100), ("P1", "P2")) in keys
    assert len([e for e in t["edges"] if tuple(e["parcels"]) == ("P1", "P2")]) == 2
    # L 形宗地 6 条引用，重建形状无损
    refs = refs_by_parcel(t)
    assert len(refs["P1"]) == 6
    ring = reconstruct(refs["P1"], t["edges"])
    assert Polygon(ring).equals(Polygon(p1_l["coordinates"][0]))


# ---------------------------------------------------------------- 边号稳定性

def test_ring_normalization_does_not_change_topology(tmp_path, monkeypatch):
    """环起点、绕向、冗余共线顶点不同 → 拓扑（边号与宗地引用）完全一致。"""
    # 标准写法
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    with TestClient(app, raise_server_exceptions=False) as c1:
        init_two(c1)
        t1 = topo(c1, 1)

    # 同一形状：顺时针 + 环起点落在边中间 + 冗余共线顶点
    p1_variant = {"type": "Polygon", "coordinates": [[
        [25, 0], [0, 0], [0, 50], [0, 100], [50, 100], [50, 0], [25, 0],
    ]]}
    p2_variant = {"type": "Polygon", "coordinates": [[
        [75, 100], [50, 100], [50, 0], [100, 0], [100, 100], [75, 100],
    ]]}
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
    with TestClient(app, raise_server_exceptions=False) as c2:
        r = c2.post("/v1/init", json={
            "region": REGION,
            "parcels": [{"id": "P1", "geometry": p1_variant},
                        {"id": "P2", "geometry": p2_variant}],
        })
        assert r.status_code == 201, r.text
        t2 = topo(c2, 1)

    assert t1 == t2


def test_edge_ids_stable_across_versions(env):
    """未变的边跨版本保持同一边号；变动的边得到新边号。"""
    client, _ = env
    init_two(client)
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(60)})
    assert r.status_code == 200, r.text

    t1, t2 = topo(client, 1), topo(client, 2)   # 历史修订也可取数
    ids1 = {(k[0], k[1], k[2]): e["edge_id"] for k, e in edges_by_key(t1).items()}
    ids2 = {(k[0], k[1], k[2]): e["edge_id"] for k, e in edges_by_key(t2).items()}

    # 未受影响的边：P1 左边、P2 右边 —— 边号跨版一致
    assert ids1[((0, 0), (0, 100), ("P1",))] == ids2[((0, 0), (0, 100), ("P1",))]
    assert ids1[((100, 0), (100, 100), ("P2",))] == ids2[((100, 0), (100, 100), ("P2",))]
    # 共边移动：旧边号消失，新边号出现
    assert ((50, 0), (50, 100), ("P1", "P2")) in ids1
    assert ((50, 0), (50, 100), ("P1", "P2")) not in ids2
    assert ((60, 0), (60, 100), ("P1", "P2")) in ids2
    # 同一（端点, 组合）在任何版本都是同一边号：把边界移回 50，边号还原
    r = client.put("/v1/parcels", json={"base_revision": 2,
                                        "parcels": move_boundary_to(50)})
    assert r.status_code == 200, r.text
    t3 = topo(client, 3)
    ids3 = {(k[0], k[1], k[2]): e["edge_id"] for k, e in edges_by_key(t3).items()}
    assert ids3 == ids1
    assert refs_by_parcel(t3) == refs_by_parcel(t1)


def test_noop_replace_keeps_topology_identical(env):
    """形状不变的替换（仅写法不同）：新版拓扑与原版完全一致，增量为空。"""
    client, _ = env
    init_two(client)
    p1_variant = {"type": "Polygon", "coordinates": [[
        [25, 0], [0, 0], [0, 50], [0, 100], [50, 100], [50, 0], [25, 0],
    ]]}
    r = client.put("/v1/parcels", json={
        "base_revision": 1, "parcels": [{"id": "P1", "geometry": p1_variant}],
    })
    assert r.status_code == 200, r.text
    t1, t2 = topo(client, 1), topo(client, 2)
    assert t1["edges"] == t2["edges"]
    assert t1["parcels"] == t2["parcels"]
    d = topo(client, 2, base=1)
    assert d["added_edges"] == []
    assert d["removed_edges"] == []
    assert d["changed_parcels"] == []


# ---------------------------------------------------------------- 增量供数

def test_diff_applies_to_full_target(env):
    """增量应用到手中旧版后，必须与目标整版一致。"""
    client, _ = env
    init_two(client)
    client.put("/v1/parcels", json={"base_revision": 1,
                                    "parcels": move_boundary_to(60)})
    t1, t2 = topo(client, 1), topo(client, 2)
    d = topo(client, 2, base=1)
    assert d["revision"] == 2 and d["base_revision"] == 1

    # 只携带新增/移除的边：未变的两条外沿边不在其中
    removed_ids = {e["edge_id"] for e in d["removed_edges"]}
    added_ids = {e["edge_id"] for e in d["added_edges"]}
    ids1 = edges_by_key(t1)
    left_edge = ids1[((0, 0), (0, 100), ("P1",))]["edge_id"]
    right_edge = ids1[((100, 0), (100, 100), ("P2",))]["edge_id"]
    assert left_edge not in removed_ids and left_edge not in added_ids
    assert right_edge not in removed_ids and right_edge not in added_ids
    assert not (removed_ids & added_ids)

    # 变动宗地给出新引用
    assert {p["parcel_id"] for p in d["changed_parcels"]} == {"P1", "P2"}

    # 应用增量：边表
    edges = {e["edge_id"]: e for e in t1["edges"]}
    for e in d["removed_edges"]:
        del edges[e["edge_id"]]
    for e in d["added_edges"]:
        edges[e["edge_id"]] = e
    assert edges == {e["edge_id"]: e for e in t2["edges"]}
    # 应用增量：宗地引用
    refs = refs_by_parcel(t1)
    for cp in d["changed_parcels"]:
        refs[cp["parcel_id"]] = cp["edges"]
    assert refs == refs_by_parcel(t2)


def test_diff_chain_across_multiple_versions(env):
    """连续多版：逐段增量应用后与最终整版一致。"""
    client, _ = env
    init_two(client)
    client.put("/v1/parcels", json={"base_revision": 1,
                                    "parcels": move_boundary_to(60)})
    client.put("/v1/parcels", json={"base_revision": 2,
                                    "parcels": move_boundary_to(30)})

    # 客户端手中是 v1，逐段取增量 1→2、2→3
    edges = {e["edge_id"]: e for e in topo(client, 1)["edges"]}
    refs = refs_by_parcel(topo(client, 1))
    for frm, to in [(1, 2), (2, 3)]:
        d = topo(client, to, base=frm)
        for e in d["removed_edges"]:
            del edges[e["edge_id"]]
        for e in d["added_edges"]:
            edges[e["edge_id"]] = e
        for cp in d["changed_parcels"]:
            refs[cp["parcel_id"]] = cp["edges"]

    t3 = topo(client, 3)
    assert edges == {e["edge_id"]: e for e in t3["edges"]}
    assert refs == refs_by_parcel(t3)

    # 也可以直接从 v1 跳到 v3
    d = topo(client, 3, base=1)
    edges = {e["edge_id"]: e for e in topo(client, 1)["edges"]}
    refs = refs_by_parcel(topo(client, 1))
    for e in d["removed_edges"]:
        del edges[e["edge_id"]]
    for e in d["added_edges"]:
        edges[e["edge_id"]] = e
    for cp in d["changed_parcels"]:
        refs[cp["parcel_id"]] = cp["edges"]
    assert edges == {e["edge_id"]: e for e in t3["edges"]}
    assert refs == refs_by_parcel(t3)


def test_diff_same_base_and_target_is_empty(env):
    client, _ = env
    init_two(client)
    d = topo(client, 1, base=1)
    assert d["added_edges"] == [] and d["removed_edges"] == []
    assert d["changed_parcels"] == []


# ---------------------------------------------------------------- 错误与原子性

def test_topology_404s(env):
    client, _ = env
    # 未初始化
    assert client.get("/v1/topology/1").status_code == 404
    init_two(client)
    # 版本不存在
    r = client.get("/v1/topology/9")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "revision_not_found"
    # 基线版本不存在
    r = client.get("/v1/topology/1", params={"base": 9})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "revision_not_found"


def test_topology_build_failure_rolls_back_publish(env, monkeypatch):
    """拓扑生成失败：整次发布回滚，原版继续可读，不留半版。"""
    client, db = env
    init_two(client)

    import app.main as main_mod
    original = main_mod.build_topology

    def boom(*args, **kwargs):
        raise RuntimeError("simulated topology build failure")

    monkeypatch.setattr(main_mod, "build_topology", boom)
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(60)})
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "build_topology", original)

    # 原版继续可读，没有留下半个新版本
    assert client.get("/v1/current").json()["revision"] == 1
    assert client.get("/v1/topology/2").status_code == 404
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM topo_edges").fetchone()[0] == 7
        assert conn.execute(
            "SELECT COUNT(*) FROM topo_edges WHERE revision > 1").fetchone()[0] == 0
    finally:
        conn.close()

    # 恢复后照常发布，且拓扑随新版一起可查
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(60)})
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert client.get("/v1/topology/2").status_code == 200


def test_topology_insert_failure_rolls_back_publish(env, monkeypatch):
    """拓扑落库失败：版本其余部分同事务回滚，不留半版。"""
    client, db = env
    init_two(client)

    import app.main as main_mod
    original = main_mod._insert_topology

    def boom(*args, **kwargs):
        raise RuntimeError("simulated topology insert failure")

    monkeypatch.setattr(main_mod, "_insert_topology", boom)
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(60)})
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "_insert_topology", original)

    assert client.get("/v1/current").json()["revision"] == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM parcels WHERE revision > 1"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM well_attributions WHERE revision > 1"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM topo_edges WHERE revision > 1"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT current_revision FROM meta").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_topology_failure_leaves_nothing(env, monkeypatch):
    """首次提交时拓扑失败：整个版本都不存在。"""
    client, _ = env
    import app.main as main_mod
    original = main_mod.build_topology

    def boom(*args, **kwargs):
        raise RuntimeError("simulated topology build failure")

    monkeypatch.setattr(main_mod, "build_topology", boom)
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P2", "geometry": P2}],
    })
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "build_topology", original)
    assert client.get("/v1/current").status_code == 404
    assert client.get("/v1/topology/1").status_code == 404


def test_restart_keeps_topology_readable(env):
    """重启后历史版本拓扑仍完整可读。"""
    client, _ = env
    init_two(client)
    client.put("/v1/parcels", json={"base_revision": 1,
                                    "parcels": move_boundary_to(60)})
    before = topo(client, 2)
    with TestClient(app, raise_server_exceptions=False) as client2:
        assert topo(client2, 2) == before
        assert client2.get("/v1/topology/1").status_code == 200
