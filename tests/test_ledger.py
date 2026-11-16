from decimal import Decimal
import pytest
from app.pipeline import sample_bytes
from app.modules.ledger import service,store

@pytest.fixture(autouse=True)
def isolated_db(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'ledger.sqlite3'))
    store.init()
    yield

CROSS='''﻿subscriber,session,sequence,start,end,zone
00000001,C00001,4,2026-11-02T09:02:20+08:00,2026-11-03T00:00:40+08:00,国内
'''
NEW_SESSION='''﻿subscriber,session,sequence,start,end,zone
00000015,C00015,1,2026-11-02T23:59:20+08:00,2026-11-03T00:00:30+08:00,漫游
'''
CONFLICT='''﻿subscriber,session,sequence,start,end,zone
00000001,C00001,2,2026-11-02T10:00:00+08:00,2026-11-02T10:00:40+08:00,国内
'''

def _setup():
    return service.seed_period('2026-11-02',sample_bytes('clean'))

def test_seed_original_bill_watermark():
    bill=_setup()
    assert bill['version']==1
    assert bill['watermark']['fragment_count']==42
    assert bill['summary']['sessions']==14
    assert bill['summary']['total_fee']=='16.00'
    # original bill and full bundle are retrievable for audit
    got=store.get_bill('2026-11-02')
    assert got['bundle']['calculate']['total_fee']=='16.00'

def test_open_period_supplement_reopens_original_bill():
    _setup()
    before=store.get_bill('2026-11-02')
    r=service.receive_supplement(CROSS)
    assert r['status']=='APPLIED' and r['decision']=='reopen'
    assert r['version']==2
    # previous version is preserved
    assert store.get_bill('2026-11-02',1)['summary']['total_fee']=='16.00'
    after=store.get_bill('2026-11-02',2)
    # C00001: 90 effective seconds -> 53990 (overlap removed), ceil 900 min, fee 90.30, delta 89.90
    assert after['watermark']['fragment_count']==43
    deltas={d['session']:d for d in store.list_deltas('2026-11-02',2)}
    assert deltas['C00001']['old_fee']=='0.40'
    assert deltas['C00001']['new_fee']=='90.30'
    assert Decimal(deltas['C00001']['new_fee'])-Decimal('0.40')==Decimal(deltas['C00001']['delta'])
    assert deltas['C00001']['delta']=='89.90'
    assert deltas['C00001']['source']=='有效时长变化引起档位变动'
    assert deltas['C00002']['delta']=='0.00'
    assert before['summary']['total_fee']=='16.00'

def test_duplicate_supplement_bytes_never_accumulates():
    _setup()
    first=service.receive_supplement(CROSS)
    assert first['status']=='APPLIED'
    again=service.receive_supplement(CROSS)
    assert again['status']=='DUPLICATE'
    bill=store.get_bill('2026-11-02')
    assert bill['version']==2  # no v3 created
    assert bill['watermark']['fragment_count']==43
    # raw copies are both retained (same digest -> one archive row)
    sups=store.list_supplements('2026-11-02')
    assert len(sups)==1 and sups[0]['state']=='APPLIED'
    assert 'subscriber,session' in store.get_supplement(sups[0]['digest'])['raw_text']

def test_same_fragment_in_different_envelope_deduped():
    _setup()
    service.receive_supplement(CROSS)
    # same logical fragment, extra whitespace/line -> different bytes, same fingerprint
    resend='﻿subscriber,session,sequence,start,end,zone\n00000001,C00001,4,2026-11-02T09:02:20+08:00,2026-11-03T00:00:40+08:00,国内\n\n'
    r=service.receive_supplement(resend)
    assert r['status']=='DUPLICATE'
    assert store.get_bill('2026-11-02')['watermark']['fragment_count']==43

def test_issued_period_carries_forward_keeps_original():
    _setup()
    service.issue('2026-11-02')
    r=service.receive_supplement(CROSS)
    assert r['status']=='APPLIED' and r['decision']=='carry'
    assert r['to_period']=='2026-11-03'
    # original bill version and amount untouched
    bill=store.get_bill('2026-11-02')
    assert bill['version']==1 and bill['summary']['total_fee']=='16.00'
    # adjustment lines landed on next period, only non-zero deltas
    adj=store.list_adjustments('2026-11-03')
    assert len(adj)==1 and adj[0]['session']=='C00001'
    assert Decimal(adj[0]['amount'])==Decimal('89.90')
    assert '补片结转' in adj[0]['source']
    # next period exists and can be audited independently
    detail_periods={p['period_id']:p for p in store.list_periods()}
    assert '2026-11-03' in detail_periods

def test_forced_reopen_decision_on_open_period():
    _setup()
    r=service.receive_supplement(NEW_SESSION,decision='reopen')
    assert r['decision']=='reopen' and r['version']==2
    # new cross-midnight session stays in the period of its start
    deltas={d['session']:d for d in store.list_deltas('2026-11-02',2)}
    assert deltas['C00015']['old_fee'] is None
    assert deltas['C00015']['source']=='新增会话补入'

