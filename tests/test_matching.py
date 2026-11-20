"""补样分配算法测试：结果与旧递归实现逐票一致，且长链不爆栈。"""
import random

from app.matching import allocate


def _recursive_allocate(topic_order, vacancies, candidates):
    """旧版递归 Kuhn 的参考实现，用于断言迭代版逐票逐序一致。"""
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

    for topic in topic_order:
        for _ in range(max(0, vacancies.get(topic, 0))):
            if not augment(topic, set()):
                break
    return {
        t: [i for i in candidates.get(t, ()) if owner.get(i) == t]
        for t in topic_order
    }


def test_matches_recursive_reference_randomly():
    """随机用例下，迭代版与旧递归版的替补票号及其顺序完全一致（基线为旧版行为）。"""
    rng = random.Random(20261120)
    for _ in range(3000):
        n, m = rng.randint(1, 8), rng.randint(1, 8)
        topics = [f"T{i}" for i in range(n)]
        invoices = [f"C{i}" for i in range(m)]
        vacancies = {t: rng.randint(0, 3) for t in topics}
        candidates = {
            t: rng.sample(invoices, rng.randint(0, len(invoices))) for t in topics
        }
        assert allocate(topics, vacancies, candidates) == _recursive_allocate(
            topics, vacancies, candidates
        )


def test_maximum_cardinality_on_random_graphs():
    """与穷举最优解对照，确认补上的名额总数确实最大（不是贪心近似）。"""
    rng = random.Random(11)
    for _ in range(600):
        n, m = rng.randint(1, 4), rng.randint(1, 5)
        topics = [f"T{i}" for i in range(n)]
        invoices = [f"C{i}" for i in range(m)]
        vacancies = {t: rng.randint(0, 2) for t in topics}
        candidates = {t: rng.sample(invoices, rng.randint(0, m)) for t in topics}
        slots = [t for t in topics for _ in range(vacancies[t])]
        best = 0

        def rec(k, used):
            nonlocal best
            if k == len(slots):
                best = max(best, len(used))
                return
            rec(k + 1, used)  # 该名额留空
            for inv in candidates[slots[k]]:
                if inv not in used:
                    used.add(inv)
                    rec(k + 1, used)
                    used.remove(inv)

        rec(0, set())
        filled = sum(len(v) for v in allocate(topics, vacancies, candidates).values())
        assert filled == best


def test_chain_requiring_deep_reassignment():
    """最后一个专题只能抢 C1，需要沿整条链逐级换位；每张票仍只占一个名额。"""
    n = 1200
    order = [f"T{i}" for i in range(1, n + 1)]
    vacancies = {t: 1 for t in order}
    candidates = {
        f"T{i}": ([f"C{i}", f"C{i + 1}"] if i < n else ["C1"]) for i in range(1, n + 1)
    }
    assign = allocate(order, vacancies, candidates)
    assert sum(len(v) for v in assign.values()) == n
    assert assign["T1"] == ["C2"] and assign[f"T{n}"] == ["C1"]
    assert len({inv for v in assign.values() for inv in v}) == n


def test_chain_shortfall_keeps_max_matching():
    """长链里票少一张：仍按最大匹配补上 n-1 个名额，只有一处留缺额。"""
    n = 1200
    order = [f"T{i}" for i in range(1, n + 1)]
    vacancies = {t: 1 for t in order}
    # 票 C1..C(n-1) 共 n-1 张，专题却有 n 个：最多只能补 n-1 个名额
    candidates = {"T1": ["C1", "C2"], f"T{n}": [f"C{n - 1}"]}
    for i in range(2, n):
        candidates[f"T{i}"] = [f"C{i - 1}", f"C{i}"]
    assign = allocate(order, vacancies, candidates)
    filled = sum(len(v) for v in assign.values())
    assert filled == n - 1
    assert len({inv for v in assign.values() for inv in v}) == filled  # 每张票只占一个名额
    assert sum(1 for v in assign.values() if not v) == 1  # 只有一处缺额
