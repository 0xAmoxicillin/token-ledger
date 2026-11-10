#!/usr/bin/env python3
"""Local token-usage collector.

Scans the three on-disk agent JSONL logs (Claude Code, Codex, Pi), normalises
each token-usage record and stores it in a single SQLite database.

Python 3 standard library only. No network access, no external packages.
Run directly:  python3 collector.py   (idempotent; prints a JSON summary).
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import sqlite3

# ---------------------------------------------------------------------------
# Paths / constants (exact contract)
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'data', 'usage.db')

BATCH_SIZE = 2000  # executemany rows per commit


def _data_home():
    """Resolve the home directory that holds the agent log trees."""
    return os.path.expanduser('~')


# Column order shared by the INSERT statement and every produced row tuple.
COLUMNS = [
    'agent', 'model', 'ts', 'day', 'session_id',
    'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens',
    'reasoning_tokens', 'total_tokens', 'cost_usd', 'source_path', 'dedup_key',
]

INSERT_SQL = (
    'INSERT OR IGNORE INTO usage_events (' + ','.join(COLUMNS) + ') '
    'VALUES (' + ','.join(['?'] * len(COLUMNS)) + ')'
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL,
  model TEXT NOT NULL,
  ts TEXT NOT NULL,
  day TEXT NOT NULL,
  session_id TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL,
  source_path TEXT NOT NULL,
  dedup_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS import_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  files_scanned INTEGER,
  records_inserted INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_events_day   ON usage_events(day);
CREATE INDEX IF NOT EXISTS idx_usage_events_agent ON usage_events(agent);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts    ON usage_events(ts);
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _as_int(value):
    """Coerce to int without ever raising; missing / non-numeric -> 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _as_float(value):
    """Coerce to float without ever raising; missing / non-numeric -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _row(agent, model, ts, session_id,
         input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         reasoning_tokens, total_tokens, cost_usd, source_path, dedup_key):
    """Build one row tuple in COLUMNS order, applying all normalisation."""
    ts = ts if isinstance(ts, str) else ''
    day = ts[:10]
    if model is None:
        model = 'unknown'
    model = str(model)
    return (
        agent,
        model,
        ts,
        day,
        session_id,
        _as_int(input_tokens),
        _as_int(output_tokens),
        _as_int(cache_read_tokens),
        _as_int(cache_write_tokens),
        _as_int(reasoning_tokens),
        _as_int(total_tokens),
        _as_float(cost_usd),
        source_path,
        dedup_key,
    )


def _open_lines(path):
    """Yield (lineno, stripped_line) for a text file, tolerating decode errors."""
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        for lineno, line in enumerate(fh, 1):
            yield lineno, line.strip()


# ---------------------------------------------------------------------------
# Source parsers.  Each yields fully-normalised row tuples (COLUMNS order).
# Every per-line operation is fault-tolerant: a single bad line is skipped,
# never fatal to the import.
# ---------------------------------------------------------------------------
def _iter_claude(path):
    """Claude Code assistant messages.

    usage = message.usage:
        input_tokens, output_tokens,
        cache_creation_input_tokens -> cache_write_tokens,
        cache_read_input_tokens     -> cache_read_tokens
    total_tokens is summed locally (Claude exposes no aggregate total).
    Dedup: prefer message.id (the same assistant message can be written twice).
    """
    for lineno, line in _open_lines(path):
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get('type') != 'assistant':
            continue
        msg = d.get('message')
        if not isinstance(msg, dict):
            continue
        usage = msg.get('usage')
        if not isinstance(usage, dict):
            continue
        try:
            ts = d.get('timestamp', '')
            model = msg.get('model') or 'unknown'
            inp = usage.get('input_tokens', 0)
            out = usage.get('output_tokens', 0)
            cr = usage.get('cache_read_input_tokens', 0)
            cw = usage.get('cache_creation_input_tokens', 0)
            total = _as_int(inp) + _as_int(out) + _as_int(cr) + _as_int(cw)
            session_id = d.get('sessionId') or d.get('session_id')

            mid = msg.get('id')
            if mid:
                dedup_key = 'claude|' + str(mid)
            else:
                dedup_key = 'claude|' + path + '|' + str(lineno) + '|' + str(ts)

            yield _row('claude', model, ts, session_id,
                       inp, out, cr, cw, 0, total, None, path, dedup_key)
        except Exception:
            # Never let one malformed record abort the file / import.
            continue


def _iter_codex(path):
    """Codex token_count events.

    MUST use payload.info.last_token_usage (a single-call increment).  The
    cumulative total_token_usage is NEVER used (it would double count).

    model is resolved from session_meta.payload.model / event_msg
    payload.thread_settings.model.  In the real logs model lives in
    thread_settings and can change mid-session, so we build (ordinal, model)
    checkpoints and attribute each token_count event to the model active at
    its ordinal.  Missing -> 'unknown'.

    Dedup: (source_path, line number, timestamp).
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    raw = []           # (lineno, ordinal, ts, last_usage_dict)
    checkpoints = []   # (ordinal, model)
    session_id = None

    for lineno, line in _open_lines(path):
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        t = d.get('type')
        p = d.get('payload')
        if not isinstance(p, dict):
            p = {}
        ordinal = d.get('ordinal')

        if t == 'session_meta':
            if session_id is None:
                session_id = p.get('session_id') or p.get('id')
            m = p.get('model')
            if m:
                checkpoints.append((_as_int(ordinal), str(m)))
            continue

        if t != 'event_msg':
            continue

        ts_obj = p.get('thread_settings')
        if isinstance(ts_obj, dict) and ts_obj.get('model'):
            checkpoints.append((_as_int(ordinal), str(ts_obj['model'])))

        if p.get('type') == 'token_count':
            info = p.get('info')
            if isinstance(info, dict):
                last = info.get('last_token_usage')
                if isinstance(last, dict):
                    raw.append((lineno, ordinal, d.get('timestamp', ''), last))

    if session_id is None:
        session_id = stem
    checkpoints.sort(key=lambda x: x[0])

    def resolve_model(ordinal):
        if not checkpoints:
            return 'unknown'
        if ordinal is None:
            return checkpoints[0][1]
        ord_i = _as_int(ordinal)
        best = checkpoints[0][1]
        for o, m in checkpoints:
            if o <= ord_i:
                best = m
            else:
                break
        return best

    for (lineno, ordinal, ts, last) in raw:
        try:
            model = resolve_model(ordinal)
            inp = last.get('input_tokens', 0)
            out = last.get('output_tokens', 0)
            cr = last.get('cached_input_tokens', 0)
            cw = last.get('cache_write_input_tokens', 0)
            rs = last.get('reasoning_output_tokens', 0)
            total = last.get('total_tokens', 0)  # source aggregate (the increment)
            dedup_key = 'codex|' + path + '|' + str(lineno) + '|' + str(ts)
            yield _row('codex', model, ts, session_id,
                       inp, out, cr, cw, rs, total, None, path, dedup_key)
        except Exception:
            continue


