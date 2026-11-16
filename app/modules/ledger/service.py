import hashlib,io,json
from datetime import datetime,timezone,timedelta
from decimal import Decimal

from app.errors import RuleError
from app.modules.intake.reader import read
from app.modules.contract.service import normalize
from app import pipeline
from app.modules.ledger import store

CN=timezone(timedelta(hours=8))
Q2=Decimal('0.01')

# ---------- identities ----------
def fingerprint(row):
    """Stable idempotency key for one logical fragment."""
    base='|'.join(str(row[k]) for k in ('subscriber','session','sequence','start','end','zone'))
    return hashlib.sha256(base.encode()).hexdigest()[:20]

def text_digest(raw):return hashlib.sha256(raw).hexdigest()

def period_of_start(iso_start):
    """Billing period = UTC+8 calendar date of the session's earliest fragment start."""
    return datetime.fromisoformat(iso_start).astimezone(CN).strftime('%Y-%m-%d')

def next_period(period_id):
    d=datetime.strptime(period_id,'%Y-%m-%d').replace(tzinfo=CN)
    return (d+timedelta(days=1)).strftime('%Y-%m-%d')

# ---------- fragment <-> CSV ----------
COLUMNS=['subscriber','session','sequence','start','end','zone']
def fragments_to_csv(fragments):
    out=io.StringIO();out.write(','.join(COLUMNS)+'\n')
    for f in fragments:
        out.write(','.join(str(f[k]) for k in COLUMNS)+'\n')
    return ('﻿'+out.getvalue()).encode('utf-8')

def parse_supplement(raw):
    """Run intake + contract on the supplement alone. Returns normalized fragments + counters."""
    intake=read(raw)
    tariffs=json.loads((pipeline.SAMPLES/'tariffs.json').read_text(encoding='utf-8'))
    contract=normalize(intake['rows'],tariffs)
    fragments=[]
    for r in contract['rows']:
        fragments.append({'subscriber':r['subscriber'],'session':r['session'],'sequence':r['sequence'],
                          'start':r['start'],'end':r['end'],'zone':r['zone'],'first_line':r['line'],
                          'fingerprint':fingerprint(r)})
    issues=intake['issues']+contract['issues']
    return {'fragments':fragments,'issues':issues,
            'counts':{'input':intake['input'],'accepted':len(contract['rows']),
                      'quarantined':len(issues),'blank':intake['blank'],'duplicate':0}}

# ---------- watermark & recompute ----------
def watermark(period_id,fragment_rows,supplement_ids):
    # sort fingerprints so the digest depends on the fragment set, not arrival order
    digest=hashlib.sha256(''.join(sorted(f['fingerprint'] for f in fragment_rows)).encode()).hexdigest()
    return {'period_id':period_id,'fragment_count':len(fragment_rows),
            'fragment_digest':digest,'supplements':sorted(set(supplement_ids)),
            'cutoff':store.now()}

def recompute(period_id,fragment_rows,supplement_ids):
    """Deterministically re-run the full rule pipeline over the period's whole watermark."""
    raw=fragments_to_csv(fragment_rows)
    bundle=pipeline.execute(raw)
    calc=bundle['calculate']
    wm=watermark(period_id,fragment_rows,supplement_ids)
    summary={'total_fee':calc['total_fee'],'minutes':calc['minutes'],'sessions':calc['sessions'],
             'accepted_fragments':calc['accepted_fragments'],
             'quarantined':bundle['quarantine']['total'],
             'active_seconds':bundle['reconcile']['active_seconds'],
             'raw_seconds':bundle['reconcile']['raw_seconds']}
    return {'total_fee':calc['total_fee'],'minutes':calc['minutes'],'sessions':calc['sessions'],
            'watermark':wm,'summary':summary,'bundle':bundle}

# ---------- difference provenance ----------
def _reason(old,new):
    if old is None:return '新增会话补入'
    if new is None:return '原计费会话被整会话隔离'
    if old['billed_minutes']!=new['billed_minutes']:return '有效时长变化引起档位变动'
    return '分片并集重算差额'

