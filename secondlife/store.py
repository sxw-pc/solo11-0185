import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from . import federation, rules
from .source_identity import assertion_digest, fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (sha TEXT PRIMARY KEY, kind TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (name TEXT PRIMARY KEY, result TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_state (subsystem TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(subsystem,entity_id));
CREATE TABLE IF NOT EXISTS decision_history (revision INTEGER NOT NULL, name TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(revision,name));
CREATE TABLE IF NOT EXISTS business_record (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_event TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id,version));
CREATE TABLE IF NOT EXISTS certificates (agency_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_signatures (sha TEXT PRIMARY KEY, issuer TEXT NOT NULL, signature TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assertions (digest TEXT PRIMARY KEY, issuer TEXT NOT NULL, local_id TEXT NOT NULL, entity_key TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assertion_sources (assertion_digest TEXT NOT NULL, sha TEXT NOT NULL, PRIMARY KEY(assertion_digest,sha));
"""

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

def _read_source(source):
    if isinstance(source,(str,Path)): return Path(source).read_bytes()
    return bytes(source)

def import_federated(db, kind, source, issuer, signature, registry, certificate=None):
    """导入机构签名的原字节文件。

    三层身份分离：raw SHA 标识文件字节；issuer:本地号 标识机构内事件；
    规范化摘要标识业务断言。验签只针对原字节，规范化仅用于断言比较。
    """
    raw=_read_source(source)
    if not registry.verify(issuer,raw,signature):
        raise ValueError('signature does not verify against raw file bytes')
    if certificate is not None:
        if registry.verify_certificate(certificate) is False:
            raise ValueError('agency certificate does not verify against federation root')
        if certificate.get('agency_id')!=issuer:
            raise ValueError('certificate agency does not match file issuer')
        key=registry.key_of(issuer)
        if key is not None and certificate.get('verification_key')!=key:
            raise ValueError('certificate verification key does not match trust registry')
    sha=fingerprint(raw)
    payload=json.loads(raw)
    if not isinstance(payload,list) or not payload:
        raise ValueError('source must contain a nonempty event list')
    if db.execute('SELECT 1 FROM source_files WHERE sha=?',(sha,)).fetchone():
        return {'sha':sha,'inserted':0,'idempotent':True}
    db.execute('SAVEPOINT federated_ingest')
    try:
        if certificate is not None:
            db.execute('INSERT OR IGNORE INTO certificates VALUES (?,?)',
                       (issuer,json.dumps(certificate,ensure_ascii=False,sort_keys=True)))
        seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        inserted=0
        for event in payload:
            if event.get('kind')!=kind or not event.get('id'):
                raise ValueError('event kind/id mismatch')
            if event.get('issuer')!=issuer:
                raise ValueError('event issuer does not match file signer')
            event_id=federation.scoped_event_id(issuer,event['id'])
            body=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT body FROM events WHERE event_id=?',(event_id,)).fetchone()
            if old:
                if old[0]!=body:
                    raise ValueError(f"issuer {issuer} reuses event id {event['id']} with different content")
                continue
            seq+=1
            inserted+=1
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',(event_id,kind,body,sha,seq))
        db.execute('INSERT INTO source_files(sha,kind,raw) VALUES (?,?,?)',(sha,kind,raw))
        db.execute('INSERT INTO file_signatures VALUES (?,?,?)',(sha,issuer,signature))
        digests=[]
        for event in payload:
            digest=assertion_digest(event)
            digests.append(digest)
            db.execute('INSERT OR IGNORE INTO assertions VALUES (?,?,?,?,?)',
                       (digest,issuer,event['id'],rules.entity_key(event),
                        json.dumps(event,ensure_ascii=False,sort_keys=True)))
            db.execute('INSERT OR IGNORE INTO assertion_sources VALUES (?,?)',(digest,sha))
        rebuild(db)
        db.execute('RELEASE SAVEPOINT federated_ingest')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT federated_ingest')
        db.execute('RELEASE SAVEPOINT federated_ingest')
        db.rollback()
        raise
    return {'sha':sha,'inserted':inserted,'assertions':digests,'idempotent':False}

def rebuild(db):
    rows=list(db.execute('SELECT event_id,body FROM events ORDER BY seq'))
    events=[json.loads(row[1]) for row in rows]
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
    for row,event in zip(rows,events):
        for subsystem,entity_type,entity_id,body in rules.business_records(event):
            db.execute('INSERT OR REPLACE INTO business_record VALUES (?,?,?,?,?,?)',(subsystem,entity_type,entity_id,row[0],event.get('version',1),json.dumps(body,ensure_ascii=False,sort_keys=True)))

def report(db):
    return {'decisions':{r['name']:{'result':r['result'],'reason':r['reason'],'evidence':json.loads(r['evidence'])} for r in db.execute('SELECT * FROM decisions ORDER BY name')},
            'entities':[dict(r) for r in db.execute('SELECT * FROM entity_state ORDER BY subsystem,entity_id')],
            'business_records':[dict(r) for r in db.execute('SELECT * FROM business_record ORDER BY subsystem,entity_type,entity_id,version')],
            'sources':[dict(r) for r in db.execute('SELECT sha,kind FROM source_files ORDER BY sha')]}

def provenance(db):
    """来源图：哪份原字节文件支持哪条逻辑断言。"""
    return {'sources':[dict(r) for r in db.execute('SELECT s.sha,s.kind,f.issuer,f.signature FROM source_files s JOIN file_signatures f ON f.sha=s.sha ORDER BY s.sha')],
            'assertions':[dict(r) for r in db.execute('SELECT digest,issuer,local_id,entity_key FROM assertions ORDER BY digest')],
            'edges':[dict(r) for r in db.execute('SELECT assertion_digest,sha FROM assertion_sources ORDER BY assertion_digest,sha')]}

def audit(db):
    before=report(db)
    before_provenance=provenance(db)
    source_rows=list(db.execute('SELECT sha,kind,raw FROM source_files ORDER BY rowid'))
    signatures={r['sha']:(r['issuer'],r['signature']) for r in db.execute('SELECT * FROM file_signatures')}
    certs=[json.loads(r[0]) for r in db.execute('SELECT body FROM certificates')]
    registry=federation.registry_from_certificates(certs)
    memory=connect(':memory:')
    for sha,kind,raw in source_rows:
        if sha in signatures:
            issuer,signature=signatures[sha]
            cert=next((c for c in certs if c['agency_id']==issuer),None)
            import_federated(memory,kind,raw,issuer=issuer,signature=signature,registry=registry,certificate=cert)
        else:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(raw); path=tmp.name
            try: import_file(memory,kind,path)
            finally: Path(path).unlink()
    after=report(memory)
    after_provenance=provenance(memory)
    return {'ok':before==after and before_provenance==after_provenance,
            'source_count':len(source_rows),'decision_count':len(before['decisions'])}
