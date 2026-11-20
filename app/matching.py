"""补样名额分配：二部图最大匹配（Kuhn 增广路算法，迭代实现）。

左侧是各专题的缺额（每个缺额一个槽位），右侧是备选发票；
票在某专题备选名单内即有一条边。每张票最多占一个名额，
算法保证本批补上的名额总数最大，且在给定次序下结果确定：
专题按登记顺序处理，每个专题的备选按登记顺序尝试。

增广路用显式栈迭代求解，逐层展开递归调用、失败回溯，
避免长链（上千个专题逐级换位）时递归过深触发 RecursionError
导致整批冲红 500。迭代版与递归版的下标推进、seen 集合和
沿途换位顺序完全一致，因此同一清册、同一提交顺序下结果不变。
"""
from __future__ import annotations


def _augment(start, candidates, owner) -> bool:
    """从 start 专题找一条增广路；找到则沿路换位并返回 True。

    等价于下面的递归 Kuhn：

        def augment(topic, seen):
            for inv in candidates[topic]:
                if inv in seen:
                    continue
                seen.add(inv)
                prev = owner.get(inv)
                if prev is None or augment(prev, seen):
                    owner[inv] = topic
                    return True
            return False

    帧结构：[专题, 下一个待尝试的备选下标, 深入子帧时选中的票]。
    子帧成功返回后，当前帧把选中的票记到自己名下并向父帧返回成功；
    子帧失败则当前帧从保存的下标继续尝试其余备选。
    """
    frames: list[list] = [[start, 0, None]]
    seen: set[str] = set()
    # ret: 刚出栈的子帧返回给栈顶帧的结果；None 表示栈顶帧需要继续扫描
    ret: bool | None = None

    while frames:
        frame = frames[-1]
        topic, idx, pending = frame

        if ret is not None:
            if ret:
                # 子帧成功：当前帧接上深入前选中的那张票，并向上返回成功
                owner[pending] = topic
                frames.pop()
                ret = True
            else:
                # 子帧失败：清掉选中的票，从保存的位置继续尝试
                frame[2] = None
                ret = None
            continue

        cands = candidates.get(topic, ())
        while idx < len(cands):
            inv = cands[idx]
            idx += 1
            if inv in seen:
                continue
            seen.add(inv)
            prev = owner.get(inv)
            if prev is None:
                # 找到空闲票：当前帧占用它并向上返回成功
                frame[1] = idx
                owner[inv] = topic
                frames.pop()
                ret = True
                break
            # 票被 prev 占用：先为 prev 另找一张，深度优先深入
            frame[1] = idx
            frame[2] = inv
            frames.append([prev, 0, None])
            break
        else:
            # 备选已穷尽，当前帧无增广路
            frames.pop()
            ret = False

    return bool(ret)


def allocate(topic_order, vacancies, candidates):
    """返回 {topic_code: [分得票号, ...]}（按备选登记顺序排列）。

    topic_order: 专题代码列表（登记顺序）
    vacancies:   {topic_code: 待补名额数}
    candidates:  {topic_code: [可用备选票号, ...]}（登记顺序）
    """
    owner: dict[str, str] = {}  # 票号 -> 占用它的专题

    for topic in topic_order:
        for _ in range(max(0, vacancies.get(topic, 0))):
            if not _augment(topic, candidates, owner):
                break

    result = {}
    for topic in topic_order:
        result[topic] = [
            inv for inv in candidates.get(topic, ()) if owner.get(inv) == topic
        ]
    return result
