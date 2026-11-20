"""功能测试：登记清册、冲红补样、共享备选、最大匹配、幂等/冲突、重启持久化。"""
import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c


def register(client, round_id="R1", topics=None):
    payload = {
        "round_id": round_id,
        "topics": topics
        or [{"topic_code": "T1", "quota": 2, "samples": ["A1", "A2"], "candidates": ["C1", "C2"]}],
    }
    return client.post("/rounds", json=payload)


def flush(client, batch_no, invoices, round_id="R1"):
    return client.post(f"/rounds/{round_id}/redflush-batches",
                       json={"batch_no": batch_no, "invoice_nos": invoices})


# ---------------------------------------------------------------- 登记

def test_register_and_get_state(client):
    r = register(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["round_id"] == "R1"
    t1 = body["topics"][0]
    assert t1["quota"] == 2 and t1["samples"] == ["A1", "A2"]
    assert t1["candidates"] == ["C1", "C2"] and t1["shortfall"] == 0
    assert body["batches_processed"] == 0

    got = client.get("/rounds/R1")
    assert got.status_code == 200 and got.json() == body


def test_register_idempotent_and_conflict(client):
    assert register(client).status_code == 201
    again = register(client)
    assert again.status_code == 200  # 同内容重登 -> 幂等返回现状
    diff = register(client, topics=[{"topic_code": "T9", "quota": 1, "samples": ["Z1"]}])
    assert diff.status_code == 409  # 同轮次不同清册 -> 冲突


@pytest.mark.parametrize(
    "topics",
    [
        # 专题代码重复
        [{"topic_code": "T1", "quota": 1}, {"topic_code": "T1", "quota": 1}],
        # 同一票号是多个专题的样本
        [{"topic_code": "T1", "quota": 1, "samples": ["X"]},
         {"topic_code": "T2", "quota": 1, "samples": ["X"]}],
        # 同一票号既是样本又是备选
        [{"topic_code": "T1", "quota": 1, "samples": ["X"]},
         {"topic_code": "T2", "quota": 1, "candidates": ["X"]}],
        # 已发样本数超过名额
        [{"topic_code": "T1", "quota": 1, "samples": ["A", "B"]}],
        # 名单内票号重复
        [{"topic_code": "T1", "quota": 2, "samples": ["A", "A"]}],
    ],
)
def test_register_validation(client, topics):
    r = register(client, topics=topics)
    assert r.status_code == 400, r.text


def test_shared_candidates_allowed(client):
    r = register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X", "Y"]},
        {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X", "Z"]},
    ])
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 冲红补样

def test_redflush_basic_backfill(client):
    register(client)
    r = flush(client, "B1", ["A1"])
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["seq"] == 1
    t1 = receipt["topics"][0]
    assert t1["vacancy_before"] == 1 and t1["replacements"] == ["C1"] and t1["shortfall"] == 0
    assert receipt["removed"]["samples"] == {"T1": ["A1"]}
    assert receipt["total_filled"] == 1

    state = client.get("/rounds/R1").json()["topics"][0]
    assert state["samples"] == ["A2", "C1"]  # 仍有效的已发样本照旧，替补补入
    assert state["candidates"] == ["C2"]


def test_shared_candidate_single_slot(client):
    """共享备选 X 只能补一个名额：先登记的专题优先，另一个留缺额。"""
    register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X"]},
        {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X"]},
    ])
    r = flush(client, "B1", ["A1", "B1"])
    receipt = r.json()
    assert receipt["total_filled"] == 1
    t1, t2 = receipt["topics"]
    assert t1["replacements"] == ["X"] and t1["shortfall"] == 0
    assert t2["replacements"] == [] and t2["shortfall"] == 1


def test_max_matching_not_greedy(client):
    """T1 备选 [X,Y]、T2 备选 [X]：最大匹配应让 T1 拿 Y、T2 拿 X，总数 2。"""
    register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X", "Y"]},
        {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X"]},
    ])
    receipt = flush(client, "B1", ["A1", "B1"]).json()
    assert receipt["total_filled"] == 2
    t1, t2 = receipt["topics"]
    assert t1["replacements"] == ["Y"] and t2["replacements"] == ["X"]


