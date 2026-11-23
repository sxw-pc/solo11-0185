"""拍卖预算约束与全局可行分配验收。

覆盖验收点：
1. 单包 A/80 基线；
2. A/A 超额反例：逐包最高在共享保证金下不可执行；
3. 简单删一包漏成交反例：可行解必须让 B 以 70 填上第二单（总额 150）；
4. 两张分别锁定 LOT1/LOT2 的合法分配（同报价同额度，权属路线不同）；
5. 同号报价重传不出第二单、迟到撤签/高价不改旧交割；
6. 原始报价字节与来源版次可回放（空库重放审计）。
"""
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from secondlife.auction import allocate, winners
from secondlife.bid_capture import dedupe, highest, parse_bid, verify_signature
from secondlife.store import (auction_audit, auction_report, connect,
                              import_auction_file, run_allocation)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'auction-budget.json'
SECRETS = {'A': 'secret-A', 'B': 'secret-B'}


def sign(bidder, lot, amount, bid_no):
    msg = f'{bidder}|{lot}|{amount}|{bid_no}'.encode()
    return hmac.new(SECRETS[bidder].encode(), msg, hashlib.sha256).hexdigest()


def write_events(events):
    fd, path = tempfile.mkstemp(suffix='.json')
    Path(path).write_text(json.dumps(events), encoding='utf-8')
    return path


def cipher_event(cid, bidder, lot, amount, bid_no, witnessed_at):
    raw = f'CIPHER::{bidder}::{lot}::{amount}::{bid_no}'.encode()
    return {'id': cid, 'kind': 'cipher_file', 'bidder': bidder, 'lot_id': lot,
            'cipher_sha': hashlib.sha256(raw).hexdigest(),
            'raw_b64': base64.b64encode(raw).decode(),
            'decrypted_at': witnessed_at, 'witnessed_at': witnessed_at}


def bid_event(eid, bid_no, bidder, lot, amount, witnessed_at, signature=None):
    return {'id': eid, 'kind': 'bid', 'bid_no': bid_no, 'bidder': bidder,
            'lot_id': lot, 'amount': amount, 'witnessed_at': witnessed_at,
            'signature': signature if signature is not None
            else sign(bidder, lot, amount, bid_no)}


def plain_bids():
    return [
        {'event_id': 'e-a1', 'bid_no': 'A1', 'bidder': 'A', 'lot_id': 'LOT1',
         'amount': 80, 'signature_valid': True},
        {'event_id': 'e-a2', 'bid_no': 'A2', 'bidder': 'A', 'lot_id': 'LOT2',
         'amount': 80, 'signature_valid': True},
        {'event_id': 'e-b1', 'bid_no': 'B1', 'bidder': 'B', 'lot_id': 'LOT1',
         'amount': 70, 'signature_valid': True},
        {'event_id': 'e-b2', 'bid_no': 'B2', 'bidder': 'B', 'lot_id': 'LOT2',
         'amount': 70, 'signature_valid': True},
    ]


class SingleLotBaselineTests(unittest.TestCase):
    def test_single_lot_highest_baseline(self):
        rows = [{'bidder': 'A', 'amount': 80}, {'bidder': 'B', 'amount': 70}]
        self.assertEqual(highest(rows), {'bidder': 'A', 'amount': 80})
        # winners 单包行为与既有 seed 测试一致
        self.assertEqual(winners({'LOT1': rows})['LOT1'],
                         {'bidder': 'A', 'amount': 80})

    def test_verify_parse_and_dedupe_primitives(self):
        event = {'id': 'e1', 'bid_no': 'N1', 'bidder': 'A', 'lot_id': 'LOT1',
                 'amount': 80, 'witnessed_at': '2026-11-20T09:05:00Z',
                 'signature': sign('A', 'LOT1', 80, 'N1')}
        self.assertTrue(verify_signature(event, SECRETS['A']))
        self.assertFalse(verify_signature(event, SECRETS['B']))
        record = parse_bid(event, SECRETS)
        self.assertTrue(record['signature_valid'])
        self.assertFalse(record['late'])
        # 同号补传：首件保留，补传识别为 duplicate，不产生第二单
        first = dict(record)
        retx = dict(record, event_id='e2', witnessed_at='2026-11-20T10:05:00Z')
        unique, duplicates = dedupe([retx, first])  # 乱序输入也按见证时间归一
        self.assertEqual([b['event_id'] for b in unique], ['e1'])
        self.assertEqual(duplicates, ['N1'])

    def test_fixture_signatures_verify(self):
        db = connect(':memory:')
        import_auction_file(db, FIXTURE)
        for row in db.execute('SELECT * FROM bids'):
            self.assertTrue(row['signature_valid'], row['bid_no'])
        db.close()


