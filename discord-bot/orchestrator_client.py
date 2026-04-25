from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

_API_PREFIX = "/api/v1"
_TIMEOUT = 10.0


class OrchestratorError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class OrchestratorClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/") + _API_PREFIX
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # Context manager                                                       #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> OrchestratorClient:
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=_TIMEOUT,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("OrchestratorClient must be used as a context manager")
        return self._client

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._http.get(
            path, params={k: v for k, v in params.items() if v is not None}
        )
        self._raise_for_status(resp)
        return resp.json()

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if actor_id:
            headers["X-Discord-User-Id"] = actor_id
        resp = await self._http.post(path, json=body, headers=headers or None)
        self._raise_for_status(resp)
        return resp.json()

    async def _put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = await self._http.put(path, json=body)
        self._raise_for_status(resp)
        return resp.json()

    async def _delete(self, path: str) -> None:
        resp = await self._http.delete(path)
        self._raise_for_status(resp)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            detail = resp.json().get("detail") or resp.json().get("title") or resp.text
        except Exception:
            detail = resp.text
        raise OrchestratorError(resp.status_code, detail)

    # ------------------------------------------------------------------ #
    # Public API                                                            #
    # ------------------------------------------------------------------ #

    async def list_hosts(self) -> list[dict]:
        return await self._get("/hosts")

    async def list_instances(self) -> list[dict]:
        return await self._get("/instances")

    async def get_instance(self, instance_id: str) -> dict:
        return await self._get(f"/instances/{instance_id}")

    async def get_instance_status(self, instance_id: str) -> dict:
        return await self._get(f"/instances/{instance_id}/status")

    async def list_missions(self, instance_id: str) -> list[str]:
        data = await self._get(f"/instances/{instance_id}/missions")
        return data.get("items", [])

    async def trigger_action(
        self,
        instance_id: str,
        action: str,
        body: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> dict:
        return await self._post(
            f"/instances/{instance_id}/actions/{action}", body=body, actor_id=actor_id
        )

    async def get_job(self, job_id: str) -> dict:
        return await self._get(f"/jobs/{job_id}")

    async def list_jobs(self, status: str | None = None) -> list[dict]:
        return await self._get("/jobs", status=status)

    async def create_invite(
        self, host_name: str = "", expires_in_hours: int | None = None
    ) -> dict:
        body: dict = {}
        if host_name:
            body["hostName"] = host_name
        if expires_in_hours is not None:
            body["expiresInHours"] = expires_in_hours
        return await self._post("/invites", body=body)

    async def list_invites(self) -> list[dict]:
        return await self._get("/invites")

    async def upload_mission(
        self, instance_id: str, filename: str, data: bytes
    ) -> dict:
        resp = await self._http.post(
            f"/instances/{instance_id}/upload",
            content=data,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
        self._raise_for_status(resp)
        return resp.json()

    async def delete_mission(self, instance_id: str, filename: str) -> None:
        await self._delete(f"/instances/{instance_id}/missions/{filename}")

    async def list_active_missions(self, host_id: str) -> list[dict]:
        data = await self._get(f"/hosts/{host_id}/missions")
        return data.get("items", [])

    async def upload_active_mission(
        self, host_id: str, filename: str, data: bytes
    ) -> dict:
        resp = await self._http.post(
            f"/hosts/{host_id}/missions/upload",
            content=data,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
        self._raise_for_status(resp)
        return resp.json()

    async def download_active_mission(self, host_id: str, filename: str) -> bytes:
        resp = await self._http.get(
            f"/hosts/{host_id}/missions/{filename}",
            timeout=60.0,
        )
        self._raise_for_status(resp)
        return resp.content

    async def delete_active_mission(self, host_id: str, filename: str) -> dict:
        resp = await self._http.delete(f"/hosts/{host_id}/missions/{filename}")
        self._raise_for_status(resp)
        return resp.json()

    async def copy_mission_to_active(self, instance_id: str, filename: str) -> dict:
        return await self._post(
            f"/instances/{instance_id}/missions/{filename}/copy-to-active"
        )

    async def remove_host(self, host_id: str) -> None:
        await self._delete(f"/hosts/{host_id}")

    async def reboot_host(self, host_id: str) -> dict:
        return await self._post(f"/hosts/{host_id}/reboot")

    async def trigger_dcs_update(self, host_id: str) -> dict:
        return await self._post(f"/hosts/{host_id}/update")

    async def get_update_status(self, host_id: str) -> dict:
        return await self._get(f"/hosts/{host_id}/update/status")

    async def get_instance_schedule(self, instance_id: str) -> dict:
        return await self._get(f"/instances/{instance_id}/schedule")

    async def set_instance_schedule(self, instance_id: str, schedule: dict) -> dict:
        return await self._put(f"/instances/{instance_id}/schedule", schedule)

    async def delete_instance_schedule(self, instance_id: str) -> None:
        await self._delete(f"/instances/{instance_id}/schedule")

    async def get_analytics_events(
        self,
        instance_id: str | None = None,
        since: str | None = None,
        limit: int = 2000,
    ) -> list[dict]:
        return await self._get(
            "/analytics/events", instance_id=instance_id, since=since, limit=limit
        )
