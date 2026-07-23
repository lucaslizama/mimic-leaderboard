#!/usr/bin/env python3
"""Mimic! — self-hostable leaderboard server.

A tiny, dependency-free HTTP+JSON leaderboard. Uses only the Python standard
library (``http.server`` + ``sqlite3``), so hosting it is just::

    python3 server.py

No build step, no ``pip install``, no external database. Scores live in a single
SQLite file you fully control.

This is the game's "Stop Killing Games" end-of-life plan: if the official server
ever goes away, anyone can run this and point their client at it. The protocol is
open and the whole server is one readable file, licensed MIT (see LICENSE).

Configuration (all optional, via environment variables):
    PORT          Port to listen on (default 8080; cloud hosts usually set this).
    DB_PATH       Path to the SQLite file (default ./leaderboard.db).
    GAME_KEY      If set, POST /submit must send a matching ``X-Game-Key`` header.
                  A soft spam gate, NOT real anti-cheat (see README "Security").
    CORS_ORIGIN   Allowed browser origin for the web build (default "*").
    RATE_LIMIT    Max submits per client IP per window (default 20).
    RATE_WINDOW   Rate-limit window, seconds (default 600).
    MAX_NAME_LEN  Max characters kept from a submitted name (default 16).
    MIN_WIN_TIME_MS  Fastest believable prize win, ms (default 30000). Submissions
                  claiming reached_goal faster than this are rejected — a debug
                  build or tampered client can otherwise land an unbeatable
                  sub-second "win" on top of the time board. Keep a healthy
                  margin below the current world record: top runs land around
                  50s, so 30s blocks garbage without ever rejecting real play.

API:
    GET  /                                 -> service info + endpoint list
    GET  /health                           -> {"status":"ok",...}
    GET  /leaderboard?sort=time&limit=20   -> {"sort":"time","entries":[...]}
             sort=time  (default) fastest run to the prize, ascending; only runs
                        that reached the goal (reached_goal=true) qualify.
             sort=score highest score, descending; every run counts.
             name=AAA   optional: only that player's runs (e.g. limit=1 for
                        their personal best on the chosen board).
    POST /submit                           -> {"ok":true,"id":N,"time_rank":R,...}
             body: {"name":"AAA","score":18500,"time_ms":161000,"reached_goal":true}
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---- configuration (environment, with sensible defaults) ----
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "leaderboard.db")
GAME_KEY = os.environ.get("GAME_KEY", "")
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "20"))
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "600"))
MAX_NAME_LEN = int(os.environ.get("MAX_NAME_LEN", "16"))
KEEP_PER_PLAYER = int(os.environ.get("KEEP_PER_PLAYER", "10"))  # per-name retention (see _submit)

# Sanity bounds so a malformed/hostile client can't store absurd values.
MAX_SCORE = 100_000_000
MAX_TIME_MS = 24 * 60 * 60 * 1000  # a run longer than 24h is not a real run
# Banking the prize takes real slicing through every stage; record runs land
# around 50s, so anything under half a minute is a debug/tampered client.
MIN_WIN_TIME_MS = int(os.environ.get("MIN_WIN_TIME_MS", "30000"))

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # strip control chars from names
_rate: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def db() -> sqlite3.Connection:
    """A fresh connection per request — simple and thread-safe under threading."""
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # tolerate concurrent readers
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                score        INTEGER NOT NULL,
                time_ms      INTEGER NOT NULL,
                reached_goal INTEGER NOT NULL,
                created_at   TEXT    NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_time ON scores(reached_goal, time_ms)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_score ON scores(score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON scores(name)")
        conn.commit()
    finally:
        conn.close()


def sanitize_name(raw: object) -> str | None:
    """Trim, collapse whitespace, drop control chars, cap length. None if empty."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"\s+", " ", _CONTROL.sub("", raw)).strip()
    return name[:MAX_NAME_LEN] if name else None