def build_deltas(old_bill,new_bundle):
    """Session-level difference between the original bill and the recomputed result.
    A session present in the original bill but quarantined under the new watermark
    is kept as an explicit loss line instead of being silently dropped."""
    old_map={r['session']:r for r in (old_bill['bundle']['calculate']['rows'] if old_bill else [])}
    new_map={r['session']:r for r in new_bundle['calculate']['rows']}
    quarantine_sessions=set()
    for r in new_bundle['quarantine']['rows']:
        raw=r.get('raw')
        if isinstance(raw,dict) and 'session' in raw:quarantine_sessions.add(raw['session'])
    rows=[]
    for session in sorted(set(old_map)|set(new_map)):
        o=old_map.get(session);n=new_map.get(session)
        old_fee=None if o is None else o['fee']
        if n is None:
            # session vanished from the bill: record the loss and keep the audit trail explicit
            new_fee='0.00';new_seconds=o['active_seconds'] if o else '0';new_minutes=o['billed_minutes'] if o else 0
            source='原计费会话被整会话隔离' if session in quarantine_sessions else '原计费会话在新水位下消失'
        else:
            new_fee=n['fee'];new_seconds=n['active_seconds'];new_minutes=n['billed_minutes'];source=_reason(o,n)
        delta=(Decimal(new_fee)-Decimal(old_fee if old_fee is not None else '0')).quantize(Q2)
        rows.append({'session':session,'old_fee':old_fee,'new_fee':new_fee,'delta':str(delta),
                     'old_seconds':None if o is None else o['active_seconds'],'new_seconds':new_seconds,
                     'old_minutes':None if o is None else o['billed_minutes'],'new_minutes':new_minutes,
                     'source':source})
    return rows

def unsafe_reopen(old_bill,new_bundle):
    """A reopen must never silently erase an already-billed session or drop its amount."""
    old_sessions={r['session'] for r in old_bill['bundle']['calculate']['rows']}
    new_sessions={r['session'] for r in new_bundle['calculate']['rows']}
    lost=sorted(old_sessions-new_sessions)
    if lost:return '原账计费会话在新水位下被整会话隔离：'+','.join(lost)
    return None

def carry_amounts(period_id,deltas):
    """Only non-zero deltas are carried into the next period as adjustment lines."""
    target=next_period(period_id);lines=[]
    for d in deltas:
        if Decimal(d['delta'])==0:continue
        lines.append({'period_id':target,'from_period':period_id,'session':d['session'],
                      'amount':d['delta'],'source':'补片结转：'+d['source']})
    return target,lines

# ---------- period bootstrap ----------
def seed_period(period_id,raw):
    """Create the original (v1) bill for a period from its first full CDR intake."""
    if store.get_period(period_id):raise RuleError('PERIOD_EXISTS','账期已存在，初始账不能重复建立',409)
    parsed=parse_supplement(raw)
    store.ensure_period(period_id)
    store.add_fragments(period_id,parsed['fragments'])
    fragments=store.list_fragments(period_id)
    bill=recompute(period_id,fragments,[])
    deltas=build_deltas(None,bill['bundle'])
    store.write_reopen(period_id,1,bill,deltas,'open','seed')
    return store.get_bill(period_id,1)

