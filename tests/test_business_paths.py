import unittest
from pathlib import Path
from secondlife.store import connect,import_file,report,audit

FIXTURES=Path(__file__).resolve().parents[1]/'fixtures'
class PathTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:')
        for kind,file in [('origin','origin-ok.json'),('test','test-ok.json'),('string','string-ok.json')]: import_file(self.db,kind,FIXTURES/file)
    def tearDown(self): self.db.close()
    def add(self,kind,file): import_file(self.db,kind,FIXTURES/file)
    def result(self,name): return report(self.db)['decisions'][name]['result']
    def test_two_groups_operate_before_failure(self):
        self.assertEqual(self.result('投运条件确认:STR1'),'投运')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
        self.assertTrue(audit(self.db)['ok'])
    def test_late_recall_stops_both_groups(self):
        self.add('origin','origin-recall.json')
        self.assertEqual(self.result('召回范围传播'),'隔离')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('投运条件确认:STR2'),'停运')
    def test_conflicting_owner_claim_freezes_both_groups(self):
        self.add('origin','origin-owner-conflict.json')
        self.assertEqual(self.result('退役包来源裁定'),'隔离')
        self.assertEqual(self.result('来源重复入库防双用'),'争议')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('投运条件确认:STR2'),'停运')
    def test_independent_retest_disagreement_is_not_accepted(self):
        self.add('test','test-retest-conflict.json')
        self.assertEqual(self.result('复测争议裁定:C1'),'争议')
        self.assertEqual(self.result('组串相容配置:STR1'),'待重组')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
    def test_expired_calibration_affects_only_referenced_group(self):
        self.add('test','test-calibration-expired.json')
        self.assertEqual(self.result('检测校准可信裁定:C1'),'无效')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
    def test_capacity_dispersion_rejects_group_without_rejecting_cells(self):
        self.add('test','test-dispersion.json')
        self.assertEqual(self.result('电芯健康分档:C1'),'合格')
        self.assertEqual(self.result('组串相容配置:STR1'),'待重组')
        self.assertEqual(self.result('组串相容配置:STR2'),'相容')
    def test_health_failure_changes_only_referenced_string(self):
        self.add('test','test-health-fail.json')
        self.assertEqual(self.result('电芯健康分档:C1'),'待核或报废')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
    def test_sample_coverage_is_independent_gate(self):
        self.add('test','test-sample-low.json')
        self.assertEqual(self.result('检测批次抽样覆盖:C1'),'不足')
        self.assertEqual(self.result('电芯健康分档:C1'),'待核或报废')
    def test_alarm_spreads_to_same_source_pack(self):
        self.add('string','string-alarm.json')
        self.assertEqual(self.result('告警隔离追责:STR1'),'直接隔离')
        self.assertEqual(self.result('告警隔离追责:STR2'),'同源隔离')
        self.assertEqual(self.result('投运条件确认:STR2'),'停运')
        self.assertEqual(self.result('退役拆组许可:STR2'),'禁止')
    def test_protection_failure_does_not_stop_other_group(self):
        self.add('string','string-protection.json')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
    def test_unsigned_or_expired_station_permit_blocks_local_group(self):
        self.add('string','string-signoff-expired.json')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
    def test_unverified_alarm_holds_one_group_without_spreading(self):
        self.add('string','string-unverified-alarm.json')
        self.assertEqual(self.result('告警隔离追责:STR1'),'待核')
        self.assertEqual(self.result('告警隔离追责'),'待核')
        self.assertEqual(self.result('投运条件确认:STR1'),'停运')
        self.assertEqual(self.result('告警隔离追责:STR2'),'正常')
        self.assertEqual(self.result('投运条件确认:STR2'),'投运')
    def test_each_entry_independent(self):
        for kind,file in [('origin','origin-ok.json'),('test','test-ok.json'),('string','string-ok.json')]:
            with self.subTest(kind=kind):
                solo=connect(':memory:')
                import_file(solo,kind,FIXTURES/file)
                self.assertTrue(report(solo)['decisions'])
                self.assertTrue(report(solo)['entities'])
                solo.close()
