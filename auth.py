#!/usr/bin/env python3
"""Local account, password hash and session management using only stdlib."""
import hashlib
import json
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "auth.db")
CREDENTIAL_PATH = os.path.join(DATA_DIR, "initial-credential.txt")
DEFAULT_USERNAME = "admin"
TOKEN_BYTES = 32
INITIAL_PASSWORD_BYTES = 12
SESSION_DAYS = 7
PBKDF2_ROUNDS = 200000
SALT_BYTES = 16
MAX_BODY_BYTES = 65536


def _utc_iso():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    )


def _verify_password(password, salt, pw_hash):
    candidate = _hash_password(password, salt)
    return hmac.compare_digest(candidate, bytes(pw_hash))


def _write_credential(password):
    fd = os.open(CREDENTIAL_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (password + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(CREDENTIAL_PATH, 0o600)


def initialize():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = _connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id INTEGER PRIMARY KEY, "
            "username TEXT UNIQUE NOT NULL, "
            "salt BLOB, pw_hash BLOB, created_at TEXT, "
            "must_change_pw INTEGER DEFAULT 1)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "token TEXT PRIMARY KEY, user_id INTEGER, "
            "created_at TEXT, expires_at TEXT)"
        )
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count:
            conn.commit()
            os.chmod(DB_PATH, 0o600)
            return None
        password = secrets.token_urlsafe(INITIAL_PASSWORD_BYTES)
        salt = os.urandom(SALT_BYTES)
        pw_hash = _hash_password(password, salt)
        conn.execute(
            "INSERT INTO users "
            "(username, salt, pw_hash, created_at, must_change_pw) "
            "VALUES (?, ?, ?, ?, 1)",
            (DEFAULT_USERNAME, salt, pw_hash, _utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    _write_credential(password)
    os.chmod(DB_PATH, 0o600)
    return password


def read_json_body(handler):
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        length = 0
    else:
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return None
    if length < 0:
        return None
    if length > MAX_BODY_BYTES:
        handler.close_connection = True
        return None
    raw = handler.rfile.read(length)
    if len(raw) != length:
        return None
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _cookie_token(handler):
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "td_session":
            return value
    return None


def session_cookie(token):
    return (
        "td_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800"
        % token
    )


def clear_session_cookie():
    return "td_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"


def authenticate(handler):
    token = _cookie_token(handler)
    if not token:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT s.token, s.expires_at, u.username, u.must_change_pw "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            expired = expires_at <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            expired = True
        if expired:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
        return {
            "token": token,
            "username": row["username"],
            "must_change_pw": bool(row["must_change_pw"]),
        }
    finally:
        conn.close()


def login(handler, payload):
    username = payload.get("username")
    password = payload.get("pw")
    if not isinstance(username, str) or not isinstance(password, str):
        return 401, {"ok": False, "error": "invalid credentials"}, None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, salt, pw_hash, must_change_pw FROM users "
            "WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not _verify_password(password, row["salt"], row["pw_hash"]):
        return 401, {"ok": False, "error": "invalid credentials"}, None
    token = secrets.token_urlsafe(TOKEN_BYTES)
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=SESSION_DAYS)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, row["id"], created_at.isoformat(), expires_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    data = {"must_change_pw": bool(row["must_change_pw"])}
    return 200, {"ok": True, "data": data}, session_cookie(token)


def status(handler):
    session = authenticate(handler)
    if session is None:
        data = {
            "authenticated": False,
            "username": None,
            "must_change_pw": False,
        }
    else:
        data = {
            "authenticated": True,
            "username": session["username"],
            "must_change_pw": session["must_change_pw"],
        }
    return 200, {"ok": True, "data": data}, None


def logout(handler, session):
    conn = _connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (session["token"],))
        conn.commit()
    finally:
        conn.close()
    return 200, {"ok": True, "data": {}}, clear_session_cookie()


def change_password(handler, session, payload):
    old_password = payload.get("old_pw")
    new_password = payload.get("new_pw")
    if not isinstance(old_password, str) or not isinstance(new_password, str):
        return 400, {"ok": False, "error": "invalid request"}, None
    if len(new_password) < 8:
        return 400, {"ok": False, "error": "new password too short"}, None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, salt, pw_hash FROM users WHERE username = ?",
            (session["username"],),
        ).fetchone()
        if row is None or not _verify_password(
            old_password, row["salt"], row["pw_hash"]
        ):
            return 401, {"ok": False, "error": "invalid credentials"}, None
        salt = os.urandom(SALT_BYTES)
        pw_hash = _hash_password(new_password, salt)
        conn.execute(
            "UPDATE users SET salt = ?, pw_hash = ?, must_change_pw = 0 "
            "WHERE id = ?",
            (salt, pw_hash, row["id"]),
        )
        conn.execute(
            "DELETE FROM sessions WHERE token = ?", (session["token"],)
        )
        conn.commit()
    finally:
        conn.close()
    _write_credential(new_password)
    return 200, {"ok": True, "data": {}}, clear_session_cookie()


def reset_initial_state():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    try:
        os.remove(CREDENTIAL_PATH)
    except FileNotFoundError:
        pass
    return initialize()
