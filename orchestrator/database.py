"""
SQLite database layer for the orchestrator.

Manages hosts and instances tables via aiosqlite.
Attached to app.state.db at startup/shutdown.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import aiosqlite

_CREATE_HOSTS = """
CREATE TABLE IF NOT EXISTS hosts (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    agent_url     TEXT NOT NULL,
    agent_api_key TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '[]',
    notes         TEXT,
    is_enabled    INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);
"""

_CREATE_INSTANCES = """
CREATE TABLE IF NOT EXISTS instances (
    id           TEXT PRIMARY KEY,
    host_id      TEXT NOT NULL REFERENCES hosts(id),
    service_name TEXT NOT NULL,
    name         TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    UNIQUE(host_id, service_name)
);
"""

_CREATE_INVITE_CODES = """
CREATE TABLE IF NOT EXISTS invite_codes (
    id          TEXT PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    host_name   TEXT NOT NULL DEFAULT '',
    used        INTEGER NOT NULL DEFAULT 0,
    used_by     TEXT,
    used_at     TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT
);
"""

_CREATE_AUDIT_LOGS = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id          TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    actor       TEXT,           -- Discord user ID / username, or NULL for system actions
    action      TEXT NOT NULL,  -- e.g. "start", "stop", "mission_load"
    instance_id TEXT,
    host_id     TEXT,
    job_id      TEXT,
    status      TEXT NOT NULL,  -- "queued" | "succeeded" | "failed"
    detail      TEXT            -- JSON blob: error message or brief result summary
);
"""

_CREATE_ANALYTICS_EVENTS = """
CREATE TABLE IF NOT EXISTS analytics_events (
    id           TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    host_id      TEXT NOT NULL,
    instance_id  TEXT,           -- service_name of the DCS instance
    event_type   TEXT NOT NULL,  -- player_join | player_leave | mission_start | mission_end
    player_name  TEXT,           -- set for player_join / player_leave
    mission_name TEXT,           -- mission active at event time
    map          TEXT            -- theatre/map at event time
);
"""

# Migration: add frp_port to hosts if not present (safe on older DBs)
_MIGRATE_HOSTS_FRP = "ALTER TABLE hosts ADD COLUMN frp_port INTEGER"

_CREATE_BENCH_RUNS = """
CREATE TABLE IF NOT EXISTS bench_runs (
    id          TEXT PRIMARY KEY,
    host_id     TEXT NOT NULL,
    mission     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration_s  INTEGER,
    intended_duration_s INTEGER,
    bench_elapsed_s INTEGER,
    run_quality TEXT NOT NULL DEFAULT 'unknown',
    injection_status TEXT,
    hard_stop_error TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL
);
"""

_CREATE_BENCH_TIMESERIES = """
CREATE TABLE IF NOT EXISTS bench_timeseries (
    run_id    TEXT NOT NULL REFERENCES bench_runs(id),
    elapsed_s REAL NOT NULL,
    drift_s   REAL NOT NULL,
    groups    INTEGER NOT NULL,
    units     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_ts_run ON bench_timeseries(run_id);
"""

_CREATE_BENCH_CPU = """
CREATE TABLE IF NOT EXISTS bench_cpu (
    run_id    TEXT NOT NULL REFERENCES bench_runs(id),
    elapsed_s REAL NOT NULL,
    cpu_pct   REAL NOT NULL,
    mem_mb    REAL NOT NULL,
    threads   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_cpu_run ON bench_cpu(run_id);
"""

_CREATE_BENCH_FINDINGS = """
CREATE TABLE IF NOT EXISTS bench_findings (
    run_id   TEXT NOT NULL REFERENCES bench_runs(id),
    rule_id  TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bench_findings_run ON bench_findings(run_id);
"""

_CREATE_BENCH_LOG_ISSUES = """
CREATE TABLE IF NOT EXISTS bench_log_issues (
    run_id     TEXT NOT NULL REFERENCES bench_runs(id),
    issue_type TEXT NOT NULL,
    signature  TEXT NOT NULL,
    count      INTEGER NOT NULL,
    first_line TEXT,
    last_line  TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bench_log_issues_run ON bench_log_issues(run_id);
"""

