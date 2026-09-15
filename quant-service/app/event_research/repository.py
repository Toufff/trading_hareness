"""Bounded append-only event evidence; no access to portfolio tables."""
from datetime import timedelta
from psycopg.types.json import Json
from .contracts import timestamp

def store_documents(db,docs):
    with db.transaction() as c:
        for d in docs:
            c.execute('''INSERT INTO quant.event_research_documents
              (document_id,content_hash,published_at,available_at,document) VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(document_id) DO NOTHING''',
              (d['document_id'],d['content_hash'],d['published_at'],d['available_at'],Json(d)))

def documents(db,cutoff):
    cutoff=timestamp(cutoff)
    with db.transaction() as c:
        rows=c.execute('''SELECT document FROM quant.event_research_documents
          WHERE available_at<=%s AND published_at BETWEEN %s AND %s
          ORDER BY published_at DESC LIMIT 2400''',(cutoff,cutoff-timedelta(days=7),cutoff)).fetchall()
    return [r['document'] for r in rows]

def instruments(db):
    with db.transaction() as c:
        return c.execute("SELECT symbol,name FROM quant.instruments WHERE symbol ~ '^[0-9]{6}\\.(SH|SZ|BJ)$'").fetchall()

def save(db,result):
    with db.transaction() as c:
        c.execute('''INSERT INTO quant.event_research_runs(run_id,cutoff,input_hash,status,result)
         VALUES(%s,%s,%s,%s,%s)''',(result['run_id'],result['cutoff'],result['input_hash'],result['status'],Json(result)))

def latest(db,cutoff):
    with db.transaction() as c:
        row=c.execute('''SELECT result FROM quant.event_research_runs WHERE cutoff<=%s
            ORDER BY cutoff DESC,created_at DESC LIMIT 1''',(cutoff,)).fetchone()
    return row['result'] if row else None
