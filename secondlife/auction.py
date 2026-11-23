"""挂牌包拍卖分配。

主交付不是“每包各取最高”，而是一张覆盖全部挂牌包的
``包 -> 有效中标方 -> 金额`` 全局分配表，并附未分配原因。

硬约束：
- 每买方中标价总额不得超过已核保证金（跨标的共享额度）；
- 一包只能落一家，同一份报价（同 bid_no）补传不得出第二单；
- 已交割的包不能被迟到高价文件夺走；
- 未双签撤签的优先锁按锁约束分配，签认协议不得自动转让；
- 来源或健康资格失效的包回到待审，不参与分配，原揭标件与版次保留。

目标：在可行约束内最大化成交总额；再以确定性次序打破并列，
保证同一输入永远得到同一张交割表。
"""
from .bid_capture import highest


def winners(lots):
    """基线：逐包独立取最高（不做跨包额度校验）。

    保留给单包基线验收；双包共享额度场景必须改用 :func:`allocate`。
    """
    return {lot: highest(rows) for lot, rows in lots.items()}


def _bid_order(bid):
    return (-bid['amount'], bid['bidder'], bid['event_id'])


def _greedy(lot_choices, budgets, locks):
    """确定性贪心初始可行解：按金额次序尝试，受额度与优先锁约束。"""
    spent = {bidder: 0 for bidder in budgets}
    for _b in {x['bidder'] for choices in lot_choices.values() for x in choices}:
        spent.setdefault(_b, 0)
    assignment = {}
    for bid in sorted((b for choices in lot_choices.values() for b in choices),
                      key=_bid_order):
        lot = bid['lot_id']
        if lot in assignment:
            continue
        lock = locks.get(lot)
        if lock is not None and lock['status'] == 'active' \
                and bid['bidder'] != lock['bidder']:
            continue
        if spent[bid['bidder']] + bid['amount'] <= budgets.get(bid['bidder'], 0):
            assignment[lot] = bid
            spent[bid['bidder']] += bid['amount']
    return assignment


def _greedy_pack(lot_choices, lot_order, budgets, locks):
    """逐标的打包贪心：按处理次序为每个标的选当前额度内最高报价。

    与全局金额贪心互补：紧额度下先锁定高约束标的，常得到更优初始解。
    """
    spent = {bidder: 0 for bidder in budgets}
    for _b in {x['bidder'] for choices in lot_choices.values() for x in choices}:
        spent.setdefault(_b, 0)
    assignment = {}
    for lot in lot_order:
        lock = locks.get(lot)
        for bid in lot_choices.get(lot, []):
            if lock is not None and lock['status'] == 'active' \
                    and bid['bidder'] != lock['bidder']:
                continue
            if spent[bid['bidder']] + bid['amount'] <= budgets.get(bid['bidder'], 0):
                assignment[lot] = bid
                spent[bid['bidder']] += bid['amount']
                break
    return assignment


def _knapsack_zero_one(values, capacity):
    """0/1 背包：capacity 额度下 values 的最大可达金额。

    用大整数位集：第 s 位置 1 表示金额 s 可达，一次位移或即完成一轮。
    """
    if capacity <= 0:
        return 0
    if sum(values) <= capacity:
        return sum(values)
    bits = 1  # 仅金额 0 可达
    mask = (1 << (capacity + 1)) - 1
    for value in values:
        if value <= capacity:
            bits = (bits | (bits << value)) & mask
    return bits.bit_length() - 1


def _dominance_filter(choices):
    """同一标的内：同买方只保留最高报价（低价报价恒被支配）。"""
    best_by_bidder = {}
    for bid in choices:  # 已按金额降序
        best_by_bidder.setdefault(bid['bidder'], bid)
    return list(best_by_bidder.values())


def _lot_priority(lot, lot_choices, budgets, locks):
    """决定标的处理次序：优先锁与紧额度标的先解，尽早产生约束与剪枝。"""
    lock = locks.get(lot)
    locked = 1 if lock is not None and lock['status'] == 'active' else 0
    top = lot_choices[lot][0]['amount']
    headroom = min(budgets.get(b['bidder'], 0) for b in lot_choices[lot])
    tight = top / max(headroom, 1)
    return (-locked, -tight, lot)


