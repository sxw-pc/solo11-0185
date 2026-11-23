"""离线告警采入与事故单编组。

保留每段波形、设备钟校准、目击人、采样摘要和签认版本。
同一事故单内任意两次告警的校准后时间差不得超过 WINDOW_SECONDS（本题 3 分钟）：
只按相邻告警是否接近建边再取传递闭包，会把跨窗告警低报成一件事故。
"""
from datetime import datetime, timedelta

WINDOW_SECONDS = 180  # 同单任意两次告警校准后时间差的硬上限（3 分钟）


def observed(string):
    return string.get('alarm', False)


def confirmed(string):
    return observed(string) and string.get('alarm_sensor_valid') and string.get('alarm_witnessed')


def _parse(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def calibrated_at(capture):
    """设备钟校准后的统一时刻 = 原始采样时刻 + 校准偏移（毫秒）。"""
    return _parse(capture['sampled_at']) + timedelta(milliseconds=capture.get('clock_offset_ms', 0))


def capture_confirmed(capture):
    """采入告警确认：传感器有效、有目击人、保留波形段与采样时刻。"""
    return (bool(capture.get('sensor_valid'))
            and bool(capture.get('witnessed_by'))
            and bool(capture.get('sampled_at'))
            and bool(capture.get('waveform', {}).get('hash')))


def _representatives(captures):
    """疑似重复波形：同包同波形哈希视为同一告警的重复采入。

    返回 (代表采入列表, {重复采入 id: 代表采入 id})。原件不丢弃，仅编组去重。
    """
    reps, duplicates, order = {}, {}, []
    for cap in sorted(captures, key=lambda c: (calibrated_at(c), c['id'])):
        key = (cap.get('pack_id'), cap['waveform']['hash'])
        if key in reps:
            duplicates[cap['id']] = reps[key]
        else:
            reps[key] = cap['id']
            order.append(cap)
    return order, duplicates


def group_incidents(captures, links=(), window_seconds=WINDOW_SECONDS, protocol='preserve'):
    """把 confirmed 采入告警编成最少事故单，返回可重算的编组图。

    - 每个 confirmed 告警恰好归一事故单；疑似重复波形挂到代表所在单，原件保留。
    - 同单跨度（最早与最晚校准时刻之差）必须 ≤ window_seconds，按全局跨度编组而非相邻差。
    - links 为人工关联（同单约束）；protocol='preserve' 全部执行，
      protocol='minimum' 只执行已封账关联（未封账的经双人审核撤回，即最少独立事故协议）。
    - 已签关联与时间硬约束冲突时在 conflicts 给出冲突子集，单仍保留并标记 window_violation。
    返回 incidents / assignment / alarm_table（告警→事故单→检修设备）/ conflicts /
    duplicates / pending / dangling_links 与最小事故单数 incident_count。
    """
    if protocol not in ('preserve', 'minimum'):
        raise ValueError(f'unknown protocol: {protocol}')
    captures = list(captures)
    confirmed_caps = [c for c in captures if capture_confirmed(c)]
    pending = sorted(c['id'] for c in captures if not capture_confirmed(c))
    incidents, conflicts, duplicates, assignment = [], [], {}, {}
    dangling = []
    packs = sorted({c.get('pack_id') for c in captures}, key=str)
    for pack in packs:
        caps = [c for c in confirmed_caps if c.get('pack_id') == pack]
        ids = {c['id'] for c in caps}
        reps, dups = _representatives(caps)
        duplicates.update(dups)
        rep_of = {c['id']: dups.get(c['id'], c['id']) for c in caps}
        by_id = {c['id']: c for c in reps}
        times = {c['id']: calibrated_at(c) for c in reps}
        parent = {c['id']: c['id'] for c in reps}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        enforced = []
        for link in links:
            link_id = link.get('link_id') or link.get('id')
            if protocol == 'minimum' and not link.get('sealed'):
                continue  # 未封账关联经双人审核撤回，不参与最小编组
            ends = [rep_of.get(a) for a in link.get('alarms', [])]
            if len(ends) < 2 or any(e not in parent for e in ends):
                if any(a in ids for a in link.get('alarms', [])):
                    dangling.append(link_id)
                continue
            enforced.append((link_id, ends))
        for link_id, ends in enforced:
            root = find(ends[0])
            for alarm in ends[1:]:
                other = find(alarm)
                if other != root:
                    parent[other] = root
        link_of = {}
        for link_id, ends in enforced:
            link_of.setdefault(find(ends[0]), set()).add(link_id)
        blocks = {}
        for cid in parent:
            blocks.setdefault(find(cid), []).append(cid)
        block_list = []
        for root, members in blocks.items():
            members.sort(key=lambda a: (times[a], a))
            span = (times[members[-1]] - times[members[0]]).total_seconds()
            block = {'members': members, 'min': times[members[0]], 'max': times[members[-1]],
                     'links': sorted(link_of.get(root, ())), 'violation': span > window_seconds}
            if block['violation']:
                conflicts.append({'pack_id': pack, 'link_ids': block['links'], 'alarms': members,
                                  'span_seconds': span, 'window_seconds': window_seconds})
            block_list.append(block)
        # 最小编组：按校准时刻排序后贪心取窗内最大前缀，强制同单块不可拆。
        block_list.sort(key=lambda b: (b['min'], b['members'][0]))
        groups = []
        for block in block_list:
            if groups and (max(groups[-1]['max'], block['max']) - groups[-1]['min']).total_seconds() <= window_seconds:
                groups[-1]['blocks'].append(block)
                groups[-1]['max'] = max(groups[-1]['max'], block['max'])
            else:
                groups.append({'min': block['min'], 'max': block['max'], 'blocks': [block]})
        for group in groups:
            members = sorted((a for b in group['blocks'] for a in b['members']),
                             key=lambda a: (times[a], a))
            incident_id = f'INC-{pack}-{members[0]}'
            incidents.append({
                'incident_id': incident_id,
                'pack_id': pack,
                'alarms': members,
                'span_seconds': (times[members[-1]] - times[members[0]]).total_seconds(),
                'repair_devices': sorted({dev for a in members
                                          for dev in (by_id[a].get('repair_device') or by_id[a].get('device_id'),)
                                          if dev}),
                'witnesses': sorted({w for a in members for w in by_id[a].get('witnessed_by', [])}),
                'links': sorted({lid for b in group['blocks'] for lid in b['links']}),
                'window_violation': any(b['violation'] for b in group['blocks']),
                'version': max(by_id[a].get('version', 1) for a in members),
            })
            for alarm in members:
                assignment[alarm] = incident_id
        for cap in caps:  # 重复采入挂到代表所在单，原件保留在编组图中
            assignment[cap['id']] = assignment[rep_of[cap['id']]]
    incidents.sort(key=lambda i: i['incident_id'])
    conflicts.sort(key=lambda c: (str(c['pack_id']), c['alarms'][0]))
    table = [{'alarm': c['id'], 'incident': assignment[c['id']],
              'repair_device': c.get('repair_device') or c.get('device_id'),
              'duplicate_of': duplicates.get(c['id'])}
             for c in sorted(confirmed_caps, key=lambda c: c['id'])]
    return {'window_seconds': window_seconds, 'protocol': protocol,
            'incidents': incidents, 'incident_count': len(incidents),
            'conflicts': conflicts, 'duplicates': duplicates,
            'pending': pending, 'dangling_links': sorted(set(dangling)),
            'assignment': assignment, 'alarm_table': table}
