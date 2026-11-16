from fastapi.testclient import TestClient

CROSS=open('/workspace/sample_data/supplement_cross_midnight.csv',encoding='utf-8-sig').read()

def test_ledger_page_renders(client):
    r=client.get('/ledger');assert r.status_code==200
    assert '补片台账' in r.text and 'watermark' in r.text
    # auto-seeded original period from the lifespan
    assert '2026-11-02' in r.text

def test_calculate_page_shows_watermark_strip(client):
    r=client.get('/calculate');assert r.status_code==200
    assert 'ledger --watermark' in r.text and '2026-11-02' in r.text

def test_full_supplement_lifecycle_over_http(client):
    # 1. open period -> reopen creates v2
    r=client.post('/api/ledger/supplements',json={'csv_text':CROSS})
    assert r.status_code==200;j=r.json()
    assert j['decision']=='reopen' and j['version']==2
    # 2. identical supplement -> duplicate, no accumulation
    dup=client.post('/api/ledger/supplements',json={'csv_text':CROSS}).json()
    assert dup['status']=='DUPLICATE'
    # 3. period detail exposes watermark, original bill, recompute, deltas
    d=client.get('/api/ledger/periods/2026-11-02').json()
    assert d['current_version']==2
    assert d['bill']['watermark']['fragment_count']==43
    assert len(d['versions'])==2  # original v1 retained + recomputed v2
    sessions={x['session']:x for x in d['deltas']}
    assert sessions['C00001']['delta']=='89.90'
    # original bill version still retrievable
    v1=client.get('/api/ledger/periods/2026-11-02?version=1').json()
    assert v1['bill']['summary']['total_fee']=='16.00'

def test_issue_then_carry_over_http(client):
    assert client.post('/api/ledger/issue',json={'period_id':'2026-11-02'}).status_code==200
    r=client.post('/api/ledger/supplements',json={'csv_text':CROSS}).json()
    assert r['decision']=='carry' and r['to_period']=='2026-11-03'
    d=client.get('/api/ledger/periods/2026-11-03').json()
    assert d['carry_in_total']=='89.90' and len(d['adjustments_in'])==1
    # original period keeps v1
    assert client.get('/api/ledger/periods/2026-11-02').json()['current_version']==1

def test_reissue_rejected(client):
    client.post('/api/ledger/issue',json={'period_id':'2026-11-02'})
    assert client.post('/api/ledger/issue',json={'period_id':'2026-11-02'}).status_code==409

def test_invalid_decision_rejected(client):
    r=client.post('/api/ledger/supplements',json={'csv_text':CROSS,'decision':'explode'})
    assert r.status_code==422

def test_empty_supplement_rejected(client):
    r=client.post('/api/ledger/supplements',json={'csv_text':'   '})
    assert r.status_code==422

def test_exports_are_csv(client):
    client.post('/api/ledger/supplements',json={'csv_text':CROSS})
    for path in ('/api/ledger/export/periods','/api/ledger/export/supplements','/api/ledger/export/deltas/2026-11-02'):
        r=client.get(path);assert r.status_code==200;assert r.headers['content-type'].startswith('text/csv')
        assert r.text.startswith('﻿')
    delta_csv=client.get('/api/ledger/export/deltas/2026-11-02').text
    assert 'difference_source' in delta_csv and 'C00001' in delta_csv

def test_raw_supplement_download_roundtrip(client):
    client.post('/api/ledger/supplements',json={'csv_text':CROSS})
    digest=client.get('/api/ledger/periods/2026-11-02').json()['supplements'][0]['digest']
    r=client.get(f'/api/ledger/supplements/{digest}/raw')
    assert r.status_code==200 and 'C00001' in r.text

def test_demo_supplement_endpoint(client):
    r=client.get('/api/ledger/demo-supplement')
    assert r.status_code==200 and 'subscriber' in r.text

def test_seed_is_idempotent_guard(client):
    # lifespan already seeded 2026-11-02; a second seed must be refused
    assert client.post('/api/ledger/seed',json={'period_id':'2026-11-02'}).status_code==409

def test_malformed_header_returns_422_and_loses_nothing(client):
    before=client.get('/api/ledger/periods/2026-11-02').json()['current_version']
    r=client.post('/api/ledger/supplements',json={'csv_text':'not,a,valid,header\n1,2,3,4\n'})
    assert r.status_code==422 and r.json()['code']=='HEADER'
    # bill unchanged
    assert client.get('/api/ledger/periods/2026-11-02').json()['current_version']==before

def test_close_endpoint_and_closed_carry(client):
    assert client.post('/api/ledger/close',json={'period_id':'2026-11-02'}).status_code==200
    r=client.post('/api/ledger/supplements',json={'csv_text':CROSS}).json()
    assert r['decision']=='carry'  # closed periods never reopen
    assert client.get('/api/ledger/periods/2026-11-02').json()['current_version']==1

def test_unknown_period_detail_404(client):
    assert client.get('/api/ledger/periods/1999-01-01').status_code==404