def _search(bids, lots, budgets, locks, node_budget=1_000_000):
    """按标的分支定界：在保证金、唯一落槌、优先锁约束下最大化成交总额。

    node_budget 限制搜索节点数；达到预算时返回当前已证最优整数解并标记
    optimal=False（正常业务规模下不会触达，触达时交付的仍是满足全部硬
    约束的可行分配，只是可能错过更优组合）。
    """
    lot_choices = {}
    for bid in sorted(bids, key=_bid_order):
        lot_choices.setdefault(bid['lot_id'], []).append(bid)
    lot_choices = {lot: _dominance_filter(choices)
                   for lot, choices in lot_choices.items()}
    lot_order = sorted(lots, key=lambda lot:
                       _lot_priority(lot, lot_choices, budgets, locks))
    # 两个互补的确定性贪心取更优，抬高初始下界让剪枝尽早生效
    seed_a = _greedy(lot_choices, budgets, locks)
    seed_b = _greedy_pack(lot_choices, lot_order, budgets, locks)
    assignment = seed_a if sum(b['amount'] for b in seed_a.values()) >= \
        sum(b['amount'] for b in seed_b.values()) else seed_b
    best = {'total': sum(b['amount'] for b in assignment.values()),
            'assignment': assignment}
    spent = {bidder: 0 for bidder in budgets}
    for _b in {x['bidder'] for choices in lot_choices.values() for x in choices}:
        spent.setdefault(_b, 0)
    # 快路径：某买方的额度 >= 其在所有标的上最高报价之和时，额度对该买方
    # 永不构成约束；所有买方都满足时，每包独立取最高（尊重优先锁）即可，
    # 退化为 O(报价数)，避免松额度下分支定界的组合爆炸
    exposure = {}
    for lot, choices in lot_choices.items():
        seen = set()
        for bid in choices:  # 已按金额降序，每个买方在该标的的首条即最高
            if bid['bidder'] in seen:
                continue
            seen.add(bid['bidder'])
            exposure[bid['bidder']] = exposure.get(bid['bidder'], 0) + bid['amount']
    if all(budgets.get(bidder, 0) >= cap
           for bidder, cap in exposure.items()):
        direct = {}
        for lot in lot_order:
            lock = locks.get(lot)
            choices = lot_choices.get(lot, [])
            if lock is not None and lock['status'] == 'active':
                choices = [b for b in choices if b['bidder'] == lock['bidder']]
            if choices:
                direct[lot] = choices[0]
        return {'total': sum(b['amount'] for b in direct.values()),
                'assignment': direct, 'optimal': True, 'nodes': 0}
    # 预计算后缀表：suffix_values[idx] 为从第 idx 个标的起，每买方在各
    # 标的的最高报价列表，供背包上界 O(标的数) 取用，避免每节点重建
    suffix_values = [None] * (len(lot_order) + 1)
    suffix_values[len(lot_order)] = {}
    for i in range(len(lot_order) - 1, -1, -1):
        acc = {bidder: list(vals)
               for bidder, vals in suffix_values[i + 1].items()}
        lock = locks.get(lot_order[i])
        seen = set()
        for bid in lot_choices.get(lot_order[i], []):
            if lock is not None and lock['status'] == 'active' \
                    and bid['bidder'] != lock['bidder']:
                continue
            if bid['bidder'] in seen:
                continue
            seen.add(bid['bidder'])
            acc.setdefault(bid['bidder'], []).append(bid['amount'])
        suffix_values[i] = acc
    # 支配备忘录：同 (idx, 已用额度) 下，已见过不低于当前 total 的路径时，
    # 当前路径在剩余子树上的任何扩展都不可能更优（状态转移只取决于剩余额度）
    memo = {}
    ub_cache = {}
    knapsack_cache = {}
    nodes = {'count': 0}

    def knapsack(bidder, values, cap):
        key = (bidder, cap, len(values))
        hit = knapsack_cache.get(key)
        if hit is None:
            hit = _knapsack_zero_one(values, cap)
            knapsack_cache[key] = hit
        return hit

    def upper_bound(idx, total):
        key = (idx, tuple(sorted(spent.items())))
        cached = ub_cache.get(key)
        if cached is None:
            bound = total
            for bidder, values in suffix_values[idx].items():
                cap = budgets.get(bidder, 0) - spent.get(bidder, 0)
                if cap > 0:
                    bound += knapsack(bidder, values, cap)
            cached = bound
            ub_cache[key] = cached
        return cached

    def walk(idx, current, total):
        nodes['count'] += 1
        if nodes['count'] > node_budget:
            return
        if total > best['total']:
            best['total'] = total
            best['assignment'] = dict(current)
        if idx == len(lot_order):
            return
        state = (idx, tuple(sorted(spent.items())))
        prior = memo.get(state)
        if prior is not None and prior >= total:
            return
        memo[state] = total
        # 严格小于才可剪：上界 == 当前最优时，可能存在达到同值的另一路径，
        # 并列次序要求我们继续搜索以得到确定的交割表
        if upper_bound(idx, total) < best['total']:
            return
        lot = lot_order[idx]
        # 分支一：该标的不成交（保留额度给后续标的）
        walk(idx + 1, current, total)
        # 分支二：依次尝试该标的的有效报价（金额降序，确定次序打破并列）
        lock = locks.get(lot)
        for bid in lot_choices.get(lot, []):
            if lock is not None and lock['status'] == 'active' \
                    and bid['bidder'] != lock['bidder']:
                continue
            if spent[bid['bidder']] + bid['amount'] > budgets.get(bid['bidder'], 0):
                continue
            spent[bid['bidder']] += bid['amount']
            current[lot] = bid
            walk(idx + 1, current, total + bid['amount'])
            del current[lot]
            spent[bid['bidder']] -= bid['amount']

    walk(0, {}, 0)
    best['optimal'] = nodes['count'] <= node_budget
    best['nodes'] = nodes['count']
    return best


