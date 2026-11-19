"""Media client for a *remote* Home Assistant over its REST API.

Why this exists: during hardware acceptance the alarm engine runs in the
laboratory while the real speaker is attached to the production Home Assistant.
Installing the integration there would need a Home Assistant restart, so the
engine talks to the remote instance instead. The device allowlist stays in
front of every call, and the token is never logged.

Only the standard library is used (``urllib`` in a worker thread), so the
package stays dependency free.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request


class RemoteHaError(RuntimeError):
    """The remote Home Assistant refused the call or was unreachable."""


class RemoteHaMediaClient:
    """Minimal remote media client: play, stop and read one entity state."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        verify_tls: bool = True,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an absolute http(s) URL")
        if not token or not token.strip():
            raise ValueError("a token is required")
        self._base = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout
        self._verify_tls = verify_tls

    # ------------------------------------------------------------- internals
    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
        url = f"{self._base}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf8")
        # Only http(s) reaches this line; the scheme is checked in __init__.
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Content-Type", "application/json")
        context = None
        if url.startswith("https://") and not self._verify_tls:
            import ssl

            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            # The scheme is validated in the constructor, so only http(s) is possible.
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self._timeout, context=context
            ) as response:
                return response.status, response.read().decode("utf8", "replace")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf8", "replace")[:400]
            raise RemoteHaError(f"HTTP {error.code}: {body}") from error
        except urllib.error.URLError as error:
            raise RemoteHaError(f"unreachable: {error.reason}") from error

    async def _call(self, method: str, path: str, payload: dict | None = None) -> str:
        status, body = await asyncio.to_thread(self._request, method, path, payload)
        if status >= 400:
            raise RemoteHaError(f"HTTP {status}")
        return body

    # ---------------------------------------------------------------- client
    async def play_media(self, entity_id: str, media: tuple[str, str]) -> None:
        media_type, media_id = media
        await self._call(
            "POST",
            "/api/services/media_player/play_media",
            {
                "entity_id": entity_id,
                "media_content_id": media_id,
                "media_content_type": media_type,
            },
        )

    async def stop(self, entity_id: str) -> None:
        await self._call("POST", "/api/services/media_player/media_stop", {"entity_id": entity_id})

    async def get_state(self, entity_id: str) -> str:
        body = await self._call("GET", f"/api/states/{entity_id}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise RemoteHaError("state response was not JSON") from error
        if "state" not in payload:
            raise RemoteHaError("state response had no state field")
        return str(payload["state"])

    async def ping(self) -> str:
        """Cheap liveness check used before a hardware run."""
        return await self.api_status()

    async def api_status(self) -> str:
        body = await self._call("GET", "/api/")
        return body.strip() or "{}"