def rate_ok(ip: str) -> bool:
    """Sliding-window per-IP limiter. Kept in memory — resets on restart."""
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_LIMIT:
            _rate[ip] = hits
            return False
        hits.append(now)
        _rate[ip] = hits
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "MimicLeaderboard/1.0"
    protocol_version = "HTTP/1.1"

    # ---- response helpers ----
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Game-Key")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self) -> str:
        # Behind a reverse proxy (Caddy, cloud LB) the real IP is the first hop
        # of X-Forwarded-For; fall back to the socket peer for direct connections.
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    # ---- routing ----
    def do_OPTIONS(self) -> None:  # CORS preflight for the browser build
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"status": "ok", "service": "mimic-leaderboard", "version": 2})
        if path == "/":
            return self._json(200, {
                "service": "mimic-leaderboard",
                "version": 2,
                "endpoints": ["/health", "/leaderboard?sort=time|score&limit=N&name=PLAYER", "/submit (POST)"],
            })
        if path == "/leaderboard":
            return self._leaderboard(parse_qs(urlparse(self.path).query))
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/submit":
            return self._json(404, {"ok": False, "error": "not found"})
        return self._submit()

    # ---- endpoints ----
    def _leaderboard(self, q: dict) -> None:
        sort = q.get("sort", ["time"])[0].lower()
        try:
            limit = int(q.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 100))
        name = sanitize_name(q.get("name", [""])[0])  # optional per-player filter

        conn = db()
        try:
            if sort == "score":
                rows = conn.execute(
                    "SELECT name, score, time_ms, reached_goal, created_at "
                    "FROM scores" + (" WHERE name = ?" if name else "") +
                    " ORDER BY score DESC, time_ms ASC LIMIT ?",
                    (name, limit) if name else (limit,),
                ).fetchall()
            else:  # "time" board: only completed runs, fastest first
                sort = "time"
                rows = conn.execute(
                    "SELECT name, score, time_ms, reached_goal, created_at "
                    "FROM scores WHERE reached_goal=1" + (" AND name = ?" if name else "") +
                    " ORDER BY time_ms ASC LIMIT ?",
                    (name, limit) if name else (limit,),
                ).fetchall()
        finally:
            conn.close()

        entries = [{
            "rank": i,
            "name": r["name"],
            "score": r["score"],
            "time_ms": r["time_ms"],
            "reached_goal": bool(r["reached_goal"]),
            "created_at": r["created_at"],
        } for i, r in enumerate(rows, start=1)]
        return self._json(200, {"sort": sort, "count": len(entries), "entries": entries})

    def _submit(self) -> None:
        # Drain the request body BEFORE any early return. On a keep-alive
        # connection, unread body bytes get parsed as the next "request" and
        # answered with a 501/400 that carries no CORS headers — a reverse
        # proxy then hands that poisoned response to an unrelated browser
        # call, which surfaces as a mystery CORS error client-side.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not (0 < length <= 4096):
            self.close_connection = True  # body absent or too big to drain safely
            return self._json(400, {"ok": False, "error": "empty or oversized body"})
        raw = self.rfile.read(length)

        if GAME_KEY and self.headers.get("X-Game-Key", "") != GAME_KEY:
            return self._json(403, {"ok": False, "error": "bad or missing game key"})
        if not rate_ok(self._client_ip()):
            return self._json(429, {"ok": False, "error": "rate limited"})

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "invalid JSON"})
        if not isinstance(data, dict):
            return self._json(400, {"ok": False, "error": "expected a JSON object"})

        name = sanitize_name(data.get("name"))
        if name is None:
            return self._json(400, {"ok": False, "error": "invalid name"})
        try:
            score = int(data["score"])
            time_ms = int(data["time_ms"])
        except (KeyError, TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "score and time_ms must be integers"})
        reached_goal = 1 if data.get("reached_goal") else 0
        if not (0 <= score <= MAX_SCORE):
            return self._json(400, {"ok": False, "error": "score out of range"})
        if not (1 <= time_ms <= MAX_TIME_MS):
            return self._json(400, {"ok": False, "error": "time_ms out of range"})
        if reached_goal and time_ms < MIN_WIN_TIME_MS:
            return self._json(400, {"ok": False, "error": "win time below plausible minimum"})

        created = datetime.now(timezone.utc).isoformat()
        conn = db()
        try:
            new_id = conn.execute(
                "INSERT INTO scores (name, score, time_ms, reached_goal, created_at) VALUES (?,?,?,?,?)",
                (name, score, time_ms, reached_goal, created),
            ).lastrowid
            # Per-player retention: keep only the union of this name's top-N
            # scores and top-N fastest wins, so one player can't flood the
            # board (a just-submitted run below both cutoffs is pruned at once).
            conn.execute(
                """
                DELETE FROM scores WHERE name = ?
                  AND id NOT IN (SELECT id FROM scores WHERE name = ?
                                 ORDER BY score DESC, time_ms ASC LIMIT ?)
                  AND id NOT IN (SELECT id FROM scores WHERE name = ? AND reached_goal = 1
                                 ORDER BY time_ms ASC LIMIT ?)
                """,
                (name, name, KEEP_PER_PLAYER, name, KEEP_PER_PLAYER),
            )
            time_rank = None
            if reached_goal:
                time_rank = conn.execute(
                    "SELECT COUNT(*)+1 FROM scores WHERE reached_goal=1 AND time_ms < ?",
                    (time_ms,),
                ).fetchone()[0]
            score_rank = conn.execute(
                "SELECT COUNT(*)+1 FROM scores WHERE score > ?", (score,)
            ).fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return self._json(200, {"ok": True, "id": new_id, "time_rank": time_rank, "score_rank": score_rank})

    def log_message(self, fmt: str, *args) -> None:  # compact access log to stderr
        sys.stderr.write("%s %s\n" % (self._client_ip(), fmt % args))


def main() -> None:
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"mimic-leaderboard listening on :{PORT} (db: {DB_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
