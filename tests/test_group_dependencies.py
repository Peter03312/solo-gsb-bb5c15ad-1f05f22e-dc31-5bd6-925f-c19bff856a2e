"""先导组放行关系集成测试。

覆盖：
* 登记/替换：为未出发组登记先导组与非负放行间隔，同一组最多一项，
  再次登记替换旧关系；成功登记产生 group-dependencies 修订；
* 放行约束：后继组可出发分钟不早于自身最早出发分钟及先导组规划到达
  分钟加间隔；先导已人工送达时改用实际送达分钟；
* 前置未满足：先导待救援/救援中/无可行路线时，后继标记 PRECURSOR_UNMET、
  不占通道，随后续重排（救援结案、滞留报告、时间推进）再次判定；
* 多级关系按依赖顺序安排，每轮在可安排组中沿用现有优先级/最早出发/
  组编号规则；
* 拒绝：目标组已出发（409 GROUP_DEPARTED）、自依赖（409
  SELF_DEPENDENCY）、成环（409 CYCLE）、组号/先导组号不存在或间隔
  为负（422）；拒绝不改编排与修订历史；
* 查询：GET /group-dependencies 与 /plan 展示先导关系与放行依据；
  场景重置清空全部关系。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-deps-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


# 链式场景：1 --e1(4min/30)--> 2 --e2(2min/30)--> 6(出口)
CHAIN_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 30},
    ],
    "exits": [6],
    "closures": [],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 40},
        {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 40},
        {"id": 3, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 40},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=CHAIN_SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


def register(c, group_id, pioneer_group_id, release_interval):
    return c.post("/group-dependencies", json={
        "group_id": group_id, "pioneer_group_id": pioneer_group_id,
        "release_interval": release_interval})


# --------------------------------------------------------------------------
# 登记与放行约束
# --------------------------------------------------------------------------

def test_register_dependency_defers_successor_departure(client):
    # 组 1：e1[0,4) + e2[4,6)，第 6 分钟到达
    plan0 = by_id(client.get("/plan").json())
    assert plan0[1]["arrival_minute"] == 6
    assert plan0[2]["departure_minute"] == 0  # 无关系时立即可出发

    resp = register(client, 2, 1, 3)
    assert resp.status_code == 201, resp.text
    groups = by_id(resp.json())
    # 后继组 2 的可出发分钟 = 先导组 1 规划到达 6 + 间隔 3 = 9
    g2 = groups[2]
    assert g2["status"] == "scheduled"
    assert g2["departure_minute"] == 9
    assert g2["arrival_minute"] == 15  # e1[9,13) + e2[13,15)
    assert g2["route"] == [1, 2]
    # 编排中展示先导关系与放行依据
    assert g2["pioneer_group_id"] == 1
    assert g2["release_interval"] == 3
    assert g2["release_after_minute"] == 9
    # 先导组与无关系组这些字段恒为 null（兼容既有字段语义）
    assert groups[1]["pioneer_group_id"] is None
    assert groups[1]["release_interval"] is None
    assert groups[1]["release_after_minute"] is None
    assert groups[3]["pioneer_group_id"] is None


def test_register_replaces_old_relationship(client):
    assert register(client, 2, 1, 3).status_code == 201
    # 再次登记替换旧关系：间隔 3 -> 0
    resp = register(client, 2, 1, 0)
    assert resp.status_code == 201
    g2 = by_id(resp.json())[2]
    assert g2["departure_minute"] == 6  # 到达 6 + 间隔 0
    assert g2["release_interval"] == 0
    assert g2["release_after_minute"] == 6
    # 同一组最多一项关系
    deps = client.get("/group-dependencies").json()["dependencies"]
    assert [d["group_id"] for d in deps] == [2]
    # 替换记入修订 detail
    entry = revs(client)[-1]
    assert entry["operation"] == "group-dependencies"
    assert entry["detail"] == {
        "group_id": 2, "pioneer_group_id": 1, "release_interval": 0,
        "replaced": {"pioneer_group_id": 1, "release_interval": 3}}


def test_registration_creates_revision_and_plan_matches(client):
    n0 = len(revs(client))
    resp = register(client, 2, 1, 3)
    assert resp.status_code == 201
    entries = revs(client)
    assert len(entries) == n0 + 1
    entry = entries[-1]
    assert entry["operation"] == "group-dependencies"
    assert entry["detail"] == {
        "group_id": 2, "pioneer_group_id": 1, "release_interval": 3,
        "replaced": None}
    # 修订快照即登记后的当前编排；差异包含后继组字段变化
    detail = client.get(f"/revisions/{entry['rev']}").json()
    assert detail["snapshot"] == client.get("/plan").json()
    ch = {c["group_id"]: c for c in detail["changes"]}[2]
    assert ch["change"] == "changed"
    assert "departure_minute" in ch["changed_fields"]
    assert "pioneer_group_id" in ch["changed_fields"]
    assert ch["after"]["release_after_minute"] == 9


def test_list_dependencies_shows_basis(client):
    assert register(client, 2, 1, 3).status_code == 201
    assert register(client, 3, 2, 2).status_code == 201
    deps = client.get("/group-dependencies").json()
    assert deps["current_minute"] == 0
    assert deps["dependencies"] == [
        {"group_id": 2, "pioneer_group_id": 1, "release_interval": 3,
         "pioneer_status": "scheduled", "release_after_minute": 9},
        {"group_id": 3, "pioneer_group_id": 2, "release_interval": 2,
         "pioneer_status": "scheduled", "release_after_minute": 17}]


def test_multi_level_chain_scheduled_in_dependency_order(client):
    # 3 依赖 2、2 依赖 1：按依赖顺序逐轮安排
    assert register(client, 3, 2, 2).status_code == 201
    resp = register(client, 2, 1, 3)
    groups = by_id(resp.json())
    # 组 1 到达 6 -> 组 2 出发 9、到达 15 -> 组 3 出发 17、到达 23
    assert groups[1]["departure_minute"] == 0
    assert groups[2]["departure_minute"] == 9
    assert groups[2]["arrival_minute"] == 15
    assert groups[3]["departure_minute"] == 17
    assert groups[3]["arrival_minute"] == 23
    assert groups[3]["release_after_minute"] == 17


def test_dependency_order_beats_priority_within_rounds(client):
    # 单通道容量 10：每轮在可安排组中按优先级占用容量，
    # 高优先级但未放行（先导未排定）的组只能等下一轮
    client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2,
                      "capacity": 10}],
        "exits": [6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 30},
            {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 3, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 1, "deadline": 30}]})
    resp = register(client, 2, 1, 1)  # 组 2 优先级最高但依赖组 1
    assert resp.status_code == 201
    groups = by_id(resp.json())
    # 第 1 轮可安排 {1, 3}：组 1（优先级 5）占 [0,2)，组 3（优先级 1）占 [2,4)
    assert groups[1]["departure_minute"] == 0
    assert groups[3]["departure_minute"] == 2
    # 第 2 轮组 2 放行依据 = 到达 2 + 间隔 1 = 3；第 3 分钟被组 3 占满，
    # 顺延到第 4 分钟进入
    assert groups[2]["departure_minute"] == 4
    assert groups[2]["release_after_minute"] == 3


def test_release_basis_pushes_past_deadline(client):
    # 组 2 截止 8：放行依据 6+3=9 已晚于截止 -> DEADLINE_PASSED 并说明依据
    client.post("/group-corrections", json=[{"group_id": 2, "deadline": 8}])
    resp = register(client, 2, 1, 3)
    assert resp.status_code == 201
    g2 = by_id(resp.json())[2]
    assert g2["status"] == "failed"
    assert g2["reason"] == "DEADLINE_PASSED"
    assert "放行依据分钟 9" in g2["detail"]
    assert g2["release_after_minute"] == 9
    assert g2["departure_minute"] is None


# --------------------------------------------------------------------------
# 登记拒绝：已出发 / 自依赖 / 成环 / 无效编号或间隔
# --------------------------------------------------------------------------

def test_rejections_change_nothing(client):
    assert register(client, 2, 1, 3).status_code == 201
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    # 自依赖
    resp = register(client, 2, 2, 1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_DEPENDENCY_NOT_ACCEPTABLE"
    assert resp.json()["conflicts"][0]["reason"] == "SELF_DEPENDENCY"
    # 成环（2 已依赖 1，再令 1 依赖 2）
    resp = register(client, 1, 2, 1)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "CYCLE"
    assert resp.json()["conflicts"][0]["chain"] == [2, 1]
    # 不存在的组 / 先导组
    assert register(client, 999, 1, 1).status_code == 422
    assert register(client, 2, 999, 1).status_code == 422
    # 负间隔
    assert register(client, 2, 1, -1).status_code == 422
    # 计划与修订历史不变
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_three_node_cycle_rejected(client):
    assert register(client, 2, 1, 0).status_code == 201
    assert register(client, 3, 2, 0).status_code == 201
    resp = register(client, 1, 3, 0)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "CYCLE"
    assert conflict["chain"] == [3, 2, 1]
    # 既有关系不受影响
    deps = client.get("/group-dependencies").json()["dependencies"]
    assert [(d["group_id"], d["pioneer_group_id"]) for d in deps] == \
        [(2, 1), (3, 2)]


def test_departed_target_rejected(client):
    # 先登记 2->1（组 2 第 9 分钟才出发），再推进 1 分钟使组 1 固定
    assert register(client, 2, 1, 3).status_code == 201
    client.post("/tick", json={"minutes": 1})  # 组 1（第 0 分钟出发）固定
    resp = register(client, 1, 2, 1)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "GROUP_DEPARTED"
    assert conflict["status"] == "fixed"
    # 先导组可以是已出发组：以组 1（fixed，规划到达 6）为先导替换组 2 的关系
    resp = register(client, 2, 1, 2)
    assert resp.status_code == 201
    g2 = by_id(resp.json())[2]
    assert g2["departure_minute"] == 8
    assert g2["release_after_minute"] == 8


# --------------------------------------------------------------------------
# 前置未满足：先导待救援 / 救援中 / 无可行路线
# --------------------------------------------------------------------------

def test_unschedulable_pioneer_marks_successor_unmet(client):
    # 组 9 人数 50 超通道容量 30，无可行路线；组 2 依赖组 9
    client.post("/groups", json=[
        {"id": 9, "size": 50, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 40}])
    resp = register(client, 2, 9, 1)
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[9]["status"] == "failed"
    assert groups[9]["reason"] == "INSUFFICIENT_CAPACITY"
    g2 = groups[2]
    assert g2["status"] == "failed"
    assert g2["reason"] == "PRECURSOR_UNMET"
    assert "先导组 9 无可行路线" in g2["detail"]
    assert g2["route"] == [] and g2["departure_minute"] is None
    assert g2["release_after_minute"] is None
    # 不占通道：通道 1 上只有组 1、3 的占用（各 10 人，[0,4)）
    load = client.get("/passages/1/load").json()
    for m in load["minutes"]:
        assert m["used"] <= 20
    # 先导组纠正为可安排后，后继随重排自动放行
    client.post("/group-corrections", json=[{"group_id": 9, "size": 10}])
    groups = by_id(client.get("/plan").json())
    assert groups[9]["status"] == "scheduled"
    assert groups[2]["status"] == "scheduled"
    assert groups[2]["release_after_minute"] == \
        groups[9]["arrival_minute"] + 1
    assert groups[2]["departure_minute"] == groups[2]["release_after_minute"]


def make_awaiting_rescue_with_follower(c):
    """组 1 进入待救援、组 2 登记为组 1 的后继（间隔 2）。"""
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 40}]})
    assert register(c, 2, 1, 2).status_code == 201
    assert c.post("/tick", json={"minutes": 4}).status_code == 200
    resp = c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 6})
    assert resp.status_code == 200
    return resp


def test_awaiting_rescue_pioneer_marks_successor_unmet(client):
    resp = make_awaiting_rescue_with_follower(client)
    groups = by_id(resp.json())
    assert groups[1]["status"] == "awaiting_rescue"
    # 滞留报告后的整体重排中，后继组随即标记前置未满足、不占通道
    g2 = groups[2]
    assert g2["status"] == "failed"
    assert g2["reason"] == "PRECURSOR_UNMET"
    assert "待救援" in g2["detail"]
    assert g2["route"] == []
    assert client.get("/passages/1/load").json()["minutes"] and all(
        m["used"] <= 10 for m in client.get("/passages/1/load").json()["minutes"])
    # 关系查询同样展示前置未满足（放行依据为 null）
    deps = client.get("/group-dependencies").json()["dependencies"]
    assert deps == [{"group_id": 2, "pioneer_group_id": 1,
                     "release_interval": 2,
                     "pioneer_status": "awaiting_rescue",
                     "release_after_minute": None}]


def test_rescuing_pioneer_keeps_successor_unmet(client):
    make_awaiting_rescue_with_follower(client)
    client.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    resp = client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 4})
    assert resp.status_code == 201
    g2 = by_id(client.get("/plan").json())[2]
    assert g2["status"] == "failed"
    assert g2["reason"] == "PRECURSOR_UNMET"
    assert "救援中" in g2["detail"]


def test_rescue_completion_releases_successor_with_delivery_basis(client):
    make_awaiting_rescue_with_follower(client)
    client.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    task_id = client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2,
        "observed_minute": 4}).json()["task_id"]
    client.post("/tick", json={"minutes": 6})  # 当前分钟 10
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 10})
    assert resp.status_code == 200
    groups = by_id(client.get("/plan").json())
    assert groups[1]["status"] == "rescued"
    assert groups[1]["delivery_minute"] == 10
    # 救援结案后立即重排：后继按 实际送达 10 + 间隔 2 = 12 放行
    g2 = groups[2]
    assert g2["status"] == "scheduled"
    assert g2["departure_minute"] == 12
    assert g2["release_after_minute"] == 12
    assert g2["pioneer_group_id"] == 1
    # 放行依据在关系查询中同步展示（先导状态 rescued）
    deps = client.get("/group-dependencies").json()["dependencies"]
    assert deps[0]["pioneer_status"] == "rescued"
    assert deps[0]["release_after_minute"] == 12


# --------------------------------------------------------------------------
# 重排触发：时间推进 / 现场滞留报告；已出发组不追溯
# --------------------------------------------------------------------------

def test_tick_keeps_release_constraint(client):
    assert register(client, 2, 1, 3).status_code == 201
    resp = client.post("/tick", json={"minutes": 2})
    groups = by_id(resp.json())
    # 组 1 已出发固定（到达 6 不变），组 2 仍以 6+3=9 为可出发分钟
    assert groups[1]["status"] == "fixed"
    assert groups[2]["status"] == "scheduled"
    assert groups[2]["departure_minute"] == 9
    assert groups[2]["release_after_minute"] == 9


def test_delay_report_on_pioneer_rebases_successor(client):
    assert register(client, 2, 1, 3).status_code == 201
    client.post("/tick", json={"minutes": 4})  # 组 1 到达节点 2
    # 组 1 在节点 2 滞留、第 8 分钟恢复：新余程 e2[8,10)，到达 10
    resp = client.post("/groups/1/delay-report",
                       json={"node_id": 2, "recovery_minute": 8})
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[1]["arrival_minute"] == 10
    # 后继组 2 随滞留报告后的重排改用新放行依据 10+3=13
    g2 = groups[2]
    assert g2["departure_minute"] == 13
    assert g2["arrival_minute"] == 19
    assert g2["release_after_minute"] == 13


def test_departed_successor_not_rescheduled(client):
    assert register(client, 2, 1, 0).status_code == 201
    # 推进到组 2 出发（第 6 分钟）之后：组 2 固定，路线与放行决定不追溯
    resp = client.post("/tick", json={"minutes": 7})
    groups = by_id(resp.json())
    assert groups[2]["status"] == "fixed"
    assert groups[2]["departure_minute"] == 6
    assert groups[2]["route"] == [1, 2]
    # 已出发的后继组不可再登记/替换关系
    resp = register(client, 2, 3, 1)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_DEPARTED"
    # 后续重排（新增封闭触及不到已出发组）不改变其路线
    assert client.post("/closures", json={
        "edge_id": 1, "start_minute": 10, "end_minute": 12}
    ).status_code == 201
    groups = by_id(client.get("/plan").json())
    assert groups[2]["route"] == [1, 2]
    assert groups[2]["departure_minute"] == 6


# --------------------------------------------------------------------------
# 场景重置与并发
# --------------------------------------------------------------------------

def test_scenario_reset_clears_dependencies(client):
    assert register(client, 2, 1, 3).status_code == 201
    assert register(client, 3, 2, 2).status_code == 201
    client.post("/scenario", json=CHAIN_SCENARIO)
    assert client.get("/group-dependencies").json()["dependencies"] == []
    groups = by_id(client.get("/plan").json())
    for g in groups.values():
        assert g["pioneer_group_id"] is None
        assert g["release_interval"] is None
        assert g["release_after_minute"] is None
    # 重置后可重新登记同组关系
    assert register(client, 2, 1, 1).status_code == 201


def test_concurrent_registrations_serialize(client):
    from concurrent.futures import ThreadPoolExecutor

    n0 = len(revs(client))
    payloads = [
        {"group_id": 2, "pioneer_group_id": 1, "release_interval": 1},
        {"group_id": 3, "pioneer_group_id": 2, "release_interval": 2},
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda p: client.post("/group-dependencies", json=p), payloads))
    assert all(r.status_code == 201 for r in responses)
    deps = client.get("/group-dependencies").json()["dependencies"]
    assert [(d["group_id"], d["pioneer_group_id"]) for d in deps] == \
        [(2, 1), (3, 2)]
    # 修订连续编号、每条登记各产生一条修订
    entries = revs(client)
    assert len(entries) == n0 + 2
    assert [e["rev"] for e in entries] == list(range(1, len(entries) + 1))
    assert entries[-1]["operation"] == "group-dependencies"
    assert entries[-2]["operation"] == "group-dependencies"
