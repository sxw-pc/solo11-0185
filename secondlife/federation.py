import hashlib
import hmac
import json
from pathlib import Path

from . import rules
from .source_identity import assertion_digest, canonical_bytes, fingerprint

ALGORITHM='HMAC-SHA256'

def distinct_sources(blobs):
    """来源去重只按原字节哈希：重排版的同一断言仍是多份来源。"""
    return list(dict.fromkeys(fingerprint(blob) for blob in blobs))

def _mac(key,data):
    if isinstance(key,str): key=key.encode('utf-8')
    return hmac.new(key,data,hashlib.sha256).hexdigest()

def scoped_event_id(issuer,local_id):
    """机构内事件身份：事件号只在签发机构作用域内唯一，跨机构同号互不覆盖。"""
    return f'{issuer}:{local_id}'

class Registry:
    """离线信任注册表：发行机构证书、按机构验签。

    原型以 HMAC 对称密钥代替 PKI（验签密钥即可签名），部署时应替换为
    非对称签名，验证方只持有公钥与根证书。
    """
    def __init__(self,root_key=None):
        self.root_key=root_key
        self._keys={}
    def register_agency(self,agency_id,key=None):
        key=key or hashlib.sha256(f'{self.root_key}:{agency_id}'.encode('utf-8')).hexdigest()
        self._keys[agency_id]=key
        return key
    def load_key(self,agency_id,key): self._keys[agency_id]=key
    def key_of(self,agency_id): return self._keys.get(agency_id)
    def certificate(self,agency_id):
        """发行机构证书：联合会根密钥签发，绑定机构与其验签密钥。"""
        body={'agency_id':agency_id,'key_id':f'{agency_id}-key-1','algorithm':ALGORITHM,
              'verification_key':self._keys[agency_id]}
        return dict(body,cert_signature=_mac(self.root_key,canonical_bytes(body)))
    def verify_certificate(self,cert):
        if self.root_key is None: return None
        body={k:v for k,v in cert.items() if k!='cert_signature'}
        return hmac.compare_digest(_mac(self.root_key,canonical_bytes(body)),cert.get('cert_signature',''))
    def sign(self,agency_id,raw):
        """对文件原字节签名，绝不签规范化字节。"""
        return _mac(self._keys[agency_id],raw)
    def verify(self,agency_id,raw,signature):
        key=self._keys.get(agency_id)
        return key is not None and hmac.compare_digest(_mac(key,raw),signature)

def registry_from_certificates(certs):
    """由证书记录重建验签视图（审计重放用；无根密钥，不再核验证书链）。"""
    reg=Registry()
    for cert in certs: reg.load_key(cert['agency_id'],cert['verification_key'])
    return reg

def load_source_bundle(path):
    """读取联盟来源包：信任注册表、机构证书、各机构签名的原文件。"""
    bundle=json.loads(Path(path).read_text(encoding='utf-8'))
    registry=Registry(bundle['trust_registry'].get('root_key'))
    for agency_id,key in bundle['trust_registry']['keys'].items(): registry.load_key(agency_id,key)
    files=[{'name':f['name'],'kind':f.get('kind','origin'),'issuer':f['issuer'],
            'raw':f['content'].encode('utf-8'),'signature':f['signature']} for f in bundle['files']]
    return registry,bundle['certificates'],files

def build_graph(files,registry):
    """来源图：先对原字节验签，再分层建立文件/机构内事件/业务断言身份与多对多边。"""
    sources,assertions,edges=[],{},{}
    for f in files:
        if not registry.verify(f['issuer'],f['raw'],f['signature']):
            raise ValueError(f"signature of {f['name']} does not verify against its raw bytes")
        sha=fingerprint(f['raw'])
        digests=[]
        for event in json.loads(f['raw']):
            digest=assertion_digest(event)
            assertions.setdefault(digest,{'digest':digest,'issuer':event['issuer'],'local_id':event['id'],
                                          'event_key':scoped_event_id(event['issuer'],event['id']),
                                          'entity_key':rules.entity_key(event),'body':event})
            edges.setdefault((sha,digest),{'sha':sha,'assertion_digest':digest})
            digests.append(digest)
        sources.append({'name':f['name'],'sha':sha,'issuer':f['issuer'],
                        'signature':f['signature'],'raw':f['raw'],'assertions':digests})
    return {'sources':sources,'assertions':list(assertions.values()),'edges':list(edges.values())}

