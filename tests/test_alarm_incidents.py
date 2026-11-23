import json
import tempfile
import unittest
from pathlib import Path

from secondlife import rules
from secondlife.alarm_capture import (WINDOW_SECONDS, calibrated_at, capture_confirmed,
                                      confirmed, group_incidents, observed)
from secondlife.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'
ALARM_KINDS = ('alarm_capture', 'alarm_link')


def load_alarm_events():
    return json.loads((FIXTURES / 'alarm-incidents.json').read_text(encoding='utf-8'))


def pack_inputs(pack_id):
    events = load_alarm_events()
    caps = [e for e in events if e['kind'] == 'alarm_capture' and e['pack_id'] == pack_id]
    ids = {c['id'] for c in caps}
    links = [e for e in events if e['kind'] == 'alarm_link' and set(e.get('alarms', [])) <= ids]
    return caps, links


def test_alarm_boolean_baseline_still_holds():
    # 验收基线：string-alarm.json 的 STR1 仍为 confirmed
    row = json.loads((FIXTURES / 'string-alarm.json').read_text(encoding='utf-8'))[0]
    assert observed(row) is True
    assert confirmed(row) is True


def test_p1_captures_calibrated_and_confirmed():
    caps, _ = pack_inputs('P1')
    assert [c['id'] for c in caps] == ['AC-A', 'AC-B', 'AC-C', 'AC-D']
    assert all(capture_confirmed(c) for c in caps)
    # 设备钟独立校准至统一时刻：B 快 5 分钟、C 慢 5 分钟，校准后落在 10:00/10:02/10:04/10:06
    assert [c['sampled_at'][11:16] for c in caps] == ['10:00', '10:07', '09:59', '10:06']
    assert [calibrated_at(c).strftime('%H:%M') for c in caps] == ['10:00', '10:02', '10:04', '10:06']
    assert len({c['waveform']['hash'] for c in caps}) == 4  # 波形各不相同


def test_transitive_closure_underreports_single_incident():
    caps, links = pack_inputs('P1')
    times = sorted((calibrated_at(c), c['id']) for c in caps)
    # 反例：只按相邻差 ≤3 分钟建边取连通分量（streaming sessionization 的传递闭包）
    components = []
    for moment, cid in times:
        if components and (moment - components[-1][-1][0]).total_seconds() <= WINDOW_SECONDS:
            components[-1].append((moment, cid))
        else:
            components.append([(moment, cid)])
    assert len(components) == 1  # A-B-C-D 被当成一件事故，事故次数低报为 1
    span = (times[-1][0] - times[0][0]).total_seconds()
    assert span == 360 and span > WINDOW_SECONDS  # A 与 D 实际相差 6 分钟，违反同单硬约束
    # 全局编组不按相邻连通：满足任意两次 ≤3 分钟的最少事故单数
    graph = group_incidents(caps, links, protocol='minimum')
    assert graph['incident_count'] == 2
    assert all(i['span_seconds'] <= WINDOW_SECONDS for i in graph['incidents'])


def test_minimum_protocol_two_sheets_full_table():
    # 协议一：双人审核后撤回未封账的 B↔C，最少独立事故协议产出两单
    caps, links = pack_inputs('P1')
    graph = group_incidents(caps, links, protocol='minimum')
    assert graph['incident_count'] == 2
    sheets = {i['incident_id']: i for i in graph['incidents']}
    assert sheets['INC-P1-AC-A']['alarms'] == ['AC-A', 'AC-B']
    assert sheets['INC-P1-AC-A']['repair_devices'] == ['DEV-1', 'DEV-2']
    assert sheets['INC-P1-AC-C']['alarms'] == ['AC-C', 'AC-D']
    assert sheets['INC-P1-AC-C']['repair_devices'] == ['DEV-3', 'DEV-4']
    table = {r['alarm']: r for r in graph['alarm_table']}
    assert table['AC-A']['incident'] == 'INC-P1-AC-A' and table['AC-A']['repair_device'] == 'DEV-1'
    assert table['AC-B']['incident'] == 'INC-P1-AC-A' and table['AC-B']['repair_device'] == 'DEV-2'
    assert table['AC-C']['incident'] == 'INC-P1-AC-C' and table['AC-C']['repair_device'] == 'DEV-3'
    assert table['AC-D']['incident'] == 'INC-P1-AC-C' and table['AC-D']['repair_device'] == 'DEV-4'
    # 每个 confirmed 告警恰好归一事故单
    assigned = [a for i in graph['incidents'] for a in i['alarms']]
    assert sorted(assigned) == ['AC-A', 'AC-B', 'AC-C', 'AC-D']
    assert sorted(graph['assignment']) == ['AC-A', 'AC-B', 'AC-C', 'AC-D']