def test_flushed_candidate_exits_pool(client):
    register(client, topics=[{"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X"]}])
    r1 = flush(client, "B1", ["X"]).json()  # 冲掉备选 X
    assert r1["removed"]["candidates"] == {"T1": ["X"]}
    assert r1["total_filled"] == 0
    assert client.get("/rounds/R1").json()["topics"][0]["candidates"] == []

    r2 = flush(client, "B2", ["A1"]).json()  # 再冲样本，无备选可补 -> 缺额 1
    assert r2["topics"][0]["shortfall"] == 1


def test_replacement_can_be_flushed_and_refilled(client):
    register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["C1", "C2"]}
    ])
    assert flush(client, "B1", ["A1"]).json()["topics"][0]["replacements"] == ["C1"]
    r2 = flush(client, "B2", ["C1"]).json()  # C1 已是样本，冲掉后再补
    assert r2["removed"]["samples"] == {"T1": ["C1"]}
    assert r2["topics"][0]["replacements"] == ["C2"]
    assert client.get("/rounds/R1").json()["topics"][0]["samples"] == ["C2"]


def test_unknown_invoice_is_noop(client):
    register(client)
    r = flush(client, "B1", ["NOPE-1"]).json()
    assert r["unknown_invoices"] == ["NOPE-1"]
    assert r["removed"] == {"samples": {}, "candidates": {}}
    assert r["total_filled"] == 0
    state = client.get("/rounds/R1").json()["topics"][0]
    assert state["samples"] == ["A1", "A2"] and state["candidates"] == ["C1", "C2"]


# ---------------------------------------------------------------- 幂等与冲突

def test_failed_batch_rolls_back(client, monkeypatch):
    """批处理中途失败：退出操作与回执整批回滚，名单保持原样。"""
    register(client)
    import app.service as service

    def boom(*_a, **_k):
        raise RuntimeError("模拟补样中途失败")

    monkeypatch.setattr(service, "allocate", boom)
    with pytest.raises(RuntimeError):
        flush(client, "B1", ["A1"])
    state = client.get("/rounds/R1").json()
    assert state["topics"][0]["samples"] == ["A1", "A2"]  # 退出操作已回滚
    assert state["topics"][0]["candidates"] == ["C1", "C2"]
    assert state["batches_processed"] == 0  # 回执未写入

def test_redflush_writes_are_atomic(client, monkeypatch):
    """回执写入阶段失败时，已完成的退名单与替补入册一并回滚。"""
    n = 300
    assert register(client, topics=_chain_topics(n)).status_code == 201
    import app.service as service

    real_dumps = service.json.dumps

    def explode(*_a, **_k):
        raise RuntimeError("模拟回执写入失败")

    monkeypatch.setattr(service.json, "dumps", explode)
    with pytest.raises(RuntimeError):
        flush(client, "B1", [f"X{i}" for i in range(1, n + 1)])
    monkeypatch.setattr(service.json, "dumps", real_dumps)

    state = client.get("/rounds/R1").json()
    assert state["batches_processed"] == 0
    assert all(t["shortfall"] == 0 for t in state["topics"])  # 全部仍为原样本
    assert all(len(t["samples"]) == 1 and t["samples"][0] == f"X{i + 1}"
               for i, t in enumerate(state["topics"]))

    # 回滚后可重新提交同一批号，且正常生效
    retry = flush(client, "B1", [f"X{i}" for i in range(1, n + 1)])
    assert retry.status_code == 201 and retry.json()["total_filled"] == n


def test_redflush_idempotent_replay(client):
    register(client, topics=[
        {"topic_code": "T1", "quota": 2, "samples": ["A1", "A2"], "candidates": ["C1", "C2"]}
    ])
    first = flush(client, "B1", ["A2", "A1"])
    assert first.status_code == 201
    replay = flush(client, "B1", ["A1", "A2"])  # 同组票号（换序）重送
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 返回原回执
    assert client.get("/rounds/R1").json()["batches_processed"] == 1


def test_redflush_conflict_on_different_set(client):
    register(client)
    assert flush(client, "B1", ["A1"]).status_code == 201
    conflict = flush(client, "B1", ["A2"])  # 同轮同批号换一组票号
    assert conflict.status_code == 409
    assert client.get("/rounds/R1").json()["batches_processed"] == 1


