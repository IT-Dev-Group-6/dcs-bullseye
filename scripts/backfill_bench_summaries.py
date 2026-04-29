#!/usr/bin/env python3
"""Backfill bench run summary rows in the orchestrator SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from math import ceil, floor
from typing import Any

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

_CREATE_BENCH_RUN_SUMMARIES = """
CREATE TABLE IF NOT EXISTS bench_run_summaries (
    run_id             TEXT PRIMARY KEY REFERENCES bench_runs(id),
    avg_cpu_pct        REAL,
    p95_cpu_pct        REAL,
    max_cpu_pct        REAL,
    avg_mem_mb         REAL,
    max_mem_mb         REAL,
    avg_drift_s        REAL,
    p95_drift_s        REAL,
    max_drift_s        REAL,
    avg_units          REAL,
    max_units          INTEGER,
    avg_groups         REAL,
    max_groups         INTEGER,
    sample_count       INTEGER,
    bench_sample_count INTEGER,
    cpu_sample_count   INTEGER,
    findings_count     INTEGER,
    log_issue_count    INTEGER,
    validity_status    TEXT NOT NULL DEFAULT 'unknown',
    performance_score  REAL,
    computed_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_run_summaries_validity ON bench_run_summaries(validity_status);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    rank = (percentile / 100.0) * (len(data) - 1)
    low = floor(rank)
    high = ceil(rank)
    if low == high:
        return data[low]
    weight = rank - low
    return data[low] * (1 - weight) + data[high] * weight


def _validity_status(run: dict[str, Any], bench_count: int, cpu_count: int) -> str:
    if run.get("hard_stop_error"):
        return "mission_script_hard_stop"
    quality = run.get("run_quality") or "unknown"
    if quality in {
        "valid",
        "partial",
        "mission_script_hard_stop",
        "no_bench_data",
        "agent_error",
        "manual_excluded",
    }:
        return quality
    if quality == "ok" and bench_count > 0 and cpu_count > 0:
        return "valid"
    if quality == "ok":
        return "partial"
    if quality == "partial_bench_rows":
        return "partial"
    if quality == "no_bench_rows":
        return "no_bench_data"
    return "unknown"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "\n".join(
            [
                _CREATE_BENCH_RUNS,
                _CREATE_BENCH_TIMESERIES,
                _CREATE_BENCH_CPU,
                _CREATE_BENCH_FINDINGS,
                _CREATE_BENCH_LOG_ISSUES,
                _CREATE_BENCH_RUN_SUMMARIES,
            ]
        )
    )
    conn.commit()


def _list_runs(conn: sqlite3.Connection, force: bool) -> list[dict[str, Any]]:
    if force:
        sql = """
            SELECT r.*
            FROM bench_runs r
            ORDER BY r.created_at ASC, r.id ASC
        """
    else:
        sql = """
            SELECT r.*
            FROM bench_runs r
            LEFT JOIN bench_run_summaries s ON s.run_id = r.id
            WHERE s.run_id IS NULL
            ORDER BY r.created_at ASC, r.id ASC
        """
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql).fetchall()]


def _get_rows(conn: sqlite3.Connection, sql: str, run_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, (run_id,)).fetchall()