class PerLotHighestCounterexampleTests(unittest.TestCase):
    def setUp(self):
        self.bids = plain_bids()
        self.budgets = {'A': 100, 'B': 100}

    def test_per_lot_highest_is_unexecutable_a_a_overrun(self):
        # 现有 winners 逐包挑最高：两包都给 A，应付 160，超保证金 60
        naive = winners({'LOT1': [{'bidder': 'A', 'amount': 80},
                                  {'bidder': 'B', 'amount': 70}],
                         'LOT2': [{'bidder': 'A', 'amount': 80},
                                  {'bidder': 'B', 'amount': 70}]})
        self.assertEqual([naive['LOT1']['bidder'], naive['LOT2']['bidder']],
                         ['A', 'A'])
        overrun = sum(r['amount'] for r in naive.values()) - self.budgets['A']
        self.assertEqual(overrun, 60)

        # 全局分配不得产生 A/A：每买方中标价不得超已核保证金
        result = allocate(['LOT1', 'LOT2'], self.bids, self.budgets)
        awarded = [result['allocation'][lot]['bidder']
                   for lot in ('LOT1', 'LOT2')]
        self.assertEqual(sorted(awarded), ['A', 'B'])
        for bidder, usage in result['budget_usage'].items():
            self.assertLessEqual(usage['used'], usage['budget'])
            self.assertEqual(usage['used'] + usage['remaining'], usage['budget'])

    def test_dropping_one_lot_is_not_the_solution(self):
        # 反例：事后砍一包只剩 80，漏掉 B/70 的第二单。
        # 可行全局解必须两包都成交，总额 150。
        result = allocate(['LOT1', 'LOT2'], self.bids, self.budgets)
        self.assertEqual(result['total'], 150)
        self.assertTrue(result['optimal'])  # 业务规模内必须证明最优
        self.assertEqual(result['unassigned'], {})
        self.assertEqual(len(result['allocation']), 2)
        amounts = sorted(item['amount'] for item in result['allocation'].values())
        self.assertEqual(amounts, [70, 80])


class PriorityLockProtocolTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        import_auction_file(self.db, FIXTURE)

    def tearDown(self):
        self.db.close()

    def test_protocol_one_lock_keeps_lot1_with_a(self):
        # 已双签但未交割的 A 优先锁：不得自动转让 LOT1
        result = run_allocation(self.db)
        self.assertEqual(result['allocation']['LOT1'],
                         {'bidder': 'A', 'amount': 80, 'bid_no': 'BID-A1',
                          'basis': '优先锁分配'})
        self.assertEqual(result['allocation']['LOT2']['bidder'], 'B')
        self.assertEqual(result['allocation']['LOT2']['amount'], 70)
        self.assertEqual(result['total'], 150)
        self.assertEqual(result['budget_usage']['A']['used'], 80)
        self.assertEqual(result['budget_usage']['B']['used'], 70)

    def test_protocol_two_counter_signed_revocation_migrates_lock(self):
        # A 与站方共同签撤原锁并迁至 LOT2 后重新分配
        revocation = [{'id': 'REV1', 'kind': 'lock_revocation',
                       'lot_id': 'LOT1', 'signed_by': ['A', 'station'],
                       'migrate_to': 'LOT2', 'version': 2}]
        import_auction_file(self.db, write_events(revocation))
        result = run_allocation(self.db)
        self.assertEqual(result['allocation']['LOT1'],
                         {'bidder': 'B', 'amount': 70, 'bid_no': 'BID-B1',
                          'basis': '全局额度可行分配'})
        self.assertEqual(result['allocation']['LOT2'],
                         {'bidder': 'A', 'amount': 80, 'bid_no': 'BID-A2',
                          'basis': '优先锁分配'})
        self.assertEqual(result['total'], 150)
        locks = {r['lot_id']: r['status']
                 for r in self.db.execute('SELECT * FROM priority_locks')}
        self.assertEqual(locks, {'LOT1': 'revoked', 'LOT2': 'active'})

    def test_revocation_without_station_signature_is_rejected(self):
        before = auction_report(self.db)
        bad = [{'id': 'REV-BAD', 'kind': 'lock_revocation', 'lot_id': 'LOT1',
                'signed_by': ['A'], 'version': 2}]
        with self.assertRaises(ValueError):
            import_auction_file(self.db, write_events(bad))
        # 整批回滚：锁仍 active，台账不变
        row = self.db.execute(
            "SELECT status FROM priority_locks WHERE lot_id='LOT1'").fetchone()
        self.assertEqual(row['status'], 'active')
        self.assertEqual(auction_report(self.db), before)

    def test_two_protocols_are_distinct_delivery_tables(self):
        first = run_allocation(self.db)
        revocation = [{'id': 'REV1', 'kind': 'lock_revocation',
                       'lot_id': 'LOT1', 'signed_by': ['A', 'station'],
                       'migrate_to': 'LOT2', 'version': 2}]
        import_auction_file(self.db, write_events(revocation))
        second = run_allocation(self.db)
        # 总额相同（150），但每包权属相反：总额报表不能替代交割表
        self.assertEqual(first['total'], second['total'], 150)
        for lot in ('LOT1', 'LOT2'):
            self.assertNotEqual(first['allocation'][lot]['bidder'],
                                second['allocation'][lot]['bidder'])


class IdempotencyAndImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        import_auction_file(self.db, FIXTURE)

    def tearDown(self):
        self.db.close()

    def test_duplicate_bid_number_cannot_create_second_order(self):
        duplicate = [
            cipher_event('CF-DUP', 'A', 'LOT1', 80, 'BID-A1',
                         '2026-11-20T10:05:00Z'),
            bid_event('EV-DUP', 'BID-A1', 'A', 'LOT1', 80,
                      '2026-11-20T10:05:00Z'),
        ]
        import_auction_file(self.db, write_events(duplicate))
        rows = self.db.execute(
            'SELECT event_id, duplicate FROM bids WHERE bid_no=? ORDER BY event_id',
            ('BID-A1',)).fetchall()
        self.assertEqual(len(rows), 2)  # 补传原件留痕
        self.assertEqual(sorted(r['duplicate'] for r in rows), [0, 1])
        result = run_allocation(self.db)
        self.assertEqual(result['allocation']['LOT1']['bid_no'], 'BID-A1')
        self.assertEqual(result['allocation']['LOT1']['bidder'], 'A')
        self.assertTrue(any(r['bid_no'] == 'BID-A1' and '补传' in r['reason']
                            for r in result['rejected_bids']))

    def test_late_higher_bid_cannot_take_settled_lot(self):
        settlement = [{'id': 'SET1', 'kind': 'settlement', 'lot_id': 'LOT1',
                       'bidder': 'B', 'amount': 70, 'bid_no': 'BID-B1',
                       'basis': '交割', 'settled_at': '2026-11-21T10:00:00Z'}]
        import_auction_file(self.db, write_events(settlement))
        late = [
            cipher_event('CF-LATE', 'A', 'LOT1', 90, 'BID-A90',
                         '2026-11-21T11:05:00Z'),
            bid_event('EV-LATE', 'BID-A90', 'A', 'LOT1', 90,
                      '2026-11-21T11:05:00Z'),
        ]
        import_auction_file(self.db, write_events(late))
        result = run_allocation(self.db)
        self.assertEqual(result['allocation']['LOT1'],
                         {'bidder': 'B', 'amount': 70, 'bid_no': 'BID-B1',
                          'basis': '已交割锁定'})
        self.assertTrue(any(r['bid_no'] == 'BID-A90' and '迟到' in r['reason']
                            for r in result['rejected_bids']))
        row = self.db.execute(
            'SELECT late FROM bids WHERE bid_no=?', ('BID-A90',)).fetchone()
        self.assertEqual(row['late'], 1)

    def test_late_revocation_does_not_change_settled_delivery(self):
        # 先交割 LOT1→B/70，之后 A 与站方才撤签 LOT1 优先锁：旧交割不变
        settlement = [{'id': 'SET1', 'kind': 'settlement', 'lot_id': 'LOT1',
                       'bidder': 'B', 'amount': 70, 'bid_no': 'BID-B1',
                       'basis': '交割', 'settled_at': '2026-11-21T10:00:00Z'}]
        import_auction_file(self.db, write_events(settlement))
        run_allocation(self.db)
        late_revocation = [{'id': 'REV-LATE', 'kind': 'lock_revocation',
                            'lot_id': 'LOT1', 'signed_by': ['A', 'station'],
                            'migrate_to': 'LOT2', 'version': 2}]
        import_auction_file(self.db, write_events(late_revocation))
        result = run_allocation(self.db)
        self.assertEqual(result['allocation']['LOT1']['bidder'], 'B')
        self.assertEqual(result['allocation']['LOT1']['basis'], '已交割锁定')

    def test_forged_signature_is_rejected_but_lot_still_trades(self):
        forged = [bid_event('EV-FORGE', 'BID-X1', 'A', 'LOT2', 99,
                            '2026-11-20T09:30:00Z', signature='deadbeef')]
        import_auction_file(self.db, write_events(forged))
        forged_bid = {'bidder': 'A', 'lot_id': 'LOT2', 'amount': 99,
                      'bid_no': 'BID-X1'}
        self.assertFalse(verify_signature(forged_bid, SECRETS['A']))
        result = run_allocation(self.db)
        self.assertTrue(any(r['bid_no'] == 'BID-X1' and '验签' in r['reason']
                            for r in result['rejected_bids']))
        # 标的不被伪签件污染：LOT2 仍按有效报价成交
        self.assertIsNotNone(result['allocation']['LOT2'])