def _iter_pi(path):
    """Pi assistant messages that carry a usage object.

    usage = message.usage:
        input, output, cacheRead, cacheWrite, totalTokens, cost.total
    session_id is taken from the file name.
    Dedup: content-based stable key (message.timestamp + usage fields),
    WITHOUT source_path/line number, so cross-file session copies collapse.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    for lineno, line in _open_lines(path):
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get('type') != 'message':
            continue
        msg = d.get('message')
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        usage = msg.get('usage')
        if not isinstance(usage, dict):
            continue
        try:
            ts = d.get('timestamp', '')
            model = msg.get('model') or 'unknown'
            cost = None
            cost_obj = usage.get('cost')
            if isinstance(cost_obj, dict):
                cost = cost_obj.get('total')
            inp = usage.get('input', 0)
            out = usage.get('output', 0)
            cr = usage.get('cacheRead', 0)
            cw = usage.get('cacheWrite', 0)
            total = usage.get('totalTokens', 0)
            # Identical messages are duplicated across session-file copies
            # (same line numbers), so the key must be derived from content.
            # Inner message.timestamp (epoch ms) + full usage breakdown.
            raw = f"{msg.get('timestamp')}|{total}|{inp}|{out}|{cr}|{cw}"
            dedup_key = 'pi|' + hashlib.sha1(raw.encode()).hexdigest()
            yield _row('pi', model, ts, stem,
                       inp, out, cr, cw, 0, total, cost, path, dedup_key)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def ensure_schema(db_path=DB_PATH):
    """Create data/ dir + tables/indexes if absent. Safe to call repeatedly."""
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def _flush(conn, batch):
    """executemany + commit; return how many rows were actually inserted."""
    if not batch:
        return 0
    before = conn.total_changes
    conn.executemany(INSERT_SQL, batch)
    after = conn.total_changes
    conn.commit()
    return after - before


def run_import(db_path=DB_PATH):
    """Scan all three sources and import new, deduplicated records.

    Returns {"files_scanned", "records_inserted", "skipped"} where `skipped`
    is the number of valid records ignored because their dedup_key already
    existed (i.e. duplicates).  Idempotent: a second run inserts 0 rows.
    """
    # Guarantee the data directory exists before touching SQLite.
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    ensure_schema(db_path)

    started_at = _utc_now_iso()
    home = _data_home()
    sources = [
        ('claude', os.path.join(home, '.claude', 'projects', '**', '*.jsonl'), _iter_claude),
        ('codex', os.path.join(home, '.codex', 'sessions', '**', '*.jsonl'), _iter_codex),
        ('pi', os.path.join(home, '.pi', 'agent', 'sessions', '**', '*.jsonl'), _iter_pi),
    ]

    conn = sqlite3.connect(db_path)
    files_scanned = 0
    candidate = 0
    inserted = 0
    batch = []

    try:
        for _agent, pattern, parser in sources:
            for path in sorted(glob.glob(pattern, recursive=True)):
                files_scanned += 1
                try:
                    for row in parser(path):
                        batch.append(row)
                        candidate += 1
                        if len(batch) >= BATCH_SIZE:
                            inserted += _flush(conn, batch)
                            batch = []
                except Exception:
                    # A broken file must not abort the whole import; rows
                    # already buffered from it remain valid and are flushed.
                    continue
        if batch:
            inserted += _flush(conn, batch)
            batch = []

        finished_at = _utc_now_iso()
        conn.execute(
            'INSERT INTO import_runs (started_at, finished_at, files_scanned, records_inserted) '
            'VALUES (?, ?, ?, ?)',
            (started_at, finished_at, files_scanned, inserted),
        )
        conn.commit()
    finally:
        conn.close()

    skipped = candidate - inserted
    return {
        'files_scanned': files_scanned,
        'records_inserted': inserted,
        'skipped': skipped,
    }


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Collect local agent token usage into SQLite (idempotent).'
    )
    ap.add_argument('--db', default=DB_PATH, help='SQLite database path')
    args = ap.parse_args()
    print(json.dumps(run_import(args.db), indent=2, ensure_ascii=False))
