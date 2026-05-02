from fastapi.testclient import TestClient

from orchestrator.database import _compute_performance_score

from .conftest import HEADERS


def test_compute_performance_score_applies_deductions_and_caps() -> None:
    summary = {
        "validity_status": "valid",
        "p95_drift_s": 3.0,
        "p95_cpu_pct": 76.0,
        "log_issue_count": 20,
    }

    assert (
        _compute_performance_score(summary, critical_findings=2, warning_findings=10)
        == 5.0
    )


def test_compute_performance_score_skips_invalid_runs() -> None:
    summary = {
        "validity_status": "partial",
        "p95_drift_s": 0.1,
        "p95_cpu_pct": 10.0,
        "log_issue_count": 0,
    }

    assert (
        _compute_performance_score(summary, critical_findings=0, warning_findings=0)
        is None
    )


def test_ingest_bench_run_populates_summary(client: TestClient, host_id: str) -> None:
    payload = {
        "mission": "test.miz",
        "started_at": "2026-04-28T10:00:00Z",
        "ended_at": "2026-04-28T10:30:00Z",
        "duration_s": 1800,
        "intended_duration_s": 1800,
        "bench_elapsed_s": 1800,
        "run_quality": "ok",
        "injection_status": "injected",
        "bench_timeseries": [
            {"elapsed_s": 60.0, "drift_s": 0.1, "groups": 5, "units": 20},
            {"elapsed_s": 120.0, "drift_s": 0.2, "groups": 6, "units": 22},
        ],
        "cpu_timeseries": [
            {"elapsed_s": 60.0, "cpu_pct": 40.0, "mem_mb": 512.0, "threads": 32},
            {"elapsed_s": 120.0, "cpu_pct": 44.0, "mem_mb": 540.0, "threads": 32},
        ],
        "findings": [{"rule_id": "ctld_poll", "severity": "info", "detail": "sample"}],
        "log_issues": [
            {
                "issue_type": "warning",
                "signature": "sig",
                "count": 1,
                "first_line": "line 1",
                "last_line": "line 1",
                "detail": "sample",
            }
        ],
    }

    resp = client.post(
        "/api/v1/bench/runs",
        headers={"X-Host-Id": host_id, "X-Agent-Key": "agent-key"},
        json=payload,
    )
    assert resp.status_code == 201
    run_id = resp.json()["id"]

    list_resp = client.get("/api/v1/bench/runs", headers=HEADERS)
    assert list_resp.status_code == 200
    runs = list_resp.json()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["summary"]["validity_status"] == "valid"
    assert runs[0]["summary"]["bench_sample_count"] == 2
    assert runs[0]["summary"]["cpu_sample_count"] == 2
    assert runs[0]["summary"]["findings_count"] == 1
    assert runs[0]["summary"]["log_issue_count"] == 1
    assert runs[0]["summary"]["performance_score"] == 88.0

    detail_resp = client.get(f"/api/v1/bench/runs/{run_id}", headers=HEADERS)
    assert detail_resp.status_code == 200
    run = detail_resp.json()
    assert run["summary"]["validity_status"] == "valid"
    assert run["summary"]["performance_score"] == 88.0
    assert len(run["bench_timeseries"]) == 2
    assert len(run["cpu_timeseries"]) == 2