class ReplayAndVersionTests(unittest.TestCase):
    def test_raw_bid_bytes_and_origin_versions_replay_in_empty_db(self):
        db = connect(':memory:')
        import_auction_file(db, FIXTURE)
        run_allocation(db)
        # 原始报价文件字节可按 sha 取回并与留存哈希一致
        for row in db.execute('SELECT * FROM cipher_files'):
            self.assertEqual(hashlib.sha256(row['raw']).hexdigest(),
                             row['cipher_sha'])
        # 标的有效来源版本随挂牌留存
        listings = {r['lot_id']: dict(r)
                    for r in db.execute('SELECT * FROM listings')}
        self.assertEqual(listings['LOT1']['origin_version'], 1)
        self.assertEqual(listings['LOT1']['test_version'], 1)
        # 空库重放全部原件，派生台账与分配版次逐字节一致
        self.assertTrue(auction_audit(db)['ok'])
        db.close()

    def test_qualification_change_suspends_only_affected_lot(self):
        db = connect(':memory:')
        import_auction_file(db, FIXTURE)
        update = [{'id': 'LIST-LOT2-V2', 'kind': 'listing', 'lot_id': 'LOT2',
                   'pack_id': 'P2', 'origin_version': 2, 'test_version': 1,
                   'status': 'suspended', 'version': 2}]
        import_auction_file(db, write_events(update))
        result = run_allocation(db)
        self.assertIsNone(result['allocation']['LOT2'])
        self.assertIn('待审', result['unassigned']['LOT2'])
        # LOT1 不受影响；已揭标原件与事件版次保留
        self.assertEqual(result['allocation']['LOT1']['bidder'], 'A')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM cipher_files')
                         .fetchone()[0], 4)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM auction_events WHERE kind='bid'").fetchone()[0], 4)
        versions = [r['version'] for r in db.execute(
            "SELECT version FROM listings WHERE lot_id='LOT2' ORDER BY version")]
        self.assertEqual(versions, [2])  # 状态为最新版，历史事件流仍可回放
        self.assertTrue(auction_audit(db)['ok'])
        db.close()

    def test_import_is_idempotent_and_conflicting_event_rolls_back(self):
        db = connect(':memory:')
        first = import_auction_file(db, FIXTURE)
        again = import_auction_file(db, FIXTURE)
        self.assertFalse(first['idempotent'])
        self.assertTrue(again['idempotent'])
        before = auction_report(db)
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        data[0]['budget'] = 999  # 复用事件 ID 改内容
        with self.assertRaises(ValueError):
            import_auction_file(db, write_events(data))
        self.assertEqual(auction_report(db), before)
        db.close()

    def test_allocation_revisions_append_and_replay(self):
        db = connect(':memory:')
        import_auction_file(db, FIXTURE)
        first = run_allocation(db)
        # 撤签迁锁后再出一版：旧版次保留，两版权属相反
        revocation = [{'id': 'REV1', 'kind': 'lock_revocation',
                       'lot_id': 'LOT1', 'signed_by': ['A', 'station'],
                       'migrate_to': 'LOT2', 'version': 2}]
        import_auction_file(db, write_events(revocation))
        second = run_allocation(db)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(second['revision'], 2)
        revisions = [r['revision'] for r in db.execute(
            'SELECT DISTINCT revision FROM allocations ORDER BY revision')]
        self.assertEqual(revisions, [1, 2])
        rev1 = {r['lot_id']: r['bidder'] for r in db.execute(
            'SELECT * FROM allocations WHERE revision=1 ORDER BY lot_id')}
        rev2 = {r['lot_id']: r['bidder'] for r in db.execute(
            'SELECT * FROM allocations WHERE revision=2 ORDER BY lot_id')}
        self.assertEqual(rev1, {'LOT1': 'A', 'LOT2': 'B'})
        self.assertEqual(rev2, {'LOT1': 'B', 'LOT2': 'A'})
        # 空库重放原件并按相同版次重算，物化结果逐字节一致
        audit = auction_audit(db)
        self.assertTrue(audit['ok'])
        self.assertEqual(audit['allocation_revisions'], 2)
        db.close()

    def test_tampered_allocation_is_detected_by_audit(self):
        db = connect(':memory:')
        import_auction_file(db, FIXTURE)
        run_allocation(db)
        db.execute("UPDATE allocations SET bidder='FORGED' WHERE lot_id='LOT1'")
        db.commit()
        self.assertFalse(auction_audit(db)['ok'])
        db.close()


if __name__ == '__main__':
    unittest.main()