def _compute_summary(conn: sqlite3.Connection, run: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    bench_rows = _get_rows(
        conn,
        "SELECT drift_s, groups, units FROM bench_timeseries WHERE run_id = ? ORDER BY elapsed_s",
        run_id,
    )
    cpu_rows = _get_rows(
        conn,
        "SELECT cpu_pct, mem_mb FROM bench_cpu WHERE run_id = ? ORDER BY elapsed_s",
        run_id,
    )
    findings = _get_rows(
        conn,
        "SELECT 1 FROM bench_findings WHERE run_id = ?",
        run_id,
    )
    log_issues = _get_rows(
        conn,
        "SELECT 1 FROM bench_log_issues WHERE run_id = ?",
        run_id,
    )

    bench_drift = [float(r["drift_s"]) for r in bench_rows]
    bench_units = [int(r["units"]) for r in bench_rows]
    bench_groups = [int(r["groups"]) for r in bench_rows]
    cpu_pct = [float(r["cpu_pct"]) for r in cpu_rows]
    mem_mb = [float(r["mem_mb"]) for r in cpu_rows]

    return {
        "run_id": run_id,
        "avg_cpu_pct": round(sum(cpu_pct) / len(cpu_pct), 3) if cpu_pct else None,
        "p95_cpu_pct": _percentile(cpu_pct, 95.0),
        "max_cpu_pct": max(cpu_pct) if cpu_pct else None,
        "avg_mem_mb": round(sum(mem_mb) / len(mem_mb), 3) if mem_mb else None,
        "max_mem_mb": max(mem_mb) if mem_mb else None,
        "avg_drift_s": round(sum(bench_drift) / len(bench_drift), 6)
        if bench_drift
        else None,
        "p95_drift_s": _percentile(bench_drift, 95.0),
        "max_drift_s": max(bench_drift) if bench_drift else None,
        "avg_units": round(sum(bench_units) / len(bench_units), 3)
        if bench_units
        else None,
        "max_units": max(bench_units) if bench_units else None,
        "avg_groups": round(sum(bench_groups) / len(bench_groups), 3)
        if bench_groups
        else None,
        "max_groups": max(bench_groups) if bench_groups else None,
        "sample_count": len(bench_rows) + len(cpu_rows),
        "bench_sample_count": len(bench_rows),
        "cpu_sample_count": len(cpu_rows),
        "findings_count": len(findings),
        "log_issue_count": len(log_issues),
        "validity_status": _validity_status(run, len(bench_rows), len(cpu_rows)),
        "performance_score": None,
        "computed_at": _now_iso(),
    }


def _upsert_summary(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO bench_run_summaries (
            run_id, avg_cpu_pct, p95_cpu_pct, max_cpu_pct,
            avg_mem_mb, max_mem_mb,
            avg_drift_s, p95_drift_s, max_drift_s,
            avg_units, max_units,
            avg_groups, max_groups,
            sample_count, bench_sample_count, cpu_sample_count,
            findings_count, log_issue_count,
            validity_status, performance_score, computed_at
        )
        VALUES (
            :run_id, :avg_cpu_pct, :p95_cpu_pct, :max_cpu_pct,
            :avg_mem_mb, :max_mem_mb,
            :avg_drift_s, :p95_drift_s, :max_drift_s,
            :avg_units, :max_units,
            :avg_groups, :max_groups,
            :sample_count, :bench_sample_count, :cpu_sample_count,
            :findings_count, :log_issue_count,
            :validity_status, :performance_score, :computed_at
        )
        ON CONFLICT(run_id) DO UPDATE SET
            avg_cpu_pct=excluded.avg_cpu_pct,
            p95_cpu_pct=excluded.p95_cpu_pct,
            max_cpu_pct=excluded.max_cpu_pct,
            avg_mem_mb=excluded.avg_mem_mb,
            max_mem_mb=excluded.max_mem_mb,
            avg_drift_s=excluded.avg_drift_s,
            p95_drift_s=excluded.p95_drift_s,
            max_drift_s=excluded.max_drift_s,
            avg_units=excluded.avg_units,
            max_units=excluded.max_units,
            avg_groups=excluded.avg_groups,
            max_groups=excluded.max_groups,
            sample_count=excluded.sample_count,
            bench_sample_count=excluded.bench_sample_count,
            cpu_sample_count=excluded.cpu_sample_count,
            findings_count=excluded.findings_count,
            log_issue_count=excluded.log_issue_count,
            validity_status=excluded.validity_status,
            performance_score=excluded.performance_score,
            computed_at=excluded.computed_at
        """,
        summary,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill bench_run_summaries rows in the orchestrator database."
    )
    parser.add_argument(
        "--db-path",
        default="/var/lib/dcs-platform/orchestrator.db",
        help="Path to the orchestrator SQLite database.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Recompute summaries for every bench run, not just missing rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute summaries and report counts without writing changes.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    conn = sqlite3.connect(args.db_path)
    try:
        _ensure_schema(conn)
        runs = _list_runs(conn, force=args.all)
        if not runs:
            print("No runs needed backfill.")
            return 0

        wrote = 0
        for run in runs:
            summary = _compute_summary(conn, run)
            if not args.dry_run:
                _upsert_summary(conn, summary)
            wrote += 1

        if not args.dry_run:
            conn.commit()

        mode = "recomputed" if args.all else "backfilled"
        action = "would update" if args.dry_run else "updated"
        print(f"{action} {wrote} bench run summaries ({mode}).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
