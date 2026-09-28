"""Copy a quiescent SQLite database into an empty configured PostgreSQL database.

Stop API and Worker writes first. Copy media separately, preserving paths or run
on the same host/data mount. No credentials or task documents are printed.
"""
import argparse
import sqlite3
from pathlib import Path
from app import store, database


def migrate(source):
    if not database.url():raise RuntimeError('请配置目标 VIDEO_AGENT_DATABASE_URL')
    source=Path(source).resolve()
    if not source.is_file():raise RuntimeError('源 SQLite 文件不存在')
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        with store.connect() as dst:
            dst.execute('BEGIN IMMEDIATE')
            for table in ('jobs','assets','request_keys','users','audit_events','login_limits','revoked_sessions'):
                if dst.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']:
                    raise RuntimeError('目标数据库不为空；禁止覆盖或重复迁移')
            tables={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            report={}
            for table in ('jobs','assets','request_keys','users','audit_events','login_limits','revoked_sessions'):
                if table not in tables:continue
                rows=src.execute(f'SELECT * FROM {table}').fetchall()
                for row in rows:
                    dst.execute(f'INSERT INTO {table} VALUES ('+','.join('?' for _ in row)+')',row)
                report[table]=len(rows)
            return report
    finally:src.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    print(migrate(parser.parse_args().source))
