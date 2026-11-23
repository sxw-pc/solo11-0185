import json
from pathlib import Path

import pytest

from secondlife import federation
from secondlife.federation import (build_archive_package, build_graph, build_joint_package,
                                   find_tampering, load_source_bundle, verify_archive_package,
                                   verify_joint_package)
from secondlife.source_identity import assertion_digest, canonical_bytes, fingerprint
from secondlife.store import audit, connect, import_file, import_federated, provenance, report

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'fixtures' / 'federated-sources.json'


@pytest.fixture()
def bundle():
    registry, certificates, files = load_source_bundle(BUNDLE)
    return {'registry': registry, 'certificates': certificates, 'files': files}


def by_name(files, name):
    return next(f for f in files if f['name'] == name)


def cert_for(certificates, issuer):
    return next(c for c in certificates if c['agency_id'] == issuer)


def import_all(db, bundle, order=('F1', 'F2', 'F3')):
    results = {}
    for name in order:
        f = by_name(bundle['files'], name)
        results[name] = import_federated(
            db, f['kind'], f['raw'], issuer=f['issuer'], signature=f['signature'],
            registry=bundle['registry'], certificate=cert_for(bundle['certificates'], f['issuer']))
    return results


def digests_of(bundle):
    return {name: assertion_digest(json.loads(by_name(bundle['files'], name)['raw'])[0])
            for name in ('F1', 'F2', 'F3')}


def test_three_raw_files_two_issuer_scoped_assertions(bundle):
    files = bundle['files']
    assert len(federation.distinct_sources([f['raw'] for f in files])) == 3
    graph = build_graph(files, bundle['registry'])
    assert len(graph['sources']) == 3
    assert len(graph['assertions']) == 2
    d = digests_of(bundle)
    assert d['F1'] == d['F2'] != d['F3']
    edges = {(e['sha'], e['assertion_digest']) for e in graph['edges']}
    assert edges == {(fingerprint(by_name(files, 'F1')['raw']), d['F1']),
                     (fingerprint(by_name(files, 'F2')['raw']), d['F2']),
                     (fingerprint(by_name(files, 'F3')['raw']), d['F3'])}
    assert {a['entity_key'] for a in graph['assertions']} == {'origin:P1', 'origin:P2'}
    assert {a['event_key'] for a in graph['assertions']} == {'甲:O1', '乙:O1'}
    assert find_tampering(graph) == []


def test_signatures_bind_raw_bytes_never_canonical(bundle):
    files, reg = bundle['files'], bundle['registry']
    f1, f2, f3 = (by_name(files, n) for n in ('F1', 'F2', 'F3'))
    assert reg.verify('甲', f1['raw'], f1['signature'])
    assert reg.verify('甲', f2['raw'], f2['signature'])
    assert reg.verify('乙', f3['raw'], f3['signature'])
    assert f1['signature'] != f2['signature']
    assert not reg.verify('甲', f2['raw'], f1['signature'])
    assert not reg.verify('甲', canonical_bytes(json.loads(f1['raw'])), f1['signature'])
    assert not reg.verify('甲', canonical_bytes(json.loads(f2['raw'])), f2['signature'])
    assert not reg.verify('乙', f1['raw'], f1['signature'])
    with pytest.raises(ValueError):
        build_graph([dict(f1, signature=f2['signature'])], reg)


def test_legacy_store_global_id_conflict_path(bundle, tmp_path):
    db = connect(':memory:')
    paths = {}
    for name in ('F1', 'F2', 'F3'):
        p = tmp_path / f'{name}.json'
        p.write_bytes(by_name(bundle['files'], name)['raw'])
        paths[name] = p
    import_file(db, 'origin', paths['F1'])
    assert import_file(db, 'origin', paths['F2'])['idempotent'] is False
    assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] == 2
    before = report(db)
    with pytest.raises(ValueError):
        import_file(db, 'origin', paths['F3'])
    assert report(db) == before
    assert db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] == 2
    assert {r['entity_id'] for r in report(db)['business_records']} == {'P1'}
    db.close()