def find_tampering(graph):
    """同一机构同一事件号出现不同断言才是该机构内篡改；跨机构同号不算。"""
    seen={}
    for a in graph['assertions']: seen.setdefault((a['issuer'],a['local_id']),set()).add(a['digest'])
    return [{'issuer':issuer,'local_id':local_id,'digests':sorted(digests)}
            for (issuer,local_id),digests in sorted(seen.items()) if len(digests)>1]

def _assertion_view(a,with_body=False):
    view={'digest':a['digest'],'issuer':a['issuer'],'local_id':a['local_id'],
          'event_key':a['event_key'],'entity_key':a['entity_key']}
    if with_body: view['body']=a['body']
    return view

def build_archive_package(graph,certificates):
    """原签名归档协议：三份原字节文件随包公开、各自签名可原样验签，附断言映射。"""
    return {'protocol':'original-signature-archive',
            'certificates':list(certificates),
            'files':[{'name':s['name'],'sha':s['sha'],'issuer':s['issuer'],
                      'signature':s['signature'],'content':s['raw'].decode('utf-8')} for s in graph['sources']],
            'assertions':[_assertion_view(a) for a in graph['assertions']],
            'edges':[dict(e) for e in graph['edges']]}

def verify_archive_package(pkg,registry):
    """按原字节逐文件验签，并重算断言摘要与来源边比对声明的映射。"""
    if pkg.get('protocol')!='original-signature-archive':
        raise ValueError('not an original-signature-archive package')
    files_ok,digests,recomputed=True,set(),set()
    for f in pkg['files']:
        raw=f['content'].encode('utf-8')
        if fingerprint(raw)!=f['sha'] or not registry.verify(f['issuer'],raw,f['signature']):
            files_ok=False
            continue
        for event in json.loads(raw):
            digest=assertion_digest(event)
            digests.add(digest)
            recomputed.add((f['sha'],digest))
    declared={a['digest'] for a in pkg['assertions']}
    edges={(e['sha'],e['assertion_digest']) for e in pkg['edges']}
    return {'ok':files_ok and digests==declared and recomputed==edges,
            'files_verified':len(pkg['files']),'assertions':len(declared)}

def build_joint_package(graph,certificates,registry,station):
    """联合规范证书协议：公开两条规范断言与甲乙及回收站的联合签名，
    三份原始文件只以哈希引用，供受托审计调阅。"""
    assertions=sorted((_assertion_view(a,with_body=True) for a in graph['assertions']),key=lambda a:a['digest'])
    source_refs=sorted(s['sha'] for s in graph['sources'])
    payload=canonical_bytes({'protocol':'joint-canonical-certificate',
                             'assertions':assertions,'source_refs':source_refs})
    signers=sorted({s['issuer'] for s in graph['sources']}|{station})
    joint=[{'signer':s,'signature':registry.sign(s,payload)} for s in signers]
    return {'protocol':'joint-canonical-certificate',
            'certificates':list(certificates),
            'assertions':assertions,
            'source_refs':source_refs,
            'joint_signature':{'subject':'canonical-assertion-certificate','signatures':joint}}

def verify_joint_package(pkg,registry):
    """验真依赖新的三方签发责任：重算规范证书载荷并核验联合签名。"""
    if pkg.get('protocol')!='joint-canonical-certificate':
        raise ValueError('not a joint-canonical-certificate package')
    if any('content' in f for f in pkg.get('files',[])):
        return {'ok':False,'reason':'joint package must reference originals by hash only'}
    payload=canonical_bytes({'protocol':'joint-canonical-certificate',
                             'assertions':pkg['assertions'],'source_refs':pkg['source_refs']})
    signatures=pkg['joint_signature']['signatures']
    signers_ok=all(registry.verify(s['signer'],payload,s['signature']) for s in signatures)
    digests_ok=all(a['digest']==assertion_digest(a['body']) for a in pkg['assertions'])
    return {'ok':bool(signatures) and signers_ok and digests_ok,
            'signers':[s['signer'] for s in signatures]}
