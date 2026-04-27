"""
Async HTTP client for communicating with a DCS agent node.

Use as a context manager per background task:

    async with AgentClient(host.agent_url, host.agent_api_key) as client:
        data = await client.list_instances()

base_url should be the agent's /agent/v1 prefix,
e.g. "http://100.72.50.122:8787/agent/v1".
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx


class AgentError(Exception):
    """Raised when the agent returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AgentClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        # Normalise: strip trailing slash
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> AgentClient:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        self._client = httpx.AsyncClient(headers=headers)
        return self

    def _full_path(self, path: str) -> str:
        """Return the full URL path (base path + relative path) for signing.

        e.g. base_url="http://localhost:8800/agent/v1", path="/instances/DCS-X/actions/start"
        → "/agent/v1/instances/DCS-X/actions/start"

        This must match request.url.path as seen by the agent's auth dependency.
        """
        base_path = urlparse(self._base_url).path.rstrip("/")
        return f"{base_path}/{path.lstrip('/')}"

    def _sign_headers(self, method: str, path: str) -> dict[str, str]:
        """Return X-Timestamp / X-Nonce / X-Signature headers for a request.

        Skipped (returns empty dict) when api_key is empty (dev mode).
        The signature covers method + full_path + timestamp + nonce, which prevents
        replaying captured requests even if the transport is not encrypted.
        """
        if not self._api_key:
            return {}
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        full_path = self._full_path(path)
        msg = f"{method.upper()}\n{full_path}\n{timestamp}\n{nonce}"
        sig = hmac.new(self._api_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": sig,
        }

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _get(self, path: str, timeout: float = 10.0) -> Any:
        assert self._client, "AgentClient must be used as a context manager"
        resp = await self._client.get(
            self._url(path), timeout=timeout, headers=self._sign_headers("GET", path)
        )
        if not resp.is_success:
            detail = resp.text[:200]
            raise AgentError(resp.status_code, detail)
        return resp.json()

    async def _post(
        self, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        assert self._client, "AgentClient must be used as a context manager"
        resp = await self._client.post(
            self._url(path),
            json=body,
            timeout=timeout,
            headers=self._sign_headers("POST", path),
        )
        if not resp.is_success:
            detail = resp.text[:200]
            raise AgentError(resp.status_code, detail)
        return resp.json()

    async def _put(
        self, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        assert self._client, "AgentClient must be used as a context manager"
        resp = await self._client.put(
            self._url(path),
            json=body,
            timeout=timeout,
            headers=self._sign_headers("PUT", path),
        )
        if not resp.is_success:
            detail = resp.text[:200]
            raise AgentError(resp.status_code, detail)
        return resp.json()

    async def _delete(self, path: str, timeout: float = 10.0) -> Any:
        assert self._client, "AgentClient must be used as a context manager"
        resp = await self._client.delete(
            self._url(path), timeout=timeout, headers=self._sign_headers("DELETE", path)
        )
        if not resp.is_success:
            detail = resp.text[:200]
            raise AgentError(resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # Agent endpoints
    # ------------------------------------------------------------------

    async def get_health(self) -> dict[str, Any]:
        """GET <agent_base_url>/../../health — note: health is at root, not under /agent/v1."""
        # Agent health is at /health (no /agent/v1 prefix).
        # Derive the agent root from the base_url.
        # e.g. http://host:8787/agent/v1 → http://host:8787/health
        root = self._base_url.replace("/agent/v1", "")
        assert self._client, "AgentClient must be used as a context manager"
        resp = await self._client.get(f"{root}/health", timeout=5.0)
        if not resp.is_success:
            raise AgentError(resp.status_code, resp.text[:200])
        return resp.json()

    async def list_instances(self) -> list[dict[str, Any]]:
        """GET /agent/v1/instances"""
        return await self._get("/instances")

    async def get_instance_status(self, service_name: str) -> dict[str, Any]:
        """GET /agent/v1/instances/{serviceName}/status"""
        return await self._get(f"/instances/{service_name}/status")

    async def trigger_action(
        self, service_name: str, action: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST /agent/v1/instances/{serviceName}/actions/{action}"""
        return await self._post(
            f"/instances/{service_name}/actions/{action}", body=body
        )

    async def list_missions(self, service_name: str) -> list[str]:
        """GET /agent/v1/instances/{serviceName}/missions"""
        data = await self._get(f"/instances/{service_name}/missions")
        return data.get("items", [])

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """GET /agent/v1/jobs/{jobId}"""
        return await self._get(f"/jobs/{job_id}")

    async def upload_mission(
        self, service_name: str, filename: str, data: bytes, timeout: float = 60.0
    ) -> dict[str, Any]:
        """POST /agent/v1/instances/{serviceName}/upload — upload a .miz file."""
        assert self._client, "AgentClient must be used as a context manager"
        path = f"/instances/{service_name}/upload"
        resp = await self._client.post(
            self._url(path),
            content=data,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **self._sign_headers("POST", path),
            },
            timeout=timeout,
        )
        if not resp.is_success:
            raise AgentError(resp.status_code, resp.text[:200])
        return resp.json()

    async def delete_mission(self, service_name: str, filename: str) -> None:
        """DELETE /agent/v1/instances/{serviceName}/missions/{filename}"""
        await self._delete(f"/instances/{service_name}/missions/{filename}")

    async def copy_mission_to_active(
        self, service_name: str, filename: str
    ) -> dict[str, Any]:
        """POST /agent/v1/instances/{serviceName}/missions/{filename}/copy-to-active"""
        return await self._post(
            f"/instances/{service_name}/missions/{filename}/copy-to-active"
        )

    async def list_active_missions(self) -> list[dict[str, Any]]:
        """GET /agent/v1/missions — list the shared Active Missions folder (root only)."""
        data = await self._get("/missions")
        return data.get("items", [])

    async def upload_active_mission(
        self, filename: str, data: bytes, timeout: float = 60.0
    ) -> dict[str, Any]:
        """POST /agent/v1/missions/upload — upload a .miz to active_missions_dir root."""
        assert self._client, "AgentClient must be used as a context manager"
        path = "/missions/upload"
        resp = await self._client.post(
            self._url(path),
            content=data,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **self._sign_headers("POST", path),
            },
            timeout=timeout,
        )
        if not resp.is_success:
            raise AgentError(resp.status_code, resp.text[:200])
        return resp.json()

    async def download_active_mission(
        self, filename: str, timeout: float = 60.0
    ) -> bytes:
        """GET /agent/v1/missions/{filename} — download a .miz file as raw bytes."""
        assert self._client, "AgentClient must be used as a context manager"
        path = f"/missions/{filename}"
        resp = await self._client.get(
            self._url(path), timeout=timeout, headers=self._sign_headers("GET", path)
        )
        if not resp.is_success:
            raise AgentError(resp.status_code, resp.text[:200])
        return resp.content

    async def delete_active_mission(self, filename: str) -> dict[str, Any]:
        """DELETE /agent/v1/missions/{filename} — move to Backup_Missions/ with backup."""
        result = await self._delete(f"/missions/{filename}")
        return result or {}

    async def reboot_host(self) -> dict[str, Any]:
        """POST /agent/v1/host/reboot — schedule a Windows reboot."""
        return await self._post("/host/reboot")

    async def trigger_dcs_update(self) -> dict[str, Any]:
        """POST /agent/v1/host/update — start the DCS update process."""
        return await self._post("/host/update")

    async def get_update_status(self) -> dict[str, Any]:
        """GET /agent/v1/host/update/status — current update progress."""
        return await self._get("/host/update/status")

    async def get_instance_schedule(self, service_name: str) -> dict[str, Any]:
        """GET /agent/v1/instances/{serviceId}/schedule."""
        return await self._get(f"/instances/{service_name}/schedule")

    async def set_instance_schedule(
        self, service_name: str, schedule: dict
    ) -> dict[str, Any]:
        """PUT /agent/v1/instances/{serviceId}/schedule."""
        return await self._put(f"/instances/{service_name}/schedule", schedule)

    async def delete_instance_schedule(self, service_name: str) -> None:
        """DELETE /agent/v1/instances/{serviceId}/schedule."""
        await self._delete(f"/instances/{service_name}/schedule")

    async def bench_inject(self, mission: str, timeout: float = 60.0) -> dict[str, Any]:
        """POST /agent/v1/bench/inject — inject gm_bench.lua into the .miz in-place."""
        return await self._post(
            "/bench/inject",
            body={"mission": mission},
            timeout=timeout,
        )

    async def bench_monitor_start(self, service_name: str) -> dict[str, Any]:
        """POST /agent/v1/bench/monitor/start — start bench_monitor.py for an instance."""
        return await self._post(
            "/bench/monitor/start", body={"service_name": service_name}
        )

    async def bench_monitor_stop(self) -> dict[str, Any]:
        """POST /agent/v1/bench/monitor/stop — stop the running bench monitor."""
        return await self._post("/bench/monitor/stop")

    async def bench_collect(
        self,
        mission: str,
        service_name: str,
        timeout: float = 120.0,
        intended_duration_s: int | None = None,
        injection_status: str | None = None,
    ) -> dict[str, Any]:
        """POST /agent/v1/bench/collect — run afterburner record+push and return run_id."""
        body: dict[str, Any] = {"mission": mission, "service_name": service_name}
        if intended_duration_s is not None:
            body["intended_duration_s"] = intended_duration_s
        if injection_status:
            body["injection_status"] = injection_status
        return await self._post(
            "/bench/collect",
            body=body,
            timeout=timeout,
        )
