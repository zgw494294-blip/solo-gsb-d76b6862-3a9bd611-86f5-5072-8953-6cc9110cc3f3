"""可领取审阅工作副本：HTTP 接口端到端测试（FastAPI TestClient）。"""

import importlib

import pytest

# 在导入 app.main 之前指定临时数据库，避免污染本地 data/copies.db
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_DB_PATH", str(tmp_path / "copies.db"))
    import app.main as main
    importlib.reload(main)  # 用新环境变量重新初始化全局仓库
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c
    main._store = None


def ann(id, start, end, label="L"):
    return {"id": id, "start": start, "end": end, "label": label}


def _create(client, text="x" * 30, anns=None):
    r = client.post("/api/copies", json={"text": text,
                                         "annotations": anns or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------- 创建 / 读取 ----------

def test_create_and_get_snapshot(client):
    cid = _create(client, anns=[ann("a", 0, 3), ann("b", 2, 5)])
    snap = client.get(f"/api/copies/{cid}").json()
    assert snap["revision"] == 1
    assert snap["text"] == "x" * 30
    assert [(a["id"], a["holder"]) for a in snap["annotations"]] == \
        [("a", None), ("b", None)]
    assert snap["groups"] == [{"annotation_ids": ["a", "b"], "holder": None}]

    got = client.get(f"/api/copies/{cid}").json()
    assert got["id"] == cid


def test_create_accepts_empty_annotations(client):
    cid = _create(client, text="abc", anns=[])
    snap = client.get(f"/api/copies/{cid}").json()
    assert snap["annotations"] == [] and snap["groups"] == []


def test_get_unknown_copy_404(client):
    r = client.get("/api/copies/deadbeef")
    assert r.status_code == 404


def test_create_invalid_inputs_422(client):
    bodies = [
        {"text": "abc", "annotations": [{"id": "a", "start": 0, "end": 9,
                                          "label": "L"}]},  # 超出文本
        {"text": "abc", "annotations": [{"id": "a", "start": 2, "end": 2,
                                          "label": "L"}]},  # 零长
        {"text": "abc", "annotations": [{"id": "a", "start": 0, "end": 1,
                                          "label": ""}]},  # 空 label
        {"text": "abc", "annotations": [ann("a", 0, 1), ann("a", 1, 2)]},  # 重复 id
        {"text": "abc", "annotations": [{"id": 1, "start": 0, "end": 1,
                                          "label": "L"}]},  # 非字符串 id
        {"text": "abc", "annotations": [{"id": "a", "start": True, "end": 1,
                                          "label": "L"}]},  # 布尔偏移
    ]
    for body in bodies:
        r = client.post("/api/copies", json=body)
        assert r.status_code == 422, body


# ---------- 领取 / 释放 ----------

def test_claim_group_then_release(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7),
                                ann("c", 12, 15)])
    r = client.post(f"/api/copies/{cid}/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a": "alice", "b": "alice", "c": None}
    assert r.json()["revision"] == 2

    # 从组内另一条整组释放
    r = client.post(f"/api/copies/{cid}/annotations/b/release",
                    json={"reviewer": "alice"})
    assert r.status_code == 200
    assert all(a["holder"] is None for a in r.json()["annotations"])


def test_claim_idempotent_same_reviewer_no_revision_bump(client):
    cid = _create(client, anns=[ann("a", 0, 3)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.post(f"/api/copies/{cid}/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 200 and r.json()["revision"] == 2


def test_claim_other_holders_group_409(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.post(f"/api/copies/{cid}/annotations/b/claim",
                    json={"reviewer": "bob"})
    assert r.status_code == 409
    assert r.json()["detail"]["holder"] == "alice"
    # 失败后副本未变
    snap = client.get(f"/api/copies/{cid}").json()
    assert {a["id"]: a["holder"] for a in snap["annotations"]} == \
        {"a": "alice", "b": "alice"}
    assert snap["revision"] == 2


def test_release_unheld_409_and_other_holder_409(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7)])
    r = client.post(f"/api/copies/{cid}/annotations/a/release",
                    json={"reviewer": "alice"})
    assert r.status_code == 409  # 无人持有
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.post(f"/api/copies/{cid}/annotations/b/release",
                    json={"reviewer": "bob"})
    assert r.status_code == 409  # 他人持有


def test_claim_release_bad_reviewer_422(client):
    cid = _create(client, anns=[ann("a", 0, 3)])
    for body in ({"reviewer": ""}, {"reviewer": 5}, {}, {"reviewer": True}):
        r = client.post(f"/api/copies/{cid}/annotations/a/claim", json=body)
        assert r.status_code == 422, body


def test_claim_unknown_annotation_404(client):
    cid = _create(client, anns=[])
    r = client.post(f"/api/copies/{cid}/annotations/x/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 404


def test_claim_unknown_copy_404(client):
    r = client.post("/api/copies/nope/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 404


# ---------- 修改 ----------

def test_modify_range_and_label_holder_only(client):
    cid = _create(client, anns=[ann("a", 0, 2), ann("b", 4, 7)])
    # 未持有不能修改
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 1,
                           "label": "X"})
    assert r.status_code == 409
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 5,
                           "label": "X"})
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()["annotations"]}
    assert (by_id["a"]["start"], by_id["a"]["end"], by_id["a"]["label"]) == \
        (0, 5, "X")
    # 扩大后与 b 正长度重叠 → 未领取的 b 整组并入 alice
    assert by_id["b"]["holder"] == "alice"
    assert r.json()["groups"] == [{"annotation_ids": ["a", "b"],
                                   "holder": "alice"}]


