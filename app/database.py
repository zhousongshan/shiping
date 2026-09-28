"""One small SQL boundary, retaining SQLite for local development and migration.

All SQL passed to this module is internal. JSON documents remain readable by old
jobs while production records are gradually normalized into dedicated tables.
"""
import os
import sqlite3
import threading
import fcntl

_ready = set()
_sqlite_ready = set()
_lock = threading.Lock()
WRITE_LOCK = 47800001
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, updated DOUBLE PRECISION, doc TEXT)',
    'CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, doc TEXT)',
    'CREATE INDEX IF NOT EXISTS jobs_status_updated ON jobs(status,updated)',
    'CREATE TABLE IF NOT EXISTS request_keys (owner TEXT, request_key TEXT, digest TEXT NOT NULL, job_id TEXT NOT NULL, PRIMARY KEY(owner,request_key))',
    'CREATE TABLE IF NOT EXISTS service_heartbeat (name TEXT PRIMARY KEY, updated DOUBLE PRECISION NOT NULL)',
    'CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, token_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)',
    'CREATE TABLE IF NOT EXISTS login_limits (identity TEXT PRIMARY KEY, failures INTEGER NOT NULL, reset_at DOUBLE PRECISION NOT NULL)',
    'CREATE TABLE IF NOT EXISTS revoked_sessions (digest TEXT PRIMARY KEY, expires DOUBLE PRECISION NOT NULL)',
    'CREATE TABLE IF NOT EXISTS generation_authorizations (owner TEXT, request_key TEXT, digest TEXT NOT NULL, max_seconds DOUBLE PRECISION NOT NULL, max_submissions INTEGER NOT NULL, expires DOUBLE PRECISION NOT NULL, note TEXT NOT NULL, job_id TEXT, PRIMARY KEY(owner,request_key))',
)


def url():
    return os.getenv('VIDEO_AGENT_DATABASE_URL', '')


class Connection:
    def __init__(self, raw, postgres=False):
        self.raw, self.postgres = raw, postgres

    def execute(self, sql, parameters=()):
        if self.postgres:
            if sql == 'BEGIN IMMEDIATE':
                # Serialize read-modify-write operations, including daily quotas.
                # The current single queue leader uses short transactions only.
                return self.raw.execute('SELECT pg_advisory_xact_lock(%s)', (WRITE_LOCK,))
            sql = sql.replace('?', '%s')
        return self.raw.execute(sql, parameters)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        try:
            self.raw.rollback() if kind else self.raw.commit()
        finally:
            self.raw.close()


def connect(data):
    dsn = url()
    if not dsn:
        data.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(data/'tasks.sqlite', timeout=30)
        raw.row_factory = sqlite3.Row
        identity=(str(data.resolve()),(data/'tasks.sqlite').stat().st_ino)
        with _lock:
            if identity not in _sqlite_ready:
                try:
                    with (data/'.schema.lock').open('a') as lock:
                        fcntl.flock(lock,fcntl.LOCK_EX)
                        if raw.execute('PRAGMA journal_mode').fetchone()[0]!='wal':
                            raw.execute('PRAGMA journal_mode=WAL')
                        for sql in SCHEMA:raw.execute(sql)
                        raw.commit()
                    _sqlite_ready.add(identity)
                except BaseException:
                    raw.close()
                    raise
        return Connection(raw)
    import psycopg
    from psycopg.rows import dict_row
    # DDL is serialized across processes, once per process/connection target.
    with _lock:
        if dsn not in _ready:
            with psycopg.connect(dsn, connect_timeout=5) as c:
                c.execute('SELECT pg_advisory_xact_lock(%s)', (WRITE_LOCK,))
                for sql in SCHEMA:
                    c.execute(sql)
            _ready.add(dsn)
    return Connection(psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5), True)