# ---------------------------------------------------------------- 长链补样

def _chain_topics(n, spare=False):
    """一条真正贯穿全链的备选链：Ti 备选 [Ci, C(i+1)]，最后一个专题只挂 C1。

    补样时 T1..T(n-1) 会按登记顺序先占 C2..Cn，最后 Tn 只能抢 C1，
    必须沿整条链逐级换位才能补满——链长上千时旧递归实现会栈溢出报 500。
    spare=True 时每个专题再挂一张独占的 E(i)，供后续批次冲红后有票可补。
    """
    topics = []
    for i in range(1, n):
        cands = [f"C{i}", f"C{i + 1}"] + ([f"E{i}"] if spare else [])
        topics.append(
            {"topic_code": f"T{i}", "quota": 1, "samples": [f"X{i}"], "candidates": cands}
        )
    last = ["C1"] + ([f"E{n}"] if spare else [])
    topics.append({"topic_code": f"T{n}", "quota": 1, "samples": [f"X{n}"], "candidates": last})
    return topics


def test_long_chain_backfills_all_slots(client):
    """上千专题逐级换位仍能整批补满，不报 500，每张票只占一个名额。"""
    n = 1500
    assert register(client, topics=_chain_topics(n)).status_code == 201
    r = flush(client, "B1", [f"X{i}" for i in range(1, n + 1)])
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["total_filled"] == n
    assert all(t["replacements"] and t["shortfall"] == 0 for t in receipt["topics"])
    # 最后一个专题抢到 C1，链上每个专题依次上移一张
    assert receipt["topics"][-1]["replacements"] == ["C1"]
    assert receipt["topics"][0]["replacements"] == ["C2"]

    state = client.get("/rounds/R1").json()["topics"]
    assert all(t["shortfall"] == 0 for t in state)
    seen = [inv for t in state for inv in t["samples"]]
    assert len(seen) == len(set(seen)) == n  # 每张票只占一个名额


def test_long_chain_shortfall_applies_and_is_reported(client):
    """备选不足时本批照样生效：能补的补满，回执逐专题列出剩余缺额。"""
    n = 1500
    topics = _chain_topics(n) + [
        {"topic_code": "T-NO-CAND", "quota": 1, "samples": ["Y1"], "candidates": []}
    ]
    assert register(client, topics=topics).status_code == 201
    r = flush(client, "B1", [f"X{i}" for i in range(1, n + 1)] + ["Y1"])
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["total_filled"] == n  # 长链全部补满
    assert [t["topic_code"] for t in receipt["topics"]][-1] == "T-NO-CAND"
    assert receipt["topics"][-1]["vacancy_before"] == 1

    short = [t for t in receipt["topics"] if t["shortfall"]]
    assert len(short) == 1 and short[0]["replacements"] == []
    assert short[0]["topic_code"] == "T-NO-CAND"
    assert short[0]["vacancy_before"] == 1 and short[0]["shortfall"] == 1

    state = client.get("/rounds/R1").json()
    assert state["batches_processed"] == 1  # 本批已生效
    assert [(t["topic_code"], t["shortfall"]) for t in state["topics"] if t["shortfall"]] == [
        (short[0]["topic_code"], 1)
    ]


def _recursive_allocate(topic_order, vacancies, candidates):
    """旧版递归 Kuhn 的参考实现：同样输入须得到逐专题相同的替补票号与顺序。"""
    import sys

    owner = {}

    def augment(topic, seen):
        for inv in candidates.get(topic, ()):
            if inv in seen:
                continue
            seen.add(inv)
            prev = owner.get(inv)
            if prev is None or augment(prev, seen):
                owner[inv] = topic
                return True
        return False

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 20000))
    try:
        for topic in topic_order:
            for _ in range(max(0, vacancies.get(topic, 0))):
                if not augment(topic, set()):
                    break
    finally:
        sys.setrecursionlimit(limit)
    return {
        t: [i for i in candidates.get(t, ()) if owner.get(i) == t]
        for t in topic_order
    }