def allocate(lots, bids, budgets, locks=None, delivered=None, suspended=None):
    """计算全局可行分配表。

    lots:      全部挂牌包编号列表（覆盖性：每个包都必须在结果中出现）。
    bids:      经 bid_capture 采录、验签与去重后的有效报价记录列表，
               late=True 的迟到件不参与解算但原件留痕。
    budgets:   买方编号 -> 已核保证金额度。
    locks:     标的 -> 优先锁（含 bidder 与 status: active/revoked）。
    delivered: 标的 -> 历史交割记录 {'bidder','amount','bid_no','settled_at'}，
               已交割包冻结，迟到高价文件不可夺走。
    suspended: 资格失效、回到待审的标的集合。

    返回字典：
      allocation:   lot -> {'bidder','amount','bid_no','basis'} 或 None
      unassigned:   lot -> 原因
      budget_usage: bidder -> {'budget','used','remaining'}
      rejected_bids: 被拒报价（补传/迟到/伪签）及原因，原件仍在台账留痕
      total:        成交总额
      optimal:      分支定界是否在节点预算内证明最优；False 时返回的仍是
                    满足全部硬约束的可行解，仅可能错过更优组合
    """
    locks = locks or {}
    delivered = dict(delivered or ())
    suspended = set(suspended or ())
    allocations, unassigned, rejected_bids = {}, {}, []

    # 1) 回到待审的包：不参与分配，揭标原件与版次在 store 中保留
    for lot in lots:
        if lot in suspended:
            unassigned[lot] = '来源或健康资格变化，回到待审'

    active_lots = [lot for lot in lots if lot not in suspended]

    # 2) 已交割的包：维持原交割，迟到高价文件不可夺走
    for lot, record in delivered.items():
        if lot in active_lots:
            allocations[lot] = {
                'bidder': record['bidder'], 'amount': record['amount'],
                'bid_no': record['bid_no'], 'basis': '已交割锁定'}

    # 3) 报价资格筛除与未分配原因。
    #    被拒绝的是“这份文件”（补传/迟到/伪签），不是标的本身：
    #    标的仍可用其余有效报价成交。
    timely = []
    for b in bids:
        if b.get('duplicate'):
            rejected_bids.append({'bid_no': b['bid_no'], 'lot_id': b['lot_id'],
                                  'reason': '同号报价补传，不得出第二单'})
        elif b.get('late'):
            rejected_bids.append({'bid_no': b['bid_no'], 'lot_id': b['lot_id'],
                                  'reason': '晚于交割/截拍见证时刻的迟到件'})
        elif not b.get('signature_valid', True):
            rejected_bids.append({'bid_no': b['bid_no'], 'lot_id': b['lot_id'],
                                  'reason': '报价验签失败'})
        else:
            timely.append(b)
    eligible = [b for b in timely
                if b['lot_id'] in active_lots and b['lot_id'] not in delivered]
    locked_lots = {lot for lot, lock in locks.items()
                   if lock.get('status') == 'active'}
    for lot in active_lots:
        if lot in delivered:
            continue
        lot_bids = [b for b in eligible if b['lot_id'] == lot]
        if lot in locked_lots and not any(
                b['bidder'] == locks[lot]['bidder'] for b in lot_bids):
            unassigned[lot] = (
                f"优先锁未撤签，锁给 {locks[lot]['bidder']}，无该方有效报价")
        elif not lot_bids:
            unassigned.setdefault(lot, '无有效报价')

    # 4) 全局解算：额度、唯一落槌、优先锁同时满足
    solvable_lots = [lot for lot in active_lots
                     if lot not in delivered and lot not in unassigned]
    solvable_bids = [b for b in eligible if b['lot_id'] in solvable_lots]
    solution = _search(solvable_bids, solvable_lots, budgets, locks)
    assignment = solution['assignment'] or {}
    for lot in solvable_lots:
        if lot in assignment:
            b = assignment[lot]
            lock = locks.get(lot)
            basis = '优先锁分配' if lock is not None and lock.get('status') == 'active' \
                else '全局额度可行分配'
            allocations[lot] = {'bidder': b['bidder'], 'amount': b['amount'],
                                'bid_no': b['bid_no'], 'basis': basis}
        else:
            unassigned[lot] = '保证金额度约束下无可行成交'

    # 5) 买方额度使用表：成交总额受已核保证金约束
    usage = {}
    for bidder, budget in sorted(budgets.items()):
        used = sum(item['amount'] for item in allocations.values()
                   if item and item['bidder'] == bidder)
        usage[bidder] = {'budget': budget, 'used': used,
                         'remaining': budget - used}

    table = {lot: allocations.get(lot) for lot in lots}
    return {
        'allocation': table,
        'unassigned': {lot: unassigned[lot] for lot in lots if lot in unassigned},
        'rejected_bids': rejected_bids,
        'budget_usage': usage,
        'total': sum(item['amount'] for item in table.values() if item),
        'optimal': solution['optimal'],
    }
