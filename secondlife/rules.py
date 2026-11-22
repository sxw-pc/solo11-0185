from datetime import datetime

def instant(value): return datetime.fromisoformat(value.replace('Z','+00:00'))

def latest(events,kind,key):
    out={}
    for e in events:
        if e['kind']==kind: out[e[key]]=e
    return out

def d(result,reason,*ids): return {'result':result,'reason':reason,'evidence':[x for x in ids if x]}

def calculate(events):
    origins=latest(events,'origin','pack_id')
    tests=latest(events,'test','cell_id')
    strings=latest(events,'string','string_id')
    out={}; states=[]
    for pack_id,o in origins.items():
        chain=o.get('owner_chain',[])
        competing={e.get('claimed_owner') for e in events if e['kind']=='origin' and e['pack_id']==pack_id}
        duplicate=len(competing)>1 and not o.get('transfer_documented',False)
        ownership=bool(chain) and chain[-1]==o.get('claimed_owner') and not duplicate
        recalled=o.get('recalled',False)
        out['退役包来源裁定']=d('接收' if ownership else '隔离','权属链及序列号重复核验',o['id'])
        out['召回范围传播']=d('隔离' if recalled else '未召回','来源公告与组串引用交叉传播',o['id'])
        out['来源重复入库防双用']=d('争议' if duplicate else '唯一','同一序列号不能被两个回收批次双用',o['id'])
        out['退回交割责任']=d('退回' if not ownership else '无退回','无权属链的电池需归还提供者',o['id'])
        states.append(('来源与回收账',pack_id,'隔离' if recalled or not ownership else '接收',o.get('version',1)))
    for cell_id,t in tests.items():
        calibrated=t.get('calibrated',False) and instant(t['calibration_start'])<=instant(t['tested_at'])<instant(t['calibration_end'])
        health=t.get('capacity_ah',0)>=70 and t.get('resistance_mohm',999)<=10 and t.get('temp_c',999)<=45
        independent=t.get('operator')!=t.get('retest_operator')
        grade='合格' if calibrated and health and independent else '待核或报废'
        out['检测校准可信裁定']=d('有效' if calibrated else '无效','校准覆盖测量时刻才能采信',t['id'])
        out['电芯健康分档']=d(grade,'容量、内阻、温升联合判定',t['id'])
        out['复测争议裁定']=d('裁定' if independent else '争议','复测需由独立实验员完成',t['id'])
        out['检测批次抽样覆盖']=d('覆盖' if t.get('sample_count',0)>=2 else '不足','样本数量不足不得签认整批',t['id'])
        states.append(('检测与分选账',cell_id,grade,t.get('version',1)))
    for string_id,s in strings.items():
        cells=[tests.get(x) for x in s.get('cells',[])]
        packs=[origins.get(c.get('pack_id')) for c in cells if c]
        compatible=len(cells)>=2 and all(c and c.get('calibrated') and instant(c['calibration_start'])<=instant(c['tested_at'])<instant(c['calibration_end']) and c.get('capacity_ah',0)>=70 and c.get('resistance_mohm',999)<=10 and c.get('temp_c',999)<=45 and c.get('operator')!=c.get('retest_operator') for c in cells)
        compatible=compatible and max((c['capacity_ah'] for c in cells if c),default=0)-min((c['capacity_ah'] for c in cells if c),default=0)<=10
        origin_ok=bool(packs) and all(p and p.get('owner_chain') and p['owner_chain'][-1]==p.get('claimed_owner') and not p.get('recalled') and (len({e.get('claimed_owner') for e in events if e['kind']=='origin' and e['pack_id']==p['pack_id']})==1 or p.get('transfer_documented',False)) for p in packs)
        protection=s.get('protection_ok',False)
        alarm=s.get('alarm',False)
        deploy=compatible and origin_ok and protection and not alarm
        out['组串相容配置']=d('相容' if compatible else '待重组','电芯健康及容量离散度共同约束',s['id'],*(c['id'] for c in cells if c))
        out['投运条件确认']=d('投运' if deploy else '停运','来源、分档、保护器测试和告警共同决定',s['id'],*(p['id'] for p in packs if p))
        out['告警隔离追责']=d('隔离' if alarm else '正常','运行温升告警关联组串与来源批次',s['id'])
        out['退役拆组许可']=d('许可' if not alarm and not deploy else '禁止','告警期间或仍投运时不可拆组',s['id'])
        states.append(('组串与投运账',string_id,'投运' if deploy else '停运',s.get('version',1)))
    return out,states

RECORD_SPECS={'origin': ('来源与回收账', 'pack_id', [('来源证明', ['owner_valid', 'duplicate']), ('召回公告', ['recalled'])]), 'test': ('检测与分选账', 'cell_id', [('检测读数', ['capacity_ah', 'resistance_mohm', 'temp_c', 'sample_count']), ('校准复测', ['calibrated', 'retest_independent'])]), 'string': ('组串与投运账', 'string_id', [('组串配置', ['cells', 'protection_ok']), ('运行告警', ['alarm'])])}

def business_records(event):
    subsystem,key,specs=RECORD_SPECS[event['kind']]
    return [(subsystem,name,str(event[key]),{field:event.get(field) for field in fields}) for name,fields in specs]