_CREATE_BENCH_QUEUE = """
CREATE TABLE IF NOT EXISTS bench_queue (
    id           TEXT PRIMARY KEY,
    miz_filename TEXT NOT NULL,
    miz_data     BLOB NOT NULL,
    host_id      TEXT NOT NULL,
    instance_id  TEXT NOT NULL,
    duration_s   INTEGER NOT NULL DEFAULT 1800,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    run_id       TEXT,
    error        TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host_row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    d["is_enabled"] = bool(d["is_enabled"])
    return d


def _inst_row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    return d


def _queue_row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d.pop("miz_data", None)  # never expose raw bytes over API
    return d


class Database:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute(_CREATE_HOSTS)
        await self._conn.execute(_CREATE_INSTANCES)
        await self._conn.execute(_CREATE_INVITE_CODES)
        await self._conn.execute(_CREATE_AUDIT_LOGS)
        await self._conn.execute(_CREATE_ANALYTICS_EVENTS)
        await self._conn.execute(_CREATE_BENCH_RUNS)
        await self._conn.executescript(_CREATE_BENCH_TIMESERIES)
        await self._conn.executescript(_CREATE_BENCH_CPU)
        await self._conn.executescript(_CREATE_BENCH_FINDINGS)
        await self._conn.executescript(_CREATE_BENCH_LOG_ISSUES)
        await self._conn.execute(_CREATE_BENCH_QUEUE)
        # Safe migration: add frp_port column if missing
        try:
            await self._conn.execute(_MIGRATE_HOSTS_FRP)
        except Exception:
            pass  # column already exists
        for sql in (
            "ALTER TABLE bench_runs ADD COLUMN intended_duration_s INTEGER",
            "ALTER TABLE bench_runs ADD COLUMN bench_elapsed_s INTEGER",
            "ALTER TABLE bench_runs ADD COLUMN run_quality TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE bench_runs ADD COLUMN injection_status TEXT",
            "ALTER TABLE bench_runs ADD COLUMN hard_stop_error TEXT",
        ):
            try:
                await self._conn.execute(sql)
            except Exception:
                pass  # column already exists
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Hosts
    # ------------------------------------------------------------------

    async def create_host(
        self,
        name: str,
        agent_url: str,
        agent_api_key: str = "",
        tags: list[str] | None = None,
        notes: str | None = None,
        frp_port: int | None = None,
    ) -> dict[str, Any]:
        host_id = "host_" + secrets.token_hex(6)
        now = _now_iso()
        tags_json = json.dumps(tags or [])
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO hosts (id, name, agent_url, agent_api_key, tags, notes, is_enabled, created_at, frp_port)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (host_id, name, agent_url, agent_api_key, tags_json, notes, now, frp_port),
        )
        await self._conn.commit()
        row = await self._get_row("SELECT * FROM hosts WHERE id = ?", (host_id,))
        assert row is not None
        return _host_row_to_dict(row)

    async def list_hosts(self) -> list[dict[str, Any]]:
        assert self._conn
        async with self._conn.execute("SELECT * FROM hosts ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [_host_row_to_dict(r) for r in rows]

    async def get_host(self, host_id: str) -> dict[str, Any] | None:
        row = await self._get_row("SELECT * FROM hosts WHERE id = ?", (host_id,))
        return _host_row_to_dict(row) if row else None

    async def update_host(
        self, host_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {"name", "agent_url", "agent_api_key", "tags", "notes", "is_enabled"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_host(host_id)

        # Serialize tags if present
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "is_enabled" in updates:
            updates["is_enabled"] = int(bool(updates["is_enabled"]))

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [host_id]
        assert self._conn
        await self._conn.execute(f"UPDATE hosts SET {set_clause} WHERE id = ?", values)
        await self._conn.commit()
        return await self.get_host(host_id)

    async def delete_host(self, host_id: str) -> bool:
        assert self._conn
        await self._conn.execute("DELETE FROM instances WHERE host_id = ?", (host_id,))
        cur = await self._conn.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def touch_host(self, host_id: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE hosts SET last_seen_at = ? WHERE id = ?",
            (_now_iso(), host_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Instances
    # ------------------------------------------------------------------

    async def create_instance(
        self,
        host_id: str,
        service_name: str,
        name: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        inst_id = "inst_" + secrets.token_hex(6)
        now = _now_iso()
        tags_json = json.dumps(tags or [])
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO instances (id, host_id, service_name, name, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (inst_id, host_id, service_name, name, tags_json, now),
        )
        await self._conn.commit()
        row = await self._get_row("SELECT * FROM instances WHERE id = ?", (inst_id,))
        assert row is not None
        return _inst_row_to_dict(row)

    async def list_instances(self, host_id: str | None = None) -> list[dict[str, Any]]:
        assert self._conn
        if host_id:
            async with self._conn.execute(
                "SELECT * FROM instances WHERE host_id = ? ORDER BY created_at",
                (host_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._conn.execute(
                "SELECT * FROM instances ORDER BY created_at"
            ) as cur:
                rows = await cur.fetchall()
        return [_inst_row_to_dict(r) for r in rows]

    async def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        """Look up by DB id first, then fall back to service_name or name (case-insensitive)."""
        row = await self._get_row(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        )
        if row is None:
            row = await self._get_row(
                "SELECT * FROM instances WHERE lower(service_name) = lower(?) OR lower(name) = lower(?)",
                (instance_id, instance_id),
            )
        return _inst_row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Invite codes
    # ------------------------------------------------------------------

    async def create_invite(
        self,
        host_name: str = "",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a human-readable invite code."""
        import random

        charset = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I/l ambiguity
        code = "GOON-" + "-".join(
            "".join(random.choices(charset, k=4)) for _ in range(3)
        )
        inv_id = "inv_" + secrets.token_hex(6)
        now = _now_iso()
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO invite_codes (id, code, host_name, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (inv_id, code, host_name, now, expires_at),
        )
        await self._conn.commit()
        row = await self._get_row("SELECT * FROM invite_codes WHERE id = ?", (inv_id,))
        assert row is not None
        return dict(row)

    async def get_invite_by_code(self, code: str) -> dict[str, Any] | None:
        row = await self._get_row(
            "SELECT * FROM invite_codes WHERE code = ?", (code.upper().strip(),)
        )
        return dict(row) if row else None

    async def consume_invite(self, code: str, used_by_host_id: str) -> bool:
        """Mark an invite as used. Returns False if already used or not found."""
        inv = await self.get_invite_by_code(code)
        if not inv or inv["used"]:
            return False
        assert self._conn
        await self._conn.execute(
            "UPDATE invite_codes SET used = 1, used_by = ?, used_at = ? WHERE code = ?",
            (used_by_host_id, _now_iso(), code.upper().strip()),
        )
        await self._conn.commit()
        return True

    async def list_invites(self) -> list[dict[str, Any]]:
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # frp port allocation
    # ------------------------------------------------------------------

    async def get_next_frp_port(self, start: int = 8800, end: int = 8899) -> int:
        """Return the next available port in [start, end]."""
        assert self._conn
        async with self._conn.execute(
            "SELECT frp_port FROM hosts WHERE frp_port IS NOT NULL ORDER BY frp_port"
        ) as cur:
            used_ports = {row[0] for row in await cur.fetchall()}
        for port in range(start, end + 1):
            if port not in used_ports:
                return port
        raise RuntimeError(f"No available frp ports in range {start}-{end}")

    # ------------------------------------------------------------------
    # Audit logs
    # ------------------------------------------------------------------

    async def write_audit_log(
        self,
        action: str,
        status: str,
        actor: str | None = None,
        instance_id: str | None = None,
        host_id: str | None = None,
        job_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append an immutable audit record. Fire-and-forget — errors are swallowed."""
        log_id = "aud_" + secrets.token_hex(6)
        try:
            assert self._conn
            await self._conn.execute(
                """
                INSERT INTO audit_logs
                    (id, timestamp, actor, action, instance_id, host_id, job_id, status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    _now_iso(),
                    actor,
                    action,
                    instance_id,
                    host_id,
                    job_id,
                    status,
                    detail,
                ),
            )
            await self._conn.commit()
        except Exception:
            pass  # audit failures must never break the main request path

    async def list_audit_logs(
        self,
        instance_id: str | None = None,
        host_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        assert self._conn
        if instance_id:
            sql = "SELECT * FROM audit_logs WHERE instance_id = ? ORDER BY timestamp DESC LIMIT ?"
            params: tuple = (instance_id, limit)
        elif host_id:
            sql = "SELECT * FROM audit_logs WHERE host_id = ? ORDER BY timestamp DESC LIMIT ?"
            params = (host_id, limit)
        else:
            sql = "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?"
            params = (limit,)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    async def get_host_by_agent_key(
        self, host_id: str, agent_api_key: str
    ) -> dict[str, Any] | None:
        """Return host row if host_id + agent_api_key match, else None."""
        row = await self._get_row(
            "SELECT * FROM hosts WHERE id = ? AND agent_api_key = ?",
            (host_id, agent_api_key),
        )
        return _host_row_to_dict(row) if row else None

    async def write_analytics_event(
        self,
        host_id: str,
        event_type: str,
        instance_id: str | None = None,
        player_name: str | None = None,
        mission_name: str | None = None,
        map: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        event_id = "evt_" + secrets.token_hex(6)
        ts = timestamp or _now_iso()
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO analytics_events
                (id, timestamp, host_id, instance_id, event_type, player_name, mission_name, map)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                ts,
                host_id,
                instance_id,
                event_type,
                player_name,
                mission_name,
                map,
            ),
        )
        await self._conn.commit()

    async def list_analytics_events(
        self,
        host_id: str | None = None,
        instance_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        assert self._conn
        clauses: list[str] = []
        params: list[Any] = []
        if host_id:
            clauses.append("host_id = ?")
            params.append(host_id)
        if instance_id:
            clauses.append("instance_id = ?")
            params.append(instance_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM analytics_events {where} ORDER BY timestamp DESC LIMIT ?"
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Bench runs
    # ------------------------------------------------------------------

    async def create_bench_run(
        self,
        host_id: str,
        mission: str,
        started_at: str,
        ended_at: str | None = None,
        duration_s: int | None = None,
        intended_duration_s: int | None = None,
        bench_elapsed_s: int | None = None,
        run_quality: str = "unknown",
        injection_status: str | None = None,
        hard_stop_error: str | None = None,
        notes: str | None = None,
    ) -> str:
        run_id = "brun_" + secrets.token_hex(6)
        now = _now_iso()
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO bench_runs (
                id, host_id, mission, started_at, ended_at, duration_s,
                intended_duration_s, bench_elapsed_s, run_quality,
                injection_status, hard_stop_error, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                host_id,
                mission,
                started_at,
                ended_at,
                duration_s,
                intended_duration_s,
                bench_elapsed_s,
                run_quality,
                injection_status,
                hard_stop_error,
                notes,
                now,
            ),
        )
        await self._conn.commit()
        return run_id

    async def insert_bench_timeseries(
        self,
        run_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        assert self._conn
        await self._conn.executemany(
            "INSERT INTO bench_timeseries (run_id, elapsed_s, drift_s, groups, units) VALUES (?,?,?,?,?)",
            [
                (run_id, r["elapsed_s"], r["drift_s"], r["groups"], r["units"])
                for r in rows
            ],
        )
        await self._conn.commit()

    async def insert_bench_cpu(
        self,
        run_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        assert self._conn
        await self._conn.executemany(
            "INSERT INTO bench_cpu (run_id, elapsed_s, cpu_pct, mem_mb, threads) VALUES (?,?,?,?,?)",
            [
                (run_id, r["elapsed_s"], r["cpu_pct"], r["mem_mb"], r["threads"])
                for r in rows
            ],
        )
        await self._conn.commit()

    async def insert_bench_findings(
        self,
        run_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        assert self._conn
        await self._conn.executemany(
            "INSERT INTO bench_findings (run_id, rule_id, severity, detail) VALUES (?,?,?,?)",
            [(run_id, r["rule_id"], r["severity"], r.get("detail")) for r in rows],
        )
        await self._conn.commit()

    async def insert_bench_log_issues(
        self,
        run_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        assert self._conn
        await self._conn.executemany(
            """
            INSERT INTO bench_log_issues
                (run_id, issue_type, signature, count, first_line, last_line, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    r["issue_type"],
                    r["signature"],
                    r["count"],
                    r.get("first_line"),
                    r.get("last_line"),
                    r.get("detail"),
                )
                for r in rows
            ],
        )
        await self._conn.commit()

    async def list_bench_runs(
        self, host_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        assert self._conn
        if host_id:
            sql = "SELECT * FROM bench_runs WHERE host_id = ? ORDER BY created_at DESC LIMIT ?"
            params: tuple = (host_id, limit)
        else:
            sql = "SELECT * FROM bench_runs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_bench_run(self, run_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = await self._get_row("SELECT * FROM bench_runs WHERE id = ?", (run_id,))
        if not row:
            return None
        result = dict(row)
        async with self._conn.execute(
            "SELECT elapsed_s, drift_s, groups, units FROM bench_timeseries WHERE run_id = ? ORDER BY elapsed_s",
            (run_id,),
        ) as cur:
            result["bench_timeseries"] = [dict(r) for r in await cur.fetchall()]
        async with self._conn.execute(
            "SELECT elapsed_s, cpu_pct, mem_mb, threads FROM bench_cpu WHERE run_id = ? ORDER BY elapsed_s",
            (run_id,),
        ) as cur:
            result["cpu_timeseries"] = [dict(r) for r in await cur.fetchall()]
        async with self._conn.execute(
            "SELECT rule_id, severity, detail FROM bench_findings WHERE run_id = ?",
            (run_id,),
        ) as cur:
            result["findings"] = [dict(r) for r in await cur.fetchall()]
        async with self._conn.execute(
            """
            SELECT issue_type, signature, count, first_line, last_line, detail
            FROM bench_log_issues
            WHERE run_id = ?
            ORDER BY issue_type, count DESC
            """,
            (run_id,),
        ) as cur:
            result["log_issues"] = [dict(r) for r in await cur.fetchall()]
        return result

    # ------------------------------------------------------------------
    # Bench queue
    # ------------------------------------------------------------------

    async def enqueue_bench(
        self,
        miz_filename: str,
        miz_data: bytes,
        host_id: str,
        instance_id: str,
        duration_s: int = 1800,
    ) -> dict[str, Any]:
        item_id = "bq_" + secrets.token_hex(6)
        now = _now_iso()
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO bench_queue
                (id, miz_filename, miz_data, host_id, instance_id, duration_s, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (item_id, miz_filename, miz_data, host_id, instance_id, duration_s, now),
        )
        await self._conn.commit()
        row = await self._get_row("SELECT * FROM bench_queue WHERE id = ?", (item_id,))
        assert row is not None
        return _queue_row_to_dict(row)

    async def list_queue(self) -> list[dict[str, Any]]:
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM bench_queue ORDER BY created_at ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [_queue_row_to_dict(r) for r in rows]

    async def get_queue_item(self, item_id: str) -> dict[str, Any] | None:
        row = await self._get_row("SELECT * FROM bench_queue WHERE id = ?", (item_id,))
        return _queue_row_to_dict(row) if row else None

    async def delete_queue_item(self, item_id: str) -> bool:
        assert self._conn
        cur = await self._conn.execute(
            "DELETE FROM bench_queue WHERE id = ? AND status = 'pending'", (item_id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def dequeue_next_pending(self) -> dict[str, Any] | None:
        """Return the oldest pending item without changing its status."""
        row = await self._get_row(
            "SELECT * FROM bench_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1",
            (),
        )
        return _queue_row_to_dict(row) if row else None

    async def update_queue_status(
        self,
        item_id: str,
        status: str,
        started_at: str | None = None,
        completed_at: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        assert self._conn
        fields: dict[str, Any] = {"status": status}
        if started_at is not None:
            fields["started_at"] = started_at
        if completed_at is not None:
            fields["completed_at"] = completed_at
        if run_id is not None:
            fields["run_id"] = run_id
        if error is not None:
            fields["error"] = error
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [item_id]
        await self._conn.execute(
            f"UPDATE bench_queue SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_row(self, sql: str, params: tuple) -> aiosqlite.Row | None:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def probe(self) -> bool:
        """Return True if the DB is reachable (used by /health)."""
        try:
            assert self._conn
            async with self._conn.execute("SELECT 1") as cur:
                await cur.fetchone()
            return True
        except Exception:
            return False