def test_preserve_protocol_three_sheets_full_table():
    # 协议二：已封账场景保留 B↔C，产出三单，不为凑两单篡改旧签认
    caps, links = pack_inputs('P1')
    graph = group_incidents(caps, links, protocol='preserve')
    assert graph['incident_count'] == 3
    sheets = {i['incident_id']: i for i in graph['incidents']}
    assert sheets['INC-P1-AC-A']['alarms'] == ['AC-A']
    assert sheets['INC-P1-AC-A']['repair_devices'] == ['DEV-1']
    assert sheets['INC-P1-AC-B']['alarms'] == ['AC-B', 'AC-C']
    assert sheets['INC-P1-AC-B']['links'] == ['LK-BC']
    assert sheets['INC-P1-AC-B']['repair_devices'] == ['DEV-2', 'DEV-3']
    assert sheets['INC-P1-AC-D']['alarms'] == ['AC-D']
    assert sheets['INC-P1-AC-D']['repair_devices'] == ['DEV-4']
    table = {r['alarm']: r for r in graph['alarm_table']}
    assert [table[a]['incident'] for a in ['AC-A', 'AC-B', 'AC-C', 'AC-D']] == \
        ['INC-P1-AC-A', 'INC-P1-AC-B', 'INC-P1-AC-B', 'INC-P1-AC-D']
    assert all(i['span_seconds'] <= WINDOW_SECONDS for i in graph['incidents'])


def test_sealed_link_conflicting_with_window_reports_subset():
    # 已签关联与时间硬约束冲突：给出冲突子集，单保留并标记
    caps, links = pack_inputs('P2')
    graph = group_incidents(caps, links)
    assert graph['incident_count'] == 1
    conflict = graph['conflicts'][0]
    assert conflict['link_ids'] == ['LK-EF']
    assert conflict['alarms'] == ['AC-E', 'AC-F']
    assert conflict['span_seconds'] == 600
    assert conflict['window_seconds'] == WINDOW_SECONDS
    assert graph['incidents'][0]['window_violation'] is True


def test_duplicate_waveforms_deduped_but_originals_kept():
    caps, links = pack_inputs('P3')
    graph = group_incidents(caps, links)
    assert graph['incident_count'] == 1  # 疑似重复波形去重，不多计事故
    assert graph['duplicates'] == {'AC-H': 'AC-G'}
    table = {r['alarm']: r for r in graph['alarm_table']}
    assert table['AC-H']['duplicate_of'] == 'AC-G'  # 原件保留在编组图中
    assert table['AC-H']['incident'] == table['AC-G']['incident'] == 'INC-P3-AC-G'
    assert graph['pending'] == ['AC-J']  # 未确认采入（传感器无效）不参与编组
    assert 'AC-J' not in graph['assignment']


class IncidentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        for kind, file in [('origin', 'origin-ok.json'), ('test', 'test-ok.json'), ('string', 'string-ok.json')]:
            import_file(self.db, kind, FIXTURES / file)
        import_file(self.db, ALARM_KINDS, FIXTURES / 'alarm-incidents.json')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, payload):
        path = Path(self.tmp.name) / 'late.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return import_file(self.db, ALARM_KINDS, path)

    def decisions(self):
        return report(self.db)['decisions']

    def p1_versions(self):
        return [g for g in report(self.db)['incident_graphs'] if g['scope'] == 'P1']

    def test_isolation_consumption_separated_from_incident_responsibility(self):
        decisions = self.decisions()
        # 事故单的真实同包传播范围投入隔离输入：P1 一个包让两条组串同源隔离
        self.assertEqual(decisions['告警隔离追责:STR1']['result'], '同源隔离')
        self.assertEqual(decisions['告警隔离追责:STR2']['result'], '同源隔离')
        self.assertEqual(decisions['投运条件确认:STR1']['result'], '停运')
        self.assertEqual(decisions['投运条件确认:STR2']['result'], '停运')
        # 事故计数与责任来自编组而非 alarm_packs 大小：一个退役包三张事故单
        self.assertEqual(decisions['事故单编组:P1']['result'], '3单')
        self.assertEqual(decisions['事故单检修责任:INC-P1-AC-A']['result'], 'DEV-1')
        self.assertEqual(decisions['事故单检修责任:INC-P1-AC-B']['result'], 'DEV-2、DEV-3')
        self.assertEqual(decisions['事故单检修责任:INC-P1-AC-D']['result'], 'DEV-4')
        self.assertEqual(decisions['事故单编组总数']['result'], '5单')
        self.assertEqual(decisions['签认时间冲突:P2']['result'], '冲突')
        self.assertEqual(decisions['疑似重复波形:P3']['result'], '1段去重')
        self.assertEqual(decisions['告警采入待核:P3']['result'], '1条待核')
        self.assertTrue(audit(self.db)['ok'])

    def test_alarm_table_recomputable_from_raw_events(self):
        # 告警→事故单→检修设备表可从原始事件重算
        graphs = rules.incident_graphs(load_alarm_events())
        expected = sorted((r for g in graphs.values() for r in g['alarm_table']), key=lambda r: r['alarm'])
        actual = report(self.db)['alarm_table']
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 8)  # P1 四条 + P2 两条 + P3 两条（AC-J 待核不入表）

    def test_late_samples_raise_new_version_without_rewriting_signed_graphs(self):
        v1 = self.p1_versions()
        self.assertEqual([v['version'] for v in v1], [1])
        self.assertEqual(v1[0]['body']['incident_count'], 3)  # 未封账 B↔C 仍有效：{A}/{B,C}/{D}
        # 双人审核撤回未封账的 B↔C：最少独立事故协议产出两单
        self.add([{'id': 'LK-BC-2', 'kind': 'alarm_link', 'link_id': 'LK-BC',
                   'alarms': ['AC-B', 'AC-C'], 'sealed': False, 'retracted': True,
                   'retracted_by': ['operator', 'inspector'],
                   'signed_at': '2026-06-02T10:20:00Z', 'version': 2}])
        self.assertEqual(self.decisions()['事故单编组:P1']['result'], '2单')
        v2 = self.p1_versions()
        self.assertEqual([v['version'] for v in v2], [1, 2])
        self.assertEqual(v2[0]['body'], v1[0]['body'])  # 旧签认图不被暗改
        sheets = {i['incident_id']: i['alarms'] for i in v2[1]['body']['incidents']}
        self.assertEqual(sheets, {'INC-P1-AC-A': ['AC-A', 'AC-B'], 'INC-P1-AC-C': ['AC-C', 'AC-D']})
        # 迟到采样触发新版本，不能暗改已签单
        self.add([{'id': 'AC-I', 'kind': 'alarm_capture', 'pack_id': 'P1',
                   'device_id': 'DEV-8', 'repair_device': 'DEV-8',
                   'sampled_at': '2026-06-02T10:08:00Z', 'clock_offset_ms': 0,
                   'sensor_valid': True, 'witnessed_by': ['operator', 'inspector'],
                   'waveform': {'hash': 'sha256:wf-i08', 'samples': [0.0, 1.4, 1.9, 0.5, 0.0]},
                   'sample_summary': {'peak_a': 1.9, 'duration_ms': 610, 'sample_count': 5},
                   'version': 1}])
        self.assertEqual(self.decisions()['事故单编组:P1']['result'], '3单')
        v3 = self.p1_versions()
        self.assertEqual([v['version'] for v in v3], [1, 2, 3])
        self.assertEqual(v3[0]['body'], v1[0]['body'])
        self.assertEqual(v3[1]['body'], v2[1]['body'])
        sheets = {i['incident_id']: i['alarms'] for i in v3[2]['body']['incidents']}
        self.assertEqual(sheets, {'INC-P1-AC-A': ['AC-A', 'AC-B'],
                                  'INC-P1-AC-C': ['AC-C', 'AC-D'],
                                  'INC-P1-AC-I': ['AC-I']})
        self.assertTrue(audit(self.db)['ok'])

    def test_sealed_link_retraction_is_rejected(self):
        # 已封账关联不得为凑单数撤回
        self.add([{'id': 'LK-EF-2', 'kind': 'alarm_link', 'link_id': 'LK-EF',
                   'alarms': ['AC-E', 'AC-F'], 'sealed': True, 'retracted': True,
                   'retracted_by': ['operator', 'inspector'],
                   'signed_at': '2026-06-02T10:30:00Z', 'version': 2}])
        decisions = self.decisions()
        self.assertEqual(decisions['签认篡改防护']['result'], '拒绝')
        self.assertEqual(decisions['签认时间冲突:P2']['result'], '冲突')  # 已封账关联仍执行
        self.assertEqual(decisions['事故单编组:P2']['result'], '1单')
        p2 = [g for g in report(self.db)['incident_graphs'] if g['scope'] == 'P2']
        self.assertEqual([g['version'] for g in p2], [1])  # 编组未变，不产生新版本
        self.assertTrue(audit(self.db)['ok'])
