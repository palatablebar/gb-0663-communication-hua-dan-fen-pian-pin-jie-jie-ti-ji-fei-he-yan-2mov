import csv,io
from decimal import Decimal
from fastapi import APIRouter,Body,Request
from fastapi.responses import Response
from app.web import page
from app.errors import RuleError
from app import pipeline
from app.modules.ledger import service,store

router=APIRouter()

@router.get('/ledger')
def view(request:Request):
    periods=store.list_periods()
    return page(request,'ledger',extra={'periods':periods,'supplements':store.list_supplements()})

@router.post('/api/ledger/seed')
def seed(payload:dict=Body(default={})):
    sample=payload.get('sample','clean')
    period_id=payload.get('period_id') or '2026-11-02'
    raw=pipeline.sample_bytes(sample) if sample in ('dirty','clean','gb18030') else None
    if raw is None:raise RuleError('SAMPLE','话单样例不存在',404)
    bill=service.seed_period(period_id,raw)
    return {'status':'SEEDED','period_id':period_id,'version':bill['version'],'watermark':bill['watermark'],'summary':bill['summary']}

@router.post('/api/ledger/supplements')
def receive(payload:dict=Body(...)):
    text=payload.get('csv_text')
    if not isinstance(text,str) or not text.strip():raise RuleError('VALUE','补片CSV不能为空')
    decision=payload.get('decision')
    if decision not in (None,'reopen','carry'):raise RuleError('VALUE','决策只能是 reopen 或 carry')
    return service.receive_supplement(text,payload.get('filename','supplement.csv'),decision)

@router.post('/api/ledger/issue')
def issue(payload:dict=Body(...)):
    return service.issue(payload.get('period_id',''))

@router.post('/api/ledger/close')
def close(payload:dict=Body(...)):
    return service.close(payload.get('period_id',''))

@router.get('/api/ledger/demo-supplement')
def demo_supplement():
    raw=(pipeline.SAMPLES/'supplement_cross_midnight.csv').read_bytes()
    return Response('﻿'+raw.decode('utf-8-sig'),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="supplement_cross_midnight.csv"'})

@router.get('/api/ledger/periods')
def periods():return store.list_periods()

@router.get('/api/ledger/periods/{period_id}')
def period_detail(period_id:str,version=None):
    period=store.get_period(period_id)
    if not period:raise RuleError('NOT_FOUND','账期不存在',404)
    bill=store.get_bill(period_id,version)
    versions=store.list_bill_versions(period_id)
    deltas=store.list_deltas(period_id,bill['version']) if bill else []
    adjustments=store.list_adjustments(period_id)
    carry_in_total=str(sum((Decimal(a['amount']) for a in adjustments),Decimal(0)).quantize(Decimal('0.01')))
    return {'period':period,'versions':versions,'current_version':bill['version'] if bill else None,
            'bill':bill,'deltas':deltas,'adjustments_in':adjustments,'carry_in_total':carry_in_total,
            'fragments':store.list_fragments(period_id),'supplements':store.list_supplements(period_id)}

@router.get('/api/ledger/supplements/{digest}/raw')
def supplement_raw(digest:str):
    sup=store.get_supplement(digest)
    if not sup:raise RuleError('NOT_FOUND','补片记录不存在',404)
    return Response('﻿'+sup['raw_text'],media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="supplement-{digest[:8]}.csv"'})

def _csv(headers,rows):
    out=io.StringIO();w=csv.writer(out);w.writerow(headers)
    for r in rows:w.writerow(r)
    return Response('﻿'+out.getvalue(),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="ledger-export.csv"'})

@router.get('/api/ledger/export/periods')
def export_periods():
    rows=[]
    for p in store.list_periods():
        b=p['latest_bill']
        rows.append([p['period_id'],p['state'],b['version'] if b else '',b['summary']['total_fee'] if b else '0.00',
                     b['summary']['minutes'] if b else 0,b['summary']['sessions'] if b else 0,
                     b['watermark']['fragment_count'] if b else 0,b['watermark']['fragment_digest'] if b else '',
                     p['adjustment_count'],p['adjustment_total'],p['created_at'],p['issued_at'] or ''])
    return _csv(['period_id','state','bill_version','total_fee','minutes','sessions','watermark_fragments','watermark_digest','adjustment_lines','adjustment_total','created_at','issued_at'],rows)

@router.get('/api/ledger/export/deltas/{period_id}')
def export_deltas(period_id:str):
    rows=[]
    for b in store.list_bill_versions(period_id):
        for d in store.list_deltas(period_id,b['version']):
            rows.append([period_id,b['version'],d['session'],d['old_fee'] or '',d['new_fee'],d['delta'],
                         d['old_seconds'] or '',d['new_seconds'],d['old_minutes'] or '',d['new_minutes'],d['source']])
    return _csv(['period_id','bill_version','session','old_fee','new_fee','delta','old_active_seconds','new_active_seconds','old_minutes','new_minutes','difference_source'],rows)

@router.get('/api/ledger/export/supplements')
def export_supplements():
    rows=[]
    for s in store.list_supplements():
        rows.append([s['digest'],s['period_id'],s['received_at'],s['filename'],s['input_rows'],s['accepted_rows'],
                     s['duplicate_rows'],s['quarantined_rows'],s['state'],s['decision'] or '',s['bill_version'] or '',s['reason'] or ''])
    return _csv(['digest','period_id','received_at','filename','input_rows','accepted_rows','duplicate_rows','quarantined_rows','state','decision','bill_version','reason'],rows)