def test_federated_import_layers_and_provenance(bundle):
    db = connect(':memory:')
    import_all(db, bundle)
    assert db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] == 3
    assert {r[0] for r in db.execute('SELECT event_id FROM events')} == {'甲:O1', '乙:O1'}
    prov = provenance(db)
    assert len(prov['sources']) == 3
    assert len(prov['assertions']) == 2
    d = digests_of(bundle)
    edges = {(e['sha'], e['assertion_digest']) for e in prov['edges']}
    assert edges == {(fingerprint(by_name(bundle['files'], 'F1')['raw']), d['F1']),
                     (fingerprint(by_name(bundle['files'], 'F2')['raw']), d['F2']),
                     (fingerprint(by_name(bundle['files'], 'F3')['raw']), d['F3'])}
    packs = {}
    for r in report(db)['business_records']:
        packs.setdefault(r['entity_id'], set()).add(r['source_event'])
    assert packs == {'P1': {'甲:O1'}, 'P2': {'乙:O1'}}
    db.close()


def test_import_order_and_duplicates_do_not_change_assertions(bundle):
    baseline = None
    for order in [('F1', 'F2', 'F3'), ('F3', 'F2', 'F1'), ('F2', 'F1', 'F3'),
                  ('F1', 'F1', 'F2', 'F3'), ('F3', 'F1', 'F2', 'F2')]:
        db = connect(':memory:')
        import_all(db, bundle, order)
        prov = provenance(db)
        summary = ({a['digest'] for a in prov['assertions']},
                   len(prov['sources']),
                   {(e['assertion_digest'], e['sha']) for e in prov['edges']},
                   {(r['entity_id'], r['source_event']) for r in report(db)['business_records']})
        assert len(summary[0]) == 2
        assert summary[1] == 3
        baseline = baseline or summary
        assert summary == baseline
        db.close()
    db = connect(':memory:')
    import_all(db, bundle)
    f1 = by_name(bundle['files'], 'F1')
    again = import_federated(db, 'origin', f1['raw'], issuer='甲', signature=f1['signature'],
                             registry=bundle['registry'],
                             certificate=cert_for(bundle['certificates'], '甲'))
    assert again['idempotent'] is True
    assert len(provenance(db)['assertions']) == 2
    db.close()


def test_same_issuer_tamper_rejected_cross_issuer_accepted(bundle):
    reg = bundle['registry']
    db = connect(':memory:')
    import_all(db, bundle, ('F1',))
    before = report(db)
    forged = json.dumps([{'id': 'O1', 'kind': 'origin', 'issuer': '甲', 'pack_id': 'P9',
                          'owner_chain': ['factory', 'fleet', 'recycler'],
                          'claimed_owner': 'recycler', 'transfer_documented': False,
                          'recalled': False, 'version': 1}], ensure_ascii=False).encode('utf-8')
    forged_sig = reg.sign('甲', forged)
    with pytest.raises(ValueError):
        import_federated(db, 'origin', forged, issuer='甲', signature=forged_sig,
                         registry=reg, certificate=cert_for(bundle['certificates'], '甲'))
    assert report(db) == before
    assert len(provenance(db)['assertions']) == 1
    import_all(db, bundle, ('F3',))
    assert {r[0] for r in db.execute('SELECT event_id FROM events')} == {'甲:O1', '乙:O1'}
    db.close()


def test_forged_signature_rejected(bundle):
    db = connect(':memory:')
    f1 = by_name(bundle['files'], 'F1')
    with pytest.raises(ValueError):
        import_federated(db, 'origin', f1['raw'], issuer='甲',
                         signature=by_name(bundle['files'], 'F3')['signature'],
                         registry=bundle['registry'],
                         certificate=cert_for(bundle['certificates'], '甲'))
    assert db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] == 0
    db.close()


def test_provenance_change_drives_rebuild_and_keeps_history(bundle):
    db = connect(':memory:')
    import_all(db, bundle, ('F1',))
    first = report(db)['decisions']
    assert {r[0] for r in db.execute('SELECT revision FROM decision_history')} == {2}
    import_all(db, bundle, ('F2', 'F3'))
    assert {r[0] for r in db.execute('SELECT revision FROM decision_history')} == {2, 3, 4}
    old = {r['name']: r['result'] for r in db.execute(
        'SELECT name,result FROM decision_history WHERE revision=2')}
    assert old == {name: d['result'] for name, d in first.items()}
    entities = {(r['subsystem'], r['entity_id']) for r in report(db)['entities']}
    assert ('来源与回收账', 'P1') in entities
    assert ('来源与回收账', 'P2') in entities
    db.close()