# ---------- late-fragment intake ----------
def receive_supplement(raw,filename='supplement.csv',decision=None):
    raw=raw if isinstance(raw,bytes) else raw.encode('utf-8')
    digest=text_digest(raw)
    # Rule 1: same supplement byte-for-byte never accumulates twice.
    dup=store.supplement_exists(digest)
    if dup:
        return {'status':'DUPLICATE','digest':digest,'period_id':dup['period_id'],
                'message':'同一补片已接收，未重复累计','supplement':dup}
    parsed=parse_supplement(raw)
    if not parsed['fragments']:
        period_id='unknown'
        for issue in parsed['issues']:
            raw_row=issue.get('raw')
            if isinstance(raw_row,dict) and raw_row.get('start'):
                try:
                    period_id=period_of_start(raw_row['start']);break
                except (ValueError,TypeError):continue
        if period_id!='unknown':store.ensure_period(period_id)
        detail={'issues':parsed['issues'],'counts':parsed['counts']}
        saved=store.archive_supplement(period_id,digest,filename,raw,parsed['counts'],detail,
                                       state='REJECTED',reason='补片无有效分片，全部隔离',decision=None)
        return {'status':'REJECTED','digest':digest,'period_id':period_id,'supplement':saved,'detail':detail}
    period_id=period_of_start(parsed['fragments'][0]['start'])
    period=store.ensure_period(period_id)
    # Rule: fragment-level idempotency — the same logical fragment arriving again is not re-counted.
    inserted,skipped=store.add_fragments(period_id,parsed['fragments'])
    parsed['counts']['duplicate']=skipped
    if inserted==0:
        saved=store.archive_supplement(period_id,digest,filename,raw,parsed['counts'],
                                       {'note':'全部为已入账分片，水位未变化'},state='DUPLICATE',decision=None)
        return {'status':'DUPLICATE','digest':digest,'period_id':period_id,'message':'补片分片均已在水位内，未重复累计','supplement':saved}
    old_bill=store.get_bill(period_id)
    if old_bill is None:
        # First data for the period becomes the original v1 bill.
        fragments=store.list_fragments(period_id)
        bill=recompute(period_id,fragments,[digest])
        deltas=build_deltas(None,bill['bundle'])
        store.archive_supplement(period_id,digest,filename,raw,parsed['counts'],'')
        store.write_reopen(period_id,1,bill,deltas,'open',digest)
        return {'status':'APPLIED','decision':'reopen','period_id':period_id,'version':1,
                'deltas':deltas,'bill':store.get_bill(period_id,1)}
    # Recompute against the new watermark BEFORE deciding.
    prior_supps=set(old_bill['watermark']['supplements'])|{digest}
    fragments=store.list_fragments(period_id)
    bill=recompute(period_id,fragments,sorted(prior_supps))
    deltas=build_deltas(old_bill,bill['bundle'])
    chosen=decision or ('reopen' if period['state']=='open' else 'carry')
    unsafe=None
    if chosen=='reopen':
        unsafe=unsafe_reopen(old_bill,bill['bundle'])
        if unsafe:chosen='carry'  # Rule 3: failed/unsafe switch keeps the prior version, fall back to carry.
    # Archive the raw supplement first, in its own transaction, so no raw fragment is ever lost.
    store.archive_supplement(period_id,digest,filename,raw,parsed['counts'],
                             {'requested':decision,'effective':chosen,'unsafe_reopen':unsafe})
    if chosen=='reopen':
        version=old_bill['version']+1
        store.write_reopen(period_id,version,bill,deltas,'open',digest)
        return {'status':'APPLIED','decision':'reopen','period_id':period_id,'version':version,
                'deltas':deltas,'bill':store.get_bill(period_id,version)}
    # carry-forward: original bill version is preserved, delta moves to the next period.
    target,adjustments=carry_amounts(period_id,deltas)
    store.ensure_period(target)
    store.write_carry(period_id,old_bill['version'],adjustments,period['state'],digest)
    return {'status':'APPLIED','decision':'carry','period_id':period_id,'to_period':target,
            'version':old_bill['version'],'deltas':deltas,'adjustments':adjustments,
            'unsafe_reopen':unsafe,'bill':old_bill}

def issue(period_id):
    if not store.get_period(period_id):raise RuleError('NOT_FOUND','账期不存在',404)
    if not store.issue_period(period_id):raise RuleError('STATE','账期已出账或已关闭，不能重复出账',409)
    return store.get_period(period_id)

def close(period_id):
    period=store.get_period(period_id)
    if not period:raise RuleError('NOT_FOUND','账期不存在',404)
    with store.cursor() as con:
        con.execute("UPDATE periods SET state='closed' WHERE period_id=?",(period_id,))
    return store.get_period(period_id)
