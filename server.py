#!/usr/bin/env python3
"""Token 消耗可视化后台管理系统 —— HTTP API + 静态文件服务。

仅依赖 Python 3 标准库，离线运行。
    python3 server.py [--host 127.0.0.1] [--port 8765]

启动时确保库结构存在；若 usage_events 为空则触发一次采集。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import collector
import auth

BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "web")
DB_PATH = collector.DB_PATH

# timeseries.by_agent 必须始终包含这三个 key（缺失补 0）
AGENTS = ("pi", "codex", "claude")

# 静态文件白名单：请求路径 -> (相对 web/ 的文件名, Content-Type)
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/login.html": ("login.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

JSON_TYPE = "application/json; charset=utf-8"

# 串行化刷新：避免两个并发 POST /api/refresh 同时启动 collector 写库。
_REFRESH_LOCK = threading.Lock()


def _run_collector_fresh():
    """在全新解释器中执行磁盘上最新的 collector.py，绕开进程内模块缓存。

    以 __main__ 直接运行脚本（不 import、不 reload），不经过模块字节码缓存，
    每次刷新必然读取当前磁盘上的 collector.py，且与 start.sh 的采集路径一致。
    """
    cp = subprocess.run(
        [sys.executable, os.path.join(BASE, "collector.py")],
        capture_output=True, text=True, cwd=BASE, timeout=300,
    )
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "collector failed")
    stats = json.loads(cp.stdout)
    return {
        "files_scanned": int(stats.get("files_scanned", 0)),
        "records_inserted": int(stats.get("records_inserted", 0)),
    }


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_days(qs):
    raw = qs.get("days", [None])[0]
    if raw is None:
        return 30
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 30


def _day_clause(days):
    """按 UTC day 过滤：day >= date(today) - (days-1)；days<=0 表示全部。

    返回 (SQL 片段, 参数列表)。
    """
    if days <= 0:
        return "", []
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    return " WHERE day >= ?", [start.isoformat()]


def api_overview(qs):
    days = _parse_days(qs)
    clause, params = _day_clause(days)
    sql = (
        "SELECT "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
        "COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens, "
        "COALESCE(SUM(cost_usd),0.0) AS total_cost_usd, "
        "COUNT(DISTINCT agent) AS agent_count, "
        "COUNT(DISTINCT session_id) AS session_count, "
        "COUNT(*) AS event_count, "
        "MIN(day) AS first_day, MAX(day) AS last_day "
        "FROM usage_events" + clause
    )
    conn = _connect()
    try:
        r = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return {
        "total_tokens": r["total_tokens"],
        "input_tokens": r["input_tokens"],
        "output_tokens": r["output_tokens"],
        "cache_read_tokens": r["cache_read_tokens"],
        "cache_write_tokens": r["cache_write_tokens"],
        "reasoning_tokens": r["reasoning_tokens"],
        "total_cost_usd": float(r["total_cost_usd"]),
        "agent_count": r["agent_count"],
        "session_count": r["session_count"],
        "event_count": r["event_count"],
        "first_day": r["first_day"],
        "last_day": r["last_day"],
        "days": days,
    }


def api_by_agent(qs):
    days = _parse_days(qs)
    clause, params = _day_clause(days)
    sql = (
        "SELECT agent, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
        "COUNT(*) AS event_count, "
        "COUNT(DISTINCT session_id) AS session_count, "
        "COALESCE(SUM(cost_usd),0.0) AS cost_usd "
        "FROM usage_events" + clause + " "
        "GROUP BY agent ORDER BY total_tokens DESC"
    )
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{
        "agent": r["agent"],
        "total_tokens": r["total_tokens"],
        "input_tokens": r["input_tokens"],
        "output_tokens": r["output_tokens"],
        "cache_read_tokens": r["cache_read_tokens"],
        "cache_write_tokens": r["cache_write_tokens"],
        "event_count": r["event_count"],
        "session_count": r["session_count"],
        "cost_usd": float(r["cost_usd"]),
    } for r in rows]


def api_timeseries(qs):
    days = _parse_days(qs)
    clause, params = _day_clause(days)
    sql = (
        "SELECT day, agent, COALESCE(SUM(total_tokens),0) AS total_tokens "
        "FROM usage_events" + clause + " "
        "GROUP BY day, agent ORDER BY day ASC"
    )
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    buckets = {}
    for r in rows:
        day = r["day"]
        if day not in buckets:
            buckets[day] = {
                "day": day,
                "by_agent": {a: 0 for a in AGENTS},
                "total": 0,
            }
        tok = r["total_tokens"]
        if r["agent"] in buckets[day]["by_agent"]:
            buckets[day]["by_agent"][r["agent"]] += tok
        buckets[day]["total"] += tok
    return [buckets[d] for d in sorted(buckets)]


def api_by_model(qs):
    days = _parse_days(qs)
    clause, params = _day_clause(days)
    sql = (
        "SELECT model, agent, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COUNT(*) AS event_count "
        "FROM usage_events" + clause + " "
        "GROUP BY model, agent ORDER BY total_tokens DESC"
    )
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{
        "model": r["model"],
        "agent": r["agent"],
        "total_tokens": r["total_tokens"],
        "event_count": r["event_count"],
    } for r in rows]


def api_sessions(qs):
    days = _parse_days(qs)
    clause, params = _day_clause(days)
    try:
        limit = int(qs.get("limit", [50])[0])
    except (TypeError, ValueError):
        limit = 50
    if limit <= 0:
        limit = 50
    limit = min(limit, 10000)
    sql = (
        "SELECT session_id, agent, MIN(model) AS model, MIN(day) AS day, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COUNT(*) AS event_count "
        "FROM usage_events" + clause + " "
        "GROUP BY session_id, agent "
        "ORDER BY total_tokens DESC LIMIT ?"
    )
    conn = _connect()
    try:
        rows = conn.execute(sql, params + [limit]).fetchall()
    finally:
        conn.close()
    return [{
        "session_id": r["session_id"],
        "agent": r["agent"],
        "model": r["model"],
        "day": r["day"],
        "total_tokens": r["total_tokens"],
        "event_count": r["event_count"],
    } for r in rows]


def api_meta(qs):
    conn = _connect()
    try:
        agents = [r["agent"] for r in conn.execute(
            "SELECT DISTINCT agent FROM usage_events ORDER BY agent"
        ).fetchall()]
        row = conn.execute(
            "SELECT finished_at, started_at FROM import_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    last_import = None
    if row is not None:
        last_import = row["finished_at"] or row["started_at"]
    return {
        "agents": agents,
        "db_path": DB_PATH,
        "last_import": last_import,
    }


API_GET = {
    "/api/overview": api_overview,
    "/api/by-agent": api_by_agent,
    "/api/timeseries": api_timeseries,
    "/api/by-model": api_by_model,
    "/api/sessions": api_sessions,
    "/api/meta": api_meta,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 底层发送 ---------------------------------------------------------
    def _write(self, status, body, content_type, is_head, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if not is_head:
            self.wfile.write(body)

    def _send_json(self, status, obj, is_head, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._write(status, body, JSON_TYPE, is_head, extra_headers)

    # ---- API --------------------------------------------------------------
    def _handle_api(self, method, parsed, is_head, payload):
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if method == "POST" and path == "/api/auth/login":
                status, obj, cookie = auth.login(self, payload)
                extra = {"Set-Cookie": cookie} if cookie else None
                self._send_json(status, obj, is_head, extra)
                return
            if method == "GET" and path == "/api/auth/status":
                status, obj, cookie = auth.status(self)
                self._send_json(status, obj, is_head)
                return
            session = auth.authenticate(self)
            if session is None:
                self._send_json(
                    401, {"ok": False, "error": "unauthorized"}, is_head
                )
                return
            if method == "POST" and path == "/api/auth/logout":
                status, obj, cookie = auth.logout(self, session)
                self._send_json(status, obj, is_head, {"Set-Cookie": cookie})
                return
            if method == "POST" and path == "/api/auth/change-pw":
                status, obj, cookie = auth.change_password(self, session, payload)
                extra = {"Set-Cookie": cookie} if cookie else None
                self._send_json(status, obj, is_head, extra)
                return
            if method == "GET" and path in API_GET:
                data = API_GET[path](qs)
            elif method == "POST" and path == "/api/refresh":
                with _REFRESH_LOCK:
                    data = _run_collector_fresh()
            else:
                self._send_json(
                    404, {"ok": False, "error": "not found: " + path}, is_head
                )
                return
            self._send_json(200, {"ok": True, "data": data}, is_head)
        except Exception as exc:  # 不把堆栈泄漏给前端
            self._send_json(500, {"ok": False, "error": str(exc)}, is_head)

    # ---- 静态文件 ---------------------------------------------------------
    def _serve_static(self, path, is_head):
        if path != "/login.html" and auth.authenticate(self) is None:
            self._write(
                302, b"", "text/plain; charset=utf-8", is_head,
                {"Location": "/login.html"},
            )
            return
        entry = STATIC.get(path)
        if entry is None:
            self._send_json(
                404, {"ok": False, "error": "not found: " + path}, is_head
            )
            return
        filename, ctype = entry
        web_root = os.path.realpath(WEB_DIR)
        full = os.path.realpath(os.path.join(web_root, filename))
        # 白名单 + realpath 校验：拒绝任何目录穿越
        if os.path.commonpath([full, web_root]) != web_root or not os.path.isfile(full):
            self._send_json(
                404, {"ok": False, "error": "not found: " + path}, is_head
            )
            return
        with open(full, "rb") as f:
            body = f.read()
        self._write(200, body, ctype, is_head)

    # ---- 路由 -------------------------------------------------------------
    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        is_head = method == "HEAD"
        effective = "GET" if method == "HEAD" else method
        payload = None
        if effective == "POST":
            payload = auth.read_json_body(self)
            if payload is None:
                self._send_json(
                    400, {"ok": False, "error": "invalid request"}, is_head
                )
                return
        if path.startswith("/api/"):
            self._handle_api(effective, parsed, is_head, payload)
            return
        if effective == "GET":
            try:
                self._serve_static(path, is_head)
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)}, is_head)
        else:
            self._send_json(
                405, {"ok": False, "error": "method not allowed"}, is_head
            )

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")


def main():
    parser = argparse.ArgumentParser(description="Token 用量看板 API 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    collector.ensure_schema(DB_PATH)
    initial_password = auth.initialize()
    if initial_password:
        print("initial credential: admin / %s" % initial_password, flush=True)
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    finally:
        conn.close()
    if count == 0:
        _run_collector_fresh()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("serving on http://%s:%d/  db=%s" % (args.host, args.port, DB_PATH),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
