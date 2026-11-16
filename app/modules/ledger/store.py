import base64,json,os,sqlite3,time
from contextlib import contextmanager
from pathlib import Path

def connect():
    path=os.environ.get('DB_PATH','/tmp/cdrforge.sqlite3')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

@contextmanager
def cursor():
    con=connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback();raise
    finally:
        con.close()

def init(con=None):
    own=con is None
    con=con or connect()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS periods(
      period_id TEXT PRIMARY KEY,
      state TEXT NOT NULL CHECK(state IN ('open','issued','closed')),
      carry_in TEXT NOT NULL DEFAULT '0.00',
      created_at TEXT NOT NULL,
      issued_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fragments(
      period_id TEXT NOT NULL REFERENCES periods(period_id),
      fingerprint TEXT NOT NULL,
      subscriber TEXT NOT NULL,
      session TEXT NOT NULL,
      sequence INTEGER NOT NULL,
      start TEXT NOT NULL,
      end TEXT NOT NULL,
      zone TEXT NOT NULL,
      first_line INTEGER NOT NULL,
      PRIMARY KEY(period_id,fingerprint)
    );
    CREATE TABLE IF NOT EXISTS bills(
      period_id TEXT NOT NULL REFERENCES periods(period_id),
      version INTEGER NOT NULL,
      total_fee TEXT NOT NULL,
      minutes INTEGER NOT NULL,
      sessions INTEGER NOT NULL,
      watermark TEXT NOT NULL,
      summary TEXT NOT NULL,
      bundle TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY(period_id,version)
    );
    CREATE TABLE IF NOT EXISTS deltas(
      period_id TEXT NOT NULL,
      version INTEGER NOT NULL,
      session TEXT NOT NULL,
      old_fee TEXT,
      new_fee TEXT NOT NULL,
      delta TEXT NOT NULL,
      old_seconds TEXT,
      new_seconds TEXT NOT NULL,
      old_minutes INTEGER,
      new_minutes INTEGER NOT NULL,
      source TEXT NOT NULL,
      PRIMARY KEY(period_id,version,session)
    );
    CREATE TABLE IF NOT EXISTS supplements(
      digest TEXT PRIMARY KEY,
      period_id TEXT NOT NULL,
      received_at TEXT NOT NULL,
      filename TEXT NOT NULL,
      raw_b64 TEXT NOT NULL,
      input_rows INTEGER NOT NULL,
      accepted_rows INTEGER NOT NULL,
      duplicate_rows INTEGER NOT NULL,
      quarantined_rows INTEGER NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('RECEIVED','APPLIED','DUPLICATE','REJECTED')),
      decision TEXT,
      bill_version INTEGER,
      reason TEXT,
      detail TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS adjustments(
      period_id TEXT NOT NULL,
      from_period TEXT NOT NULL,
      supplement_digest TEXT NOT NULL,
      session TEXT NOT NULL,
      amount TEXT NOT NULL,
      source TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY(period_id,supplement_digest,session)
    );''')
    if own:con.commit();con.close()

def now():return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
def _dumps(v):return json.dumps(v,ensure_ascii=False,sort_keys=True)

# ---------- periods ----------
def get_period(period_id):
    with cursor() as con:
        row=con.execute('SELECT * FROM periods WHERE period_id=?',(period_id,)).fetchone()
    return dict(row) if row else None

def ensure_period(period_id):
    with cursor() as con:
        con.execute('INSERT OR IGNORE INTO periods(period_id,state,created_at) VALUES(?,?,?)',(period_id,'open',now()))
        row=con.execute('SELECT * FROM periods WHERE period_id=?',(period_id,)).fetchone()
    return dict(row)

def list_periods():
    with cursor() as con:
        rows=con.execute('SELECT * FROM periods ORDER BY period_id').fetchall()
        out=[]
        for r in rows:
            p=dict(r)
            bill=con.execute('SELECT version,total_fee,minutes,sessions,watermark,summary,created_at FROM bills WHERE period_id=? ORDER BY version DESC LIMIT 1',(p['period_id'],)).fetchone()
            adj=con.execute('SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS total,COUNT(*) AS n FROM adjustments WHERE period_id=?',(p['period_id'],)).fetchone()
            p['latest_bill']=dict(bill) if bill else None
            if bill:
                p['latest_bill']['watermark']=json.loads(bill['watermark'])
                p['latest_bill']['summary']=json.loads(bill['summary'])
            p['adjustment_count']=adj['n'];p['adjustment_total']=f"{adj['total']:.2f}"
            out.append(p)
    return out

def issue_period(period_id):
    with cursor() as con:
        cur=con.execute("UPDATE periods SET state='issued',issued_at=COALESCE(issued_at,?) WHERE period_id=? AND state='open'",(now(),period_id))
        return cur.rowcount==1

# ---------- fragments (watermark source of truth) ----------
def list_fragments(period_id):
    with cursor() as con:
        rows=con.execute('SELECT * FROM fragments WHERE period_id=? ORDER BY start,session,sequence',(period_id,)).fetchall()
    return [dict(r) for r in rows]

def add_fragments(period_id,fragments):
    """INSERT OR IGNORE on (period_id,fingerprint). Returns (inserted,skipped)."""
    inserted=0
    with cursor() as con:
        for f in fragments:
            cur=con.execute('''INSERT OR IGNORE INTO fragments(period_id,fingerprint,subscriber,session,sequence,start,end,zone,first_line)
                VALUES(?,?,?,?,?,?,?,?,?)''',(period_id,f['fingerprint'],f['subscriber'],f['session'],f['sequence'],f['start'],f['end'],f['zone'],f['first_line']))
            inserted+=cur.rowcount
    return inserted,len(fragments)-inserted

# ---------- bills & deltas ----------
def list_bill_versions(period_id):
    with cursor() as con:
        rows=con.execute('SELECT period_id,version,total_fee,minutes,sessions,watermark,summary,created_at FROM bills WHERE period_id=? ORDER BY version',(period_id,)).fetchall()
    out=[]
    for r in rows:
        d=dict(r);d['watermark']=json.loads(r['watermark']);d['summary']=json.loads(r['summary']);out.append(d)
    return out

def get_bill(period_id,version=None):
    with cursor() as con:
        if version is None:
            row=con.execute('SELECT * FROM bills WHERE period_id=? ORDER BY version DESC LIMIT 1',(period_id,)).fetchone()
        else:
            row=con.execute('SELECT * FROM bills WHERE period_id=? AND version=?',(period_id,version)).fetchone()
    if not row:return None
    d=dict(row);d['watermark']=json.loads(row['watermark']);d['summary']=json.loads(row['summary']);d['bundle']=json.loads(row['bundle']);return d

def list_deltas(period_id,version):
    with cursor() as con:
        rows=con.execute('SELECT * FROM deltas WHERE period_id=? AND version=? ORDER BY session',(period_id,version)).fetchall()
    return [dict(r) for r in rows]

def _apply_supplement_state(con,supplement_digest,decision,version):
    con.execute("UPDATE supplements SET state='APPLIED',decision=?,bill_version=?,reason=NULL,detail=? WHERE digest=?",
                (decision,version,_dumps({'applied_at':now()}),supplement_digest))

def switch_connection():
    """Dedicated connection for the atomic version switch; isolated so a failure
    here cannot corrupt the read/archive connections."""
    return connect()

def write_reopen(period_id,version,bill,deltas,state,supplement_digest):
    """Atomic reopen: insert the new bill version + deltas and point the period at it.
    Any failure rolls back; every prior bill version stays intact."""
    con=switch_connection()
    try:
        con.execute('BEGIN IMMEDIATE')
        con.execute('''INSERT INTO bills(period_id,version,total_fee,minutes,sessions,watermark,summary,bundle,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',(period_id,version,bill['total_fee'],bill['minutes'],bill['sessions'],_dumps(bill['watermark']),_dumps(bill['summary']),_dumps(bill['bundle']),now()))
        for d in deltas:
            con.execute('''INSERT INTO deltas(period_id,version,session,old_fee,new_fee,delta,old_seconds,new_seconds,old_minutes,new_minutes,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(period_id,version,d['session'],d['old_fee'],d['new_fee'],d['delta'],d['old_seconds'],d['new_seconds'],d['old_minutes'],d['new_minutes'],d['source']))
        con.execute("UPDATE periods SET state=? WHERE period_id=?",(state,period_id))
        _apply_supplement_state(con,supplement_digest,'reopen',version)
        con.commit()
    except Exception:
        con.rollback();raise
    finally:
        con.close()

def write_carry(period_id,version,adjustments,state,supplement_digest):
    """Atomic carry-forward: no new bill row is written (the prior version is preserved);
    only adjustment lines and the supplement state change."""
    con=switch_connection()
    try:
        con.execute('BEGIN IMMEDIATE')
        for a in adjustments:
            con.execute('''INSERT OR IGNORE INTO adjustments(period_id,from_period,supplement_digest,session,amount,source,created_at)
                VALUES(?,?,?,?,?,?,?)''',(a['period_id'],a['from_period'],supplement_digest,a['session'],a['amount'],a['source'],now()))
        con.execute("UPDATE periods SET state=? WHERE period_id=?",(state,period_id))
        _apply_supplement_state(con,supplement_digest,'carry',version)
        con.commit()
    except Exception:
        con.rollback();raise
    finally:
        con.close()

# ---------- supplements (raw archive, idempotent by digest) ----------
def supplement_exists(digest):
    with cursor() as con:
        row=con.execute('SELECT digest,state,decision,bill_version,period_id FROM supplements WHERE digest=?',(digest,)).fetchone()
    return dict(row) if row else None

def archive_supplement(period_id,digest,filename,raw,counts,detail,state='RECEIVED',decision=None,reason=None):
    with cursor() as con:
        con.execute('''INSERT OR IGNORE INTO supplements(digest,period_id,received_at,filename,raw_b64,input_rows,accepted_rows,duplicate_rows,quarantined_rows,state,decision,bill_version,reason,detail)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(digest,period_id,now(),filename,base64.b64encode(raw).decode('ascii'),counts['input'],counts['accepted'],counts['duplicate'],counts['quarantined'],state,decision,None,reason,_dumps(detail)))
        row=con.execute('SELECT * FROM supplements WHERE digest=?',(digest,)).fetchone()
    return dict(row)

def reject_supplement(digest,reason,detail):
    with cursor() as con:
        con.execute("UPDATE supplements SET state='REJECTED',reason=?,detail=? WHERE digest=?",(reason,_dumps(detail),digest))

def list_supplements(period_id=None):
    with cursor() as con:
        if period_id:
            rows=con.execute('SELECT digest,period_id,received_at,filename,input_rows,accepted_rows,duplicate_rows,quarantined_rows,state,decision,bill_version,reason FROM supplements WHERE period_id=? ORDER BY received_at',(period_id,)).fetchall()
        else:
            rows=con.execute('SELECT digest,period_id,received_at,filename,input_rows,accepted_rows,duplicate_rows,quarantined_rows,state,decision,bill_version,reason FROM supplements ORDER BY received_at').fetchall()
    return [dict(r) for r in rows]

def get_supplement(digest):
    with cursor() as con:
        row=con.execute('SELECT * FROM supplements WHERE digest=?',(digest,)).fetchone()
    if not row:return None
    d=dict(row);d['detail']=json.loads(row['detail']);d['raw_text']=base64.b64decode(row['raw_b64']).decode('utf-8-sig');return d

# ---------- carry-forward adjustments ----------
def list_adjustments(period_id):
    with cursor() as con:
        rows=con.execute('SELECT * FROM adjustments WHERE period_id=? ORDER BY created_at,session',(period_id,)).fetchall()
    return [dict(r) for r in rows]
