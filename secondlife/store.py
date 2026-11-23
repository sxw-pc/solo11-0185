import base64
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import rules
from . import auction as auction_mod
from .bid_capture import verify_signature

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (sha TEXT PRIMARY KEY, kind TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (name TEXT PRIMARY KEY, result TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_state (subsystem TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(subsystem,entity_id));
CREATE TABLE IF NOT EXISTS decision_history (revision INTEGER NOT NULL, name TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(revision,name));
CREATE TABLE IF NOT EXISTS business_record (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_event TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id,version));
CREATE TABLE IF NOT EXISTS auction_sources (sha TEXT PRIMARY KEY, kind TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS auction_events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, sha TEXT, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bidders (bidder TEXT PRIMARY KEY, budget INTEGER NOT NULL, secret TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cipher_files (cipher_sha TEXT PRIMARY KEY, bidder TEXT NOT NULL, lot_id TEXT NOT NULL, raw BLOB NOT NULL, decrypted_at TEXT, witnessed_at TEXT NOT NULL, source_event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bids (bid_no TEXT NOT NULL, event_id TEXT NOT NULL, bidder TEXT NOT NULL, lot_id TEXT NOT NULL, amount INTEGER NOT NULL, cipher_sha TEXT, witnessed_at TEXT NOT NULL, signature_valid INTEGER NOT NULL, late INTEGER NOT NULL DEFAULT 0, duplicate INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(bid_no,event_id));
CREATE TABLE IF NOT EXISTS priority_locks (lot_id TEXT PRIMARY KEY, bidder TEXT NOT NULL, status TEXT NOT NULL, signed_by TEXT NOT NULL, version INTEGER NOT NULL, updated_event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listings (lot_id TEXT PRIMARY KEY, pack_id TEXT NOT NULL, origin_version INTEGER NOT NULL, test_version INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL, source_event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlements (lot_id TEXT PRIMARY KEY, bidder TEXT NOT NULL, amount INTEGER NOT NULL, bid_no TEXT NOT NULL, basis TEXT NOT NULL, settled_at TEXT NOT NULL, source_event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations (revision INTEGER NOT NULL, lot_id TEXT NOT NULL, bidder TEXT, amount INTEGER, bid_no TEXT, basis TEXT, unassigned_reason TEXT, PRIMARY KEY(revision,lot_id));
"""

AUCTION_KINDS = {'bidder_registration', 'cipher_file', 'bid', 'priority_lock',
                 'lock_revocation', 'settlement', 'listing', 'allocation_run'}


def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db

def import_file(db, kind, file):
    raw=Path(file).read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,list) or not payload:
        raise ValueError('source must contain a nonempty event list')
    if db.execute('SELECT 1 FROM source_files WHERE sha=?',(sha,)).fetchone():
        return {'sha':sha,'inserted':0,'idempotent':True}
    db.execute('SAVEPOINT ingest')
    try:
        seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        for event in payload:
            if event.get('kind')!=kind or not event.get('id'):
                raise ValueError('event kind/id mismatch')
            body=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT body FROM events WHERE event_id=?',(event['id'],)).fetchone()
            if old:
                if old[0]!=body: raise ValueError('same event id with different content')
                continue
            seq+=1
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',(event['id'],kind,body,sha,seq))
        db.execute('INSERT INTO source_files(sha,kind,raw) VALUES (?,?,?)',(sha,kind,raw))
        rebuild(db)
        db.execute('RELEASE SAVEPOINT ingest')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT ingest')
        db.execute('RELEASE SAVEPOINT ingest')
        db.rollback()
        raise
    return {'sha':sha,'inserted':len(payload),'idempotent':False}

def rebuild(db):
    events=[json.loads(row[0]) for row in db.execute('SELECT body FROM events ORDER BY seq')]
    decisions,states=rules.calculate(events)
    revision=db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]+1
    db.execute('DELETE FROM decisions')
    db.execute('DELETE FROM entity_state')
    db.execute('DELETE FROM business_record')
    for name,item in sorted(decisions.items()):
        db.execute('INSERT INTO decisions VALUES (?,?,?,?)',(name,item['result'],item['reason'],json.dumps(item['evidence'],ensure_ascii=False,sort_keys=True)))
        db.execute('INSERT OR REPLACE INTO decision_history VALUES (?,?,?,?)',(revision,name,item['result'],item['reason']))
    for subsystem,entity,state,version in states:
        db.execute('INSERT INTO entity_state VALUES (?,?,?,?)',(subsystem,entity,state,version))
    for event in events:
        for subsystem,entity_type,entity_id,body in rules.business_records(event):
            db.execute('INSERT OR REPLACE INTO business_record VALUES (?,?,?,?,?,?)',(subsystem,entity_type,entity_id,event['id'],event.get('version',1),json.dumps(body,ensure_ascii=False,sort_keys=True)))

def report(db):
    return {'decisions':{r['name']:{'result':r['result'],'reason':r['reason'],'evidence':json.loads(r['evidence'])} for r in db.execute('SELECT * FROM decisions ORDER BY name')},
            'entities':[dict(r) for r in db.execute('SELECT * FROM entity_state ORDER BY subsystem,entity_id')],
            'business_records':[dict(r) for r in db.execute('SELECT * FROM business_record ORDER BY subsystem,entity_type,entity_id,version')],
            'sources':[dict(r) for r in db.execute('SELECT sha,kind FROM source_files ORDER BY sha')]}

def audit(db):
    before=report(db)
    source_rows=list(db.execute('SELECT kind,raw FROM source_files ORDER BY rowid'))
    memory=connect(':memory:')
    for kind,raw in source_rows:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw); path=tmp.name
        try: import_file(memory,kind,path)
        finally: Path(path).unlink()
    after=report(memory)
    return {'ok':before==after,'source_count':len(source_rows),'decision_count':len(before['decisions'])}


# ---------------------------------------------------------------- 拍卖台账

def _instant(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def import_auction_file(db, file):
    """导入拍卖事件文件（报价、核额、锁、交割、挂牌版次）。

    原件字节整体留存并按 sha256 幂等；同事件 ID 异内容整批回滚。
    派生表每次从完整事件流重建，已生成的分配版次与交割记录不被删除。
    """
    raw=Path(file).read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,list) or not payload:
        raise ValueError('auction source must contain a nonempty event list')
    if db.execute('SELECT 1 FROM auction_sources WHERE sha=?',(sha,)).fetchone():
        return {'sha':sha,'inserted':0,'idempotent':True}
    db.execute('SAVEPOINT auction_ingest')
    try:
        seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM auction_events').fetchone()[0]
        for event in payload:
            if event.get('kind') not in AUCTION_KINDS or not event.get('id'):
                raise ValueError('auction event kind/id mismatch')
            if event.get('kind')=='allocation_run':
                raise ValueError('allocation_run 事件只能由内部分配生成，不得外部导入')
            body=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT body FROM auction_events WHERE event_id=?',(event['id'],)).fetchone()
            if old:
                if old[0]!=body: raise ValueError('same event id with different content')
                continue
            seq+=1
            db.execute('INSERT INTO auction_events VALUES (?,?,?,?,?)',
                       (event['id'],event['kind'],body,sha,seq))
        db.execute('INSERT INTO auction_sources(sha,kind,raw) VALUES (?,?,?)',
                   (sha,'auction',raw))
        rebuild_auction(db)
        db.execute('RELEASE SAVEPOINT auction_ingest')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT auction_ingest')
        db.execute('RELEASE SAVEPOINT auction_ingest')
        db.rollback()
        raise
    return {'sha':sha,'inserted':len(payload),'idempotent':False}


def rebuild_auction(db):
    """从拍卖事件流重建派生表；分配版次与交割事实只追加、不重写。"""
    events=[json.loads(r[0]) for r in
            db.execute("SELECT body FROM auction_events WHERE kind!='allocation_run' ORDER BY seq")]
    db.execute('DELETE FROM bidders')
    db.execute('DELETE FROM cipher_files')
    db.execute('DELETE FROM bids')
    db.execute('DELETE FROM priority_locks')
    db.execute('DELETE FROM listings')
    db.execute('DELETE FROM settlements')

    settled_at = {}
    for e in events:
        kind=e['kind']
        if kind=='bidder_registration':
            db.execute('INSERT OR REPLACE INTO bidders VALUES (?,?,?,?)',
                       (e['bidder'],e['budget'],e['secret'],e.get('version',1)))
        elif kind=='cipher_file':
            raw=base64.b64decode(e['raw_b64'])
            actual=hashlib.sha256(raw).hexdigest()
            if actual != e['cipher_sha']:
                raise ValueError(f"cipher sha mismatch in event {e['id']}")
            db.execute('INSERT OR REPLACE INTO cipher_files VALUES (?,?,?,?,?,?,?)',
                       (e['cipher_sha'],e['bidder'],e['lot_id'],raw,
                        e.get('decrypted_at'),e['witnessed_at'],e['id']))
        elif kind=='settlement':
            settled_at[e['lot_id']]=e['settled_at']
            db.execute('INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?,?)',
                       (e['lot_id'],e['bidder'],e['amount'],e['bid_no'],
                        e['basis'],e['settled_at'],e['id']))
        elif kind=='listing':
            db.execute('INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?)',
                       (e['lot_id'],e['pack_id'],e['origin_version'],
                        e['test_version'],e['status'],e.get('version',1),e['id']))

    # 优先锁：撤签必须买方与站方共同签署；迁移在目标标的生成新锁
    for e in events:
        if e['kind']=='priority_lock':
            db.execute('INSERT OR REPLACE INTO priority_locks VALUES (?,?,?,?,?,?)',
                       (e['lot_id'],e['bidder'],'active',
                        json.dumps(e.get('signed_by',[]),ensure_ascii=False),
                        e.get('version',1),e['id']))
        elif e['kind']=='lock_revocation':
            signed=set(e.get('signed_by',[]))
            row=db.execute('SELECT * FROM priority_locks WHERE lot_id=?',
                           (e['lot_id'],)).fetchone()
            if row is None:
                raise ValueError(f"revocation without prior lock in event {e['id']}")
            if row['bidder'] not in signed or 'station' not in signed:
                raise ValueError('锁撤签必须买方与站方共同签署')
            db.execute('UPDATE priority_locks SET status=?,signed_by=?,updated_event=? WHERE lot_id=?',
                       ('revoked',json.dumps(sorted(signed),ensure_ascii=False),
                        e['id'],e['lot_id']))
            if e.get('migrate_to'):
                db.execute('INSERT OR REPLACE INTO priority_locks VALUES (?,?,?,?,?,?)',
                           (e['migrate_to'],row['bidder'],'active',
                            json.dumps(sorted(signed),ensure_ascii=False),
                            row['version']+1,e['id']))

    # 报价：验签、同号补传去重、迟到标记；原字节已在 cipher_files 留存
    secrets={r['bidder']:r['secret'] for r in db.execute('SELECT * FROM bidders')}
    first_seen={}
    for e in events:
        if e['kind']!='bid':
            continue
        valid=1 if verify_signature(e,secrets.get(e['bidder'])) else 0
        # 同号补传不得出第二单：首见证件有效，补传件留痕（duplicate=1）不参与解算
        duplicate=0 if e['bid_no'] not in first_seen else 1
        first_seen.setdefault(e['bid_no'],e['id'])
        late=int(e['lot_id'] in settled_at and
                 _instant(e['witnessed_at']) > _instant(settled_at[e['lot_id']]))
        db.execute('INSERT OR REPLACE INTO bids VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (e['bid_no'],e['id'],e['bidder'],e['lot_id'],e['amount'],
                    e.get('cipher_sha'),e['witnessed_at'],valid,late,duplicate))


def _load_allocation_inputs(db):
    bids=[{
        'event_id':r['event_id'],'bid_no':r['bid_no'],'bidder':r['bidder'],
        'lot_id':r['lot_id'],'amount':r['amount'],'cipher_sha':r['cipher_sha'],
        'witnessed_at':r['witnessed_at'],'signature_valid':bool(r['signature_valid']),
        'late':bool(r['late']),'duplicate':bool(r['duplicate']),
    } for r in db.execute('SELECT * FROM bids ORDER BY witnessed_at,event_id')]
    budgets={r['bidder']:r['budget'] for r in db.execute('SELECT * FROM bidders')}
    locks={r['lot_id']:{'bidder':r['bidder'],'status':r['status'],
                        'signed_by':json.loads(r['signed_by']),'version':r['version']}
           for r in db.execute('SELECT * FROM priority_locks')}
    delivered={r['lot_id']:{'bidder':r['bidder'],'amount':r['amount'],
                            'bid_no':r['bid_no'],'basis':r['basis'],
                            'settled_at':r['settled_at']}
               for r in db.execute('SELECT * FROM settlements')}
    suspended={r['lot_id'] for r in db.execute("SELECT * FROM listings WHERE status='suspended'")}
    lots=[r['lot_id'] for r in db.execute('SELECT DISTINCT lot_id FROM listings ORDER BY lot_id')]
    return lots,bids,budgets,locks,delivered,suspended


def run_allocation(db):
    """解算并追加一版分配表；旧版次保留，已交割包按历史事实冻结。

    每次解算同时落一条只追加的 allocation_run 事件：空库重放原件时
    据此在同一时刻重算，保证分配版次本身也可审计回放。
    """
    lots,bids,budgets,locks,delivered,suspended=_load_allocation_inputs(db)
    result=auction_mod.allocate(lots,bids,budgets,locks,delivered,suspended)
    revision=(db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM allocations')
              .fetchone()[0])
    for lot in lots:
        item=result['allocation'].get(lot)
        if item:
            db.execute('INSERT INTO allocations VALUES (?,?,?,?,?,?,?)',
                       (revision,lot,item['bidder'],item['amount'],
                        item['bid_no'],item['basis'],None))
        else:
            db.execute('INSERT INTO allocations VALUES (?,?,?,?,?,?,?)',
                       (revision,lot,None,None,None,None,
                        result['unassigned'].get(lot,'未分配')))
    seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM auction_events').fetchone()[0]+1
    run_event={'id':f'RUN-{revision}','kind':'allocation_run','revision':revision}
    body=json.dumps(run_event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    db.execute('INSERT INTO auction_events VALUES (?,?,?,?,?)',
               (run_event['id'],'allocation_run',body,None,seq))
    db.commit()
    result['revision']=revision
    return result


def auction_report(db):
    """拍卖台账全量视图：原件、报价、锁、版次、各版分配与买方额度。"""
    latest=db.execute('SELECT COALESCE(MAX(revision),0) FROM allocations').fetchone()[0]
    return {
        'bidders':[dict(r) for r in db.execute('SELECT * FROM bidders ORDER BY bidder')],
        'cipher_files':[{'cipher_sha':r['cipher_sha'],'bidder':r['bidder'],
                         'lot_id':r['lot_id'],'decrypted_at':r['decrypted_at'],
                         'witnessed_at':r['witnessed_at'],'source_event':r['source_event']}
                        for r in db.execute('SELECT * FROM cipher_files ORDER BY cipher_sha')],
        'bids':[dict(r) for r in db.execute('SELECT * FROM bids ORDER BY lot_id,amount DESC,bidder')],
        'locks':[dict(r) for r in db.execute('SELECT * FROM priority_locks ORDER BY lot_id')],
        'listings':[dict(r) for r in db.execute('SELECT * FROM listings ORDER BY lot_id')],
        'settlements':[dict(r) for r in db.execute('SELECT * FROM settlements ORDER BY lot_id')],
        'allocations':[dict(r) for r in db.execute('SELECT * FROM allocations ORDER BY revision,lot_id')],
        'current_revision':latest,
        'sources':[dict(r) for r in db.execute('SELECT sha,kind FROM auction_sources ORDER BY sha')],
    }


def auction_audit(db):
    """空库重放全部拍卖原件，比对派生台账（含分配版次），检测物化篡改。

    业务原件按入库顺序逐个重放；在重放每个原件之前，先补跑所有 seq 落在
    “上一原件之后、本原件之前”的 allocation_run。这样每次解算都作用在与
    当时一致的事件状态上，由同一确定性引擎逐字节复现各分配版次。
    """
    before=auction_report(db)
    memory=connect(':memory:')
    import tempfile
    run_seqs=[r[0] for r in db.execute(
        "SELECT seq FROM auction_events WHERE kind='allocation_run' ORDER BY seq")]
    business=list(db.execute(
        "SELECT s.raw, COALESCE(MIN(e.seq),0) AS min_seq "
        "FROM auction_sources s LEFT JOIN auction_events e ON e.sha=s.sha "
        "WHERE s.kind!='allocation_run' GROUP BY s.rowid ORDER BY s.rowid"))
    done=0
    for raw, min_seq in business:
        # 本原件事件入库之前发生的解算，先在上一状态上补跑
        due=sum(1 for s in run_seqs if s < min_seq)
        while done < due:
            run_allocation(memory)
            done+=1
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw); path=tmp.name
        try: import_auction_file(memory,path)
        finally: Path(path).unlink()
    while done < len(run_seqs):  # 末个业务原件之后产生的解算版次
        run_allocation(memory)
        done+=1
    after=auction_report(memory)
    return {'ok':before==after,'source_count':len(before['sources']),
            'allocation_revisions':len(run_seqs)}