def test_replay_yields_independent_p1_p2_records(bundle):
    db = connect(':memory:')
    import_all(db, bundle)
    result = audit(db)
    assert result['ok'] and result['source_count'] == 3
    packs = {}
    for r in report(db)['business_records']:
        packs.setdefault(r['entity_id'], set()).add(r['source_event'])
    assert packs == {'P1': {'甲:O1'}, 'P2': {'乙:O1'}}
    db.close()


def test_archive_package_three_verifiable_files_two_mappings(bundle):
    graph = build_graph(bundle['files'], bundle['registry'])
    pkg = build_archive_package(graph, bundle['certificates'])
    assert pkg['protocol'] == 'original-signature-archive'
    assert 'joint_signature' not in pkg
    assert len(pkg['files']) == 3
    assert len(pkg['assertions']) == 2
    result = verify_archive_package(pkg, bundle['registry'])
    assert result['ok'] and result['files_verified'] == 3
    jia = [f for f in pkg['files'] if f['issuer'] == '甲']
    assert len(jia) == 2 and len({f['sha'] for f in jia}) == 2
    d = digests_of(bundle)
    assert {a['digest'] for a in pkg['assertions']} == {d['F1'], d['F3']}
    edge_map = {}
    for e in pkg['edges']:
        edge_map.setdefault(e['assertion_digest'], set()).add(e['sha'])
    assert edge_map[d['F1']] == {fingerprint(by_name(bundle['files'], 'F1')['raw']),
                                 fingerprint(by_name(bundle['files'], 'F2')['raw'])}
    assert edge_map[d['F3']] == {fingerprint(by_name(bundle['files'], 'F3')['raw'])}


def test_joint_package_canonical_assertions_and_three_party_signature(bundle):
    graph = build_graph(bundle['files'], bundle['registry'])
    archive = build_archive_package(graph, bundle['certificates'])
    pkg = build_joint_package(graph, bundle['certificates'], bundle['registry'], station='回收站')
    assert pkg['protocol'] == 'joint-canonical-certificate'
    assert 'files' not in pkg
    assert len(pkg['assertions']) == 2
    assert all('body' in a for a in pkg['assertions'])
    assert len(pkg['source_refs']) == 3
    assert set(pkg['source_refs']) == {s['sha'] for s in graph['sources']}
    assert {s['signer'] for s in pkg['joint_signature']['signatures']} == {'甲', '乙', '回收站'}
    assert verify_joint_package(pkg, bundle['registry'])['ok']
    assert len(json.dumps(pkg, ensure_ascii=False)) < len(json.dumps(archive, ensure_ascii=False))
    forged = json.loads(json.dumps(pkg))
    forged['assertions'][0]['body']['pack_id'] = 'P9'
    assert not verify_joint_package(forged, bundle['registry'])['ok']


def test_two_protocols_have_distinct_trust_shapes(bundle):
    graph = build_graph(bundle['files'], bundle['registry'])
    archive = build_archive_package(graph, bundle['certificates'])
    joint = build_joint_package(graph, bundle['certificates'], bundle['registry'], station='回收站')
    assert [f['issuer'] for f in archive['files']] == ['甲', '甲', '乙']
    assert {s['signer'] for s in joint['joint_signature']['signatures']} == {'甲', '乙', '回收站'}
    assert all('content' in f for f in archive['files'])
    assert 'files' not in joint and len(joint['source_refs']) == 3
    assert len(archive['assertions']) == len(joint['assertions']) == 2
    assert {a['digest'] for a in archive['assertions']} == {a['digest'] for a in joint['assertions']}
    with pytest.raises(ValueError):
        verify_joint_package(archive, bundle['registry'])
    with pytest.raises(ValueError):
        verify_archive_package(joint, bundle['registry'])
