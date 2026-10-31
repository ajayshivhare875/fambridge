"""
FamBridge - Supabase/Postgres storage backend.

Drop-in persistence for common.load_json / common.save_json / the Fernet key.
When the DATABASE_URL env var is set, JSON docs live in the `kv_store` table.
When it is unset or unreachable, everything falls back to local data/*.json
files, so the gateway keeps working in both modes.

Run once in the Supabase SQL editor:

    create table if not exists kv_store (
      key text primary key,
      value jsonb not null,
      updated_at timestamptz default now()
    );

Then set DATABASE_URL on Render (Project Settings -> Database -> URI).
"""

import base64
import json
import os
import sys
import threading

FERNET_DB_KEY = "fernet.key"

_MISSING = object()
_logged_ok = False

# Connection pool: creating a fresh psycopg2 connection per query was taking
# ~800ms each (Supabase pooler handshake), making every bot button press take
# 2-3 seconds. Reusing pooled connections cuts this to ~100ms per query.
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                import psycopg2.pool
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    1, 5, os.environ["DATABASE_URL"], connect_timeout=10)
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn, close=False):
    try:
        if close:
            _get_pool().putconn(conn, close=True)
        else:
            _get_pool().putconn(conn)
    except Exception:
        pass


def _reset_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None


def db_enabled():
    """True when a Postgres URL is configured."""
    return bool(os.getenv("DATABASE_URL", "").strip())


def _note_ok_once():
    global _logged_ok
    if not _logged_ok:
        _logged_ok = True
        print("[db_store] connected: using Supabase kv_store for persistence", flush=True)


def db_load(key, default=_MISSING):
    """Return the stored JSON value for key, or `default` when absent/unreachable."""
    if not db_enabled():
        return default
    for attempt in (1, 2):
        conn = None
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                row = cur.fetchone()
            _put_conn(conn)
            if row is None:
                return default
            _note_ok_once()
            v = row[0]
            # psycopg2 usually returns jsonb already parsed; handle raw text too.
            return json.loads(v) if isinstance(v, str) else v
        except Exception as e:  # DB down/misconfigured -> caller falls back to files
            if conn is not None:
                _put_conn(conn, close=True)
            if attempt == 1:
                # Stale pooled connection? Reset pool and retry once.
                _reset_pool()
                continue
            print(f"[db_store] load failed for {key!r}: {e}", file=sys.stderr, flush=True)
            return default


def db_save(key, data):
    """Upsert the JSON value for key. True on success, False on failure."""
    if not db_enabled():
        return False
    for attempt in (1, 2):
        conn = None
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kv_store (key, value, updated_at)"
                    " VALUES (%s, %s::jsonb, now())"
                    " ON CONFLICT (key) DO UPDATE"
                    " SET value = EXCLUDED.value, updated_at = now()",
                    (key, json.dumps(data)),
                )
            conn.commit()
            _put_conn(conn)
            _note_ok_once()
            return True
        except Exception as e:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _put_conn(conn, close=True)
            if attempt == 1:
                # Stale pooled connection? Reset pool and retry once.
                _reset_pool()
                continue
            print(f"[db_store] save failed for {key!r}: {e}", file=sys.stderr, flush=True)
            return False


def db_get_fernet_key():
    """Return the persisted Fernet key bytes, or None."""
    raw = db_load(FERNET_DB_KEY, None)
    if not raw:
        return None
    try:
        return base64.b64decode(raw.encode())
    except Exception:
        return None


def db_set_fernet_key(key_bytes):
    """Persist the Fernet key so redeploys can still decrypt stored secrets."""
    return db_save(FERNET_DB_KEY, base64.b64encode(key_bytes).decode())