def test_modify_split_group_keeps_holder(client):
    cid = _create(client, anns=[ann("a", 0, 4), ann("b", 3, 7)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 15, "end": 18,
                           "label": "L"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a": "alice", "b": "alice"}
    assert [g["annotation_ids"] for g in r.json()["groups"]] == [["b"], ["a"]]


def test_modify_touching_other_holder_is_allowed(client):
    # a 与 b 相接不连边：a 改成与 b 仍仅相接，允许
    cid = _create(client, anns=[ann("a", 0, 1), ann("b", 1, 4)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/b/claim",
                json={"reviewer": "bob"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 1,
                           "label": "X"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a": "alice", "b": "bob"}


def test_modify_into_other_holder_409_atomic(client):
    cid = _create(client, anns=[ann("a", 0, 1), ann("b", 5, 6)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/b/claim",
                json={"reviewer": "bob"})
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 5,
                           "label": "X"})  # 与 b[5,6) 相接：允许
    assert r.status_code == 200
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 6,
                           "label": "X"})  # 与 b 重叠 → 409
    assert r.status_code == 409
    assert r.json()["detail"]["foreign"] == {"b": "bob"}
    snap = client.get(f"/api/copies/{cid}").json()
    by_id = {a["id"]: a for a in snap["annotations"]}
    assert (by_id["a"]["start"], by_id["a"]["end"]) == (0, 5)  # 上次成功的状态
    assert by_id["b"]["holder"] == "bob"


def test_modify_invalid_422_and_unchanged(client):
    cid = _create(client, text="abc", anns=[ann("a", 0, 2)])
    client.post(f"/api/copies/{cid}/annotations/a/claim",
                json={"reviewer": "alice"})
    for body in ({"reviewer": "alice", "start": 2, "end": 2, "label": "X"},
                 {"reviewer": "alice", "start": -1, "end": 2, "label": "X"},
                 {"reviewer": "alice", "start": 0, "end": 9, "label": "X"},
                 {"reviewer": "alice", "start": 0, "end": 1, "label": ""},
                 {"reviewer": "alice", "start": True, "end": 1, "label": "X"}):
        r = client.patch(f"/api/copies/{cid}/annotations/a", json=body)
        assert r.status_code == 422, body
    snap = client.get(f"/api/copies/{cid}").json()
    a = snap["annotations"][0]
    assert (a["start"], a["end"], a["label"]) == (0, 2, "L")
    assert snap["revision"] == 2


def test_modify_unknown_annotation_404(client):
    cid = _create(client, anns=[])
    r = client.patch(f"/api/copies/{cid}/annotations/x",
                     json={"reviewer": "alice", "start": 0, "end": 1,
                           "label": "X"})
    assert r.status_code == 404


# ---------- 副本批注可直接用于原审阅接口 ----------

def test_copy_annotations_usable_in_review_api(client):
    text = "目标是统一口径，验收时复核样本。"
    cid = _create(client, text=text,
                  anns=[ann("a1", 0, 4, "目标"), ann("a2", 13, 15, "对象")])
    # 领取连通组后修改一条，再把读取到的同一提交状态批注喂给原 /review
    client.post(f"/api/copies/{cid}/annotations/a1/claim",
                json={"reviewer": "alice"})
    snap = client.get(f"/api/copies/{cid}").json()
    r = client.post("/api/review/preview", json={
        "text": snap["text"],
        "max_chars": 10,
        "context_chars": 2,
        "annotations": [{"id": a["id"], "start": a["start"], "end": a["end"],
                         "label": a["label"]} for a in snap["annotations"]],
    })
    assert r.status_code == 200
    assert [f["fragment_id"] for f in r.json()["fragments"]] == \
        ["frag-1", "frag-2"]


# ---------- 特殊批注 id（含 "/" / 空串 / "%2F" 字面量）：请求体定位 ----------

def test_create_accepts_special_string_ids(client):
    # 创建接受任意唯一字符串 id：含 "/" 的、空串、"%2F" 字面量均原样保留
    cid = _create(client, anns=[ann("审阅/1", 0, 3), ann("", 5, 8),
                                ann("a%2Fb", 10, 13), ann("a/b", 15, 18)])
    snap = client.get(f"/api/copies/{cid}").json()
    assert [a["id"] for a in snap["annotations"]] == \
        ["审阅/1", "", "a%2Fb", "a/b"]
    assert all(a["holder"] is None for a in snap["annotations"])


def test_special_ids_not_addressable_via_path(client):
    # 路径段无法表达特殊 id："/" 会拆段，"%2F" 在路由匹配前即被解码回 "/"，
    # 空串构不成路径段——三者都匹配不到路由（404），须改用请求体定位
    cid = _create(client, anns=[ann("审阅/1", 0, 3), ann("", 5, 8)])
    body = {"reviewer": "alice"}
    r = client.post(f"/api/copies/{cid}/annotations/审阅/1/claim", json=body)
    assert r.status_code == 404
    r = client.post(f"/api/copies/{cid}/annotations/%E5%AE%A1%E9%98%85%2F1/claim",
                    json=body)
    assert r.status_code == 404
    r = client.post(f"/api/copies/{cid}/annotations//claim", json=body)
    assert r.status_code == 404
    # 路由未命中不改变副本
    snap = client.get(f"/api/copies/{cid}").json()
    assert all(a["holder"] is None for a in snap["annotations"])
    assert snap["revision"] == 1


def test_special_id_claim_modify_release_full_flow(client):
    cid = _create(client, anns=[ann("审阅/1", 0, 4), ann("b", 3, 7),
                                ann("c", 12, 15)])
    # 领取：特殊 id 与普通 id 同组，整组归 alice
    r = client.post(f"/api/copies/{cid}/annotations/claim",
                    json={"ann_id": "审阅/1", "reviewer": "alice"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"审阅/1": "alice", "b": "alice", "c": None}
    assert r.json()["revision"] == 2
    # 修改：持有者改单条范围与 label
    r = client.patch(f"/api/copies/{cid}/annotations",
                     json={"ann_id": "审阅/1", "reviewer": "alice",
                           "start": 1, "end": 4, "label": "改"})
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()["annotations"]}
    assert (by_id["审阅/1"]["start"], by_id["审阅/1"]["end"],
            by_id["审阅/1"]["label"]) == (1, 4, "改")
    # 释放：从组内普通 id 释放整组（请求体定位普通 id 同样可用）
    r = client.post(f"/api/copies/{cid}/annotations/release",
                    json={"ann_id": "b", "reviewer": "alice"})
    assert r.status_code == 200
    assert all(a["holder"] is None for a in r.json()["annotations"])
    assert r.json()["revision"] == 4


def test_empty_id_claim_modify_release_full_flow(client):
    cid = _create(client, anns=[ann("", 0, 4), ann("b", 10, 13)])
    r = client.post(f"/api/copies/{cid}/annotations/claim",
                    json={"ann_id": "", "reviewer": "alice"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"": "alice", "b": None}
    r = client.patch(f"/api/copies/{cid}/annotations",
                     json={"ann_id": "", "reviewer": "alice",
                           "start": 0, "end": 2, "label": "空id改"})
    assert r.status_code == 200
    a = [x for x in r.json()["annotations"] if x["id"] == ""][0]
    assert (a["start"], a["end"], a["label"]) == (0, 2, "空id改")
    r = client.post(f"/api/copies/{cid}/annotations/release",
                    json={"ann_id": "", "reviewer": "alice"})
    assert r.status_code == 200
    assert all(x["holder"] is None for x in r.json()["annotations"])


def test_percent_literal_and_slash_ids_not_conflated(client):
    # "a%2Fb"（字面量）与 "a/b" 是两个不同的 id，请求体原样传递不得混同
    cid = _create(client, anns=[ann("a%2Fb", 0, 3), ann("a/b", 10, 13)])
    r = client.post(f"/api/copies/{cid}/annotations/claim",
                    json={"ann_id": "a%2Fb", "reviewer": "alice"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a%2Fb": "alice", "a/b": None}
    r = client.post(f"/api/copies/{cid}/annotations/claim",
                    json={"ann_id": "a/b", "reviewer": "bob"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a%2Fb": "alice", "a/b": "bob"}
    # 各自只能由持有者本人释放
    r = client.post(f"/api/copies/{cid}/annotations/release",
                    json={"ann_id": "a%2Fb", "reviewer": "bob"})
    assert r.status_code == 409
    r = client.post(f"/api/copies/{cid}/annotations/release",
                    json={"ann_id": "a%2Fb", "reviewer": "alice"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"a%2Fb": None, "a/b": "bob"}


def test_body_locator_group_and_modify_rules(client):
    # 整组持有权与范围修改规则在请求体定位下同样成立
    cid = _create(client, anns=[ann("审阅/1", 0, 2), ann("b", 4, 7),
                                ann("c", 20, 25)])
    client.post(f"/api/copies/{cid}/annotations/claim",
                json={"ann_id": "审阅/1", "reviewer": "alice"})
    client.post(f"/api/copies/{cid}/annotations/c/claim",
                json={"reviewer": "bob"})
    # 修改后与未领取的 b 正长度重叠 → b 并入 alice 的组
    r = client.patch(f"/api/copies/{cid}/annotations",
                     json={"ann_id": "审阅/1", "reviewer": "alice",
                           "start": 0, "end": 5, "label": "L"})
    assert r.status_code == 200
    assert {a["id"]: a["holder"] for a in r.json()["annotations"]} == \
        {"审阅/1": "alice", "b": "alice", "c": "bob"}
    # 修改后触及他人持有的 c → 409，范围与持有权均不变
    r = client.patch(f"/api/copies/{cid}/annotations",
                     json={"ann_id": "审阅/1", "reviewer": "alice",
                           "start": 0, "end": 21, "label": "L"})
    assert r.status_code == 409
    assert r.json()["detail"]["foreign"] == {"c": "bob"}
    snap = client.get(f"/api/copies/{cid}").json()
    by_id = {a["id"]: a for a in snap["annotations"]}
    assert (by_id["审阅/1"]["start"], by_id["审阅/1"]["end"]) == (0, 5)
    assert by_id["c"]["holder"] == "bob"


def test_body_locator_error_semantics(client):
    cid = _create(client, anns=[ann("审阅/1", 0, 4)])
    base = f"/api/copies/{cid}/annotations"
    # 422：reviewer 非法 / ann_id 缺失 / ann_id 非字符串
    for body in ({"ann_id": "审阅/1", "reviewer": ""},
                 {"ann_id": "审阅/1"},
                 {"ann_id": 5, "reviewer": "alice"},
                 {"ann_id": None, "reviewer": "alice"}):
        r = client.post(f"{base}/claim", json=body)
        assert r.status_code == 422, body
    # 404：批注 / 副本不存在
    r = client.post(f"{base}/claim", json={"ann_id": "nope", "reviewer": "alice"})
    assert r.status_code == 404
    r = client.post("/api/copies/nope/annotations/claim",
                    json={"ann_id": "审阅/1", "reviewer": "alice"})
    assert r.status_code == 404
    # 409：他人持有 / 无人持有
    client.post(f"{base}/claim", json={"ann_id": "审阅/1", "reviewer": "alice"})
    r = client.post(f"{base}/claim", json={"ann_id": "审阅/1", "reviewer": "bob"})
    assert r.status_code == 409 and r.json()["detail"]["holder"] == "alice"
    r = client.post(f"{base}/release", json={"ann_id": "审阅/1", "reviewer": "bob"})
    assert r.status_code == 409
    # 422：修改区间非法，且失败不改变副本
    r = client.patch(base, json={"ann_id": "审阅/1", "reviewer": "alice",
                                 "start": 2, "end": 2, "label": "X"})
    assert r.status_code == 422
    snap = client.get(f"/api/copies/{cid}").json()
    a = snap["annotations"][0]
    assert (a["start"], a["end"], a["label"], a["holder"]) == \
        (0, 4, "L", "alice")
    assert snap["revision"] == 2  # 仅领取产生过一次版本


def test_path_routes_unchanged_for_plain_ids(client):
    # 普通 id 仍走原路径路由，语义不变
    cid = _create(client, anns=[ann("a", 0, 3)])
    r = client.post(f"/api/copies/{cid}/annotations/a/claim",
                    json={"reviewer": "alice"})
    assert r.status_code == 200 and r.json()["revision"] == 2
    r = client.patch(f"/api/copies/{cid}/annotations/a",
                     json={"reviewer": "alice", "start": 0, "end": 2,
                           "label": "X"})
    assert r.status_code == 200
    r = client.post(f"/api/copies/{cid}/annotations/a/release",
                    json={"reviewer": "alice"})
    assert r.status_code == 200
    assert all(a["holder"] is None for a in r.json()["annotations"])