def _expected_assignments(state, flushed):
    """按旧版递归算法推算本批补样结果（票号与顺序）。"""
    inflight = set(flushed)
    order = [t["topic_code"] for t in state["topics"]]
    vacancies = {
        t["topic_code"]: t["quota"] - len([s for s in t["samples"] if s not in inflight])
        for t in state["topics"]
    }
    candidates = {
        t["topic_code"]: [c for c in t["candidates"] if c not in inflight]
        for t in state["topics"]
    }
    return _recursive_allocate(order, vacancies, candidates)


def test_long_chain_followup_batches_match_old_version(client):
    """连续多批：每批的逐专题替补票号与顺序须与旧版递归算法一致。

    本批用掉哪些备选会影响下一批，因此必须逐批在当前名单上比对；
    长链上累计换位后仍要保持同样的选择。
    """
    n = 1200
    assert register(client, topics=_chain_topics(n, spare=True)).status_code == 201
    first = flush(client, "B1", [f"X{i}" for i in range(1, n + 1)]).json()
    # B1 整链换位：Ti 拿 C(i+1)，Tn 抢 C1
    assert first["total_filled"] == n and first["topics"][-1]["replacements"] == ["C1"]

    applied = ["B1"]
    # 后续每批冲掉前一批补回的票 + 另一个专题当前持有的票，验证“上一批用掉
    # 哪些备选影响下一批可用池”，且每批都仍能补上。
    carried = None
    for k, topic in enumerate(["T600", "T700", "T800", "T900"], start=2):
        state = client.get("/rounds/R1").json()
        held = {t["topic_code"]: t["samples"] for t in state["topics"]}
        invoices = [held[topic][0]] + ([carried] if carried else [])
        expected = _expected_assignments(state, invoices)
        assert sum(len(v) for v in expected.values()) >= 1, f"B{k} 应确实补上票"
        resp = flush(client, f"B{k}", invoices)
        assert resp.status_code == 201, resp.text
        receipt = resp.json()
        assert [t["topic_code"] for t in receipt["topics"]] == list(expected)
        assert receipt["total_filled"] == sum(len(v) for v in expected.values())
        for t in receipt["topics"]:
            assert t["replacements"] == expected[t["topic_code"]], f"B{k}"
            assert t["shortfall"] == t["vacancy_before"] - len(expected[t["topic_code"]])
        carried = next(
            (t["replacements"][0] for t in receipt["topics"] if t["replacements"]), None
        )

        applied.append(f"B{k}")
        after = client.get("/rounds/R1").json()
        assert after["batches_processed"] == len(applied)
        for t_after, t_receipt in zip(after["topics"], receipt["topics"]):
            got = [i for i in t_after["samples"] if i in t_receipt["replacements"]]
            assert got == t_receipt["replacements"]
            assert t_after["shortfall"] == t_receipt["shortfall"]
            assert all(i not in t_after["samples"] for i in invoices)


# ---------------------------------------------------------------- 查询与持久化

def test_receipts_query(client):
    register(client)
    flush(client, "B1", ["A1"])
    flush(client, "B2", ["A2"])
    one = client.get("/rounds/R1/redflush-batches/B1")
    assert one.status_code == 200 and one.json()["batch_no"] == "B1"
    allr = client.get("/rounds/R1/redflush-batches")
    assert [r["batch_no"] for r in allr.json()] == ["B1", "B2"]
    assert client.get("/rounds/R1/redflush-batches/BX").status_code == 404
    assert client.get("/rounds/NOPE").status_code == 404
    assert client.get("/rounds/NOPE/redflush-batches").status_code == 404


def test_restart_persistence(tmp_path, monkeypatch):
    """重启（新建连接/新应用实例）后仍可查现行名单和回执。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "restart.db"))
    db.init_db()
    with TestClient(app) as c1:
        register(c1)
        assert flush(c1, "B1", ["A1"]).status_code == 201
    with TestClient(app) as c2:  # 模拟重启：同一数据库文件，全新连接
        state = c2.get("/rounds/R1").json()
        assert state["topics"][0]["samples"] == ["A2", "C1"]
        assert state["batches_processed"] == 1
        receipt = c2.get("/rounds/R1/redflush-batches/B1").json()
        assert receipt["topics"][0]["replacements"] == ["C1"]