def test_forced_carry_even_when_open():
    _setup()
    r=service.receive_supplement(CROSS,decision='carry')
    assert r['decision']=='carry'
    assert store.get_bill('2026-11-02')['version']==1
    assert store.list_adjustments('2026-11-03')[0]['session']=='C00001'

def test_unsafe_reopen_falls_back_to_carry_and_keeps_version():
    _setup()
    # conflicting same-sequence fragment isolates whole C00001: reopen would erase a billed session
    r=service.receive_supplement(CONFLICT,decision='reopen')
    assert r['decision']=='carry'
    assert r['unsafe_reopen'] and 'C00001' in r['unsafe_reopen']
    bill=store.get_bill('2026-11-02')
    assert bill['version']==1 and bill['summary']['total_fee']=='16.00'
    # the rejected-for-reopen delta (new fee 0 for the lost session) is carried, not applied
    adj=store.list_adjustments('2026-11-03')
    assert any(a['session']=='C00001' and Decimal(a['amount'])==Decimal('-0.40') for a in adj)
    # raw supplement and its fragments are still archived, nothing lost
    sups=store.list_supplements('2026-11-02')
    assert sups[-1]['state']=='APPLIED' and sups[-1]['decision']=='carry'
    assert len(store.list_fragments('2026-11-02'))==43

def test_invalid_supplement_rejected_but_archived():
    _setup()
    bad='''subscriber,session,sequence,start,end,zone
00000001,C00099,1,2026-11-02T09:00:40+08:00,2026-11-02T09:00:00+08:00,国内
'''
    r=service.receive_supplement(bad)
    assert r['status']=='REJECTED'
    sups=store.list_supplements('2026-11-02')
    assert sups[-1]['state']=='REJECTED'
    assert store.get_supplement(sups[-1]['digest'])['raw_text']  # original retained
    assert store.get_bill('2026-11-02')['version']==1

def test_switch_failure_rolls_back_previous_version(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'switch.sqlite3'))
    store.init()
    service.seed_period('2026-11-02',sample_bytes('clean'))
    import app.modules.ledger.store as ls
    real_switch=ls.switch_connection
    triggered={'done':False}
    class Boom:
        def __init__(self,con):self._con=con
        def execute(self,sql,params=()):
            if not triggered['done'] and str(sql).lstrip().startswith('INSERT INTO bills'):
                triggered['done']=True;raise RuntimeError('disk full')
            return self._con.execute(sql,params)
        def __getattr__(self,name):return getattr(self._con,name)
    monkeypatch.setattr(ls,'switch_connection',lambda: Boom(real_switch()))
    with pytest.raises(RuntimeError):
        service.receive_supplement(CROSS)
    assert triggered['done']
    # NOTE: do not call monkeypatch.undo() here — it would also unset DB_PATH before
    # the assertions below. Fixture teardown restores everything after the test.
    # prior version remains the current and only bill; supplement archived, no half-written bill
    bill=store.get_bill('2026-11-02')
    assert bill['version']==1 and bill['summary']['total_fee']=='16.00'
    assert len(store.list_bill_versions('2026-11-02'))==1
    assert store.list_supplements('2026-11-02')[0]['state']=='RECEIVED'
    # the fragment watermark was committed before the failed switch and is not lost
    assert len(store.list_fragments('2026-11-02'))==43

def test_watermark_digest_is_order_stable_and_versions_accumulate():
    _setup()
    r1=service.receive_supplement(CROSS)
    r2=service.receive_supplement(NEW_SESSION)
    assert (r1['version'],r2['version'])==(2,3)
    v3=store.get_bill('2026-11-02')
    assert v3['watermark']['fragment_count']==44
    assert len(v3['watermark']['supplements'])==2
    # digest depends on the fragment set only: recompute from an independently sorted set
    import hashlib
    expect=hashlib.sha256(''.join(sorted(f['fingerprint'] for f in store.list_fragments('2026-11-02'))).encode()).hexdigest()
    assert v3['watermark']['fragment_digest']==expect
    # v3 deltas are against v2, so the already-adjusted session shows no further movement
    d3={d['session']:d for d in store.list_deltas('2026-11-02',3)}
    assert d3['C00015']['source']=='新增会话补入' and d3['C00015']['delta']=='3.00'
    assert d3['C00001']['delta']=='0.00'

def test_issue_idempotent_and_closed_guard():
    _setup()
    service.issue('2026-11-02')
    import pytest
    from app.errors import RuleError
    with pytest.raises(RuleError):service.issue('2026-11-02')
    service.close('2026-11-02')
    r=service.receive_supplement(CROSS)
    # closed period: no reopen; carry path still preserves the bill
    assert r['decision']=='carry'
    assert store.get_bill('2026-11-02')['version']==1
