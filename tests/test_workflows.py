import json
import tempfile
import unittest
from pathlib import Path
import secondlife
from secondlife.store import connect,import_file,report,audit

ROOT=Path(__file__).resolve().parents[1]
class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def load(self,command,file): return import_file(self.db,IMPORTS[command],ROOT/'fixtures'/file)
    def test_normal_decisions_and_replay(self):
        for cmd,file in DEMO: self.load(cmd,file)
        actual=report(self.db)
        self.assertGreaterEqual(len(actual['decisions']),10)
        self.assertTrue(audit(self.db)['ok'])
        self.assertTrue(all(d['evidence'] for d in actual['decisions'].values()))
        print('module file', secondlife.__file__)
        self.assertTrue(str(Path(secondlife.__file__).resolve()).startswith(str(ROOT)))
    def test_idempotency_and_conflict_rollback(self):
        cmd,file=DEMO[0]
        first=self.load(cmd,file)
        self.assertTrue(self.load(cmd,file)['idempotent'])
        original=report(self.db)
        data=json.loads((ROOT/'fixtures'/file).read_text(encoding='utf-8'))
        data[0]['value']='changed'
        conflict=Path(self.tmp.name)/'conflict.json'
        conflict.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError): import_file(self.db,IMPORTS[cmd],conflict)
        self.assertEqual(original,report(self.db))
    def test_boundaries_change_outcomes(self):
        for cmd,file in DEMO: self.load(cmd,file)
        before=report(self.db)['decisions']
        for cmd,file in BAD: self.load(cmd,file)
        after=report(self.db)['decisions']
        changed=[name for name in before if before[name]['result']!=after[name]['result']]
        self.assertGreaterEqual(len(changed),3,changed)
        self.assertTrue(audit(self.db)['ok'])
    def test_six_distinct_business_entities(self):
        for cmd,file in DEMO: self.load(cmd,file)
        records=report(self.db)['business_records']
        self.assertGreaterEqual(len(set((r['subsystem'],r['entity_type']) for r in records)),6)
        self.assertTrue(all(r['source_event'] for r in records))
    def test_audit_detects_materialized_state_tampering(self):
        for cmd,file in DEMO: self.load(cmd,file)
        name=next(iter(report(self.db)['decisions']))
        self.db.execute('UPDATE decisions SET result=? WHERE name=?',('FORGED',name))
        self.db.commit()
        self.assertFalse(audit(self.db)['ok'])
    def test_expected_normal_conclusions(self):
        for cmd,file in DEMO: self.load(cmd,file)
        decisions=report(self.db)['decisions']
        for name,expected in NORMAL.items():
            with self.subTest(name=name): self.assertEqual(decisions[name]['result'],expected)
    def test_each_subsystem_changes_own_decision(self):
        for cmd,file,decision in CROSS:
            with self.subTest(decision=decision):
                sample=connect(':memory:')
                for normal_cmd,normal_file in DEMO:
                    import_file(sample,IMPORTS[normal_cmd],ROOT/'fixtures'/normal_file)
                before=report(sample)['decisions'][decision]['result']
                import_file(sample,IMPORTS[cmd],ROOT/'fixtures'/file)
                after=report(sample)['decisions'][decision]['result']
                self.assertNotEqual(before,after)
                sample.close()

IMPORTS={'origin-import': 'origin', 'test-import': 'test', 'string-import': 'string'}
DEMO=[('origin-import', 'origin-ok.json'), ('test-import', 'test-ok.json'), ('string-import', 'string-ok.json')]
BAD=[('origin-import', 'origin-bad.json'), ('test-import', 'test-bad.json'), ('string-import', 'string-bad.json')]
NORMAL={'退役包来源裁定': '接收', '召回范围传播': '未召回', '检测校准可信裁定': '有效', '电芯健康分档': '合格', '复测争议裁定': '裁定', '组串相容配置': '相容', '投运条件确认': '投运', '告警隔离追责': '正常'}
CROSS=[('origin-import', 'origin-bad.json', '投运条件确认'), ('test-import', 'test-bad.json', '投运条件确认'), ('string-import', 'string-bad.json', '投运条件确认')]
