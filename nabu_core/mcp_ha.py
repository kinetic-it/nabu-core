"""Home Assistant access through the existing HA-MCP endpoint.

The runtime already holds a working, authorised Home Assistant connection: the
``ha-mcp`` server behind ``HA_MCP_URL``. This module reuses exactly that
channel instead of asking for a second credential (a long lived token), which
is why the alarm engine can address a real speaker without any REST token.

Only the standard library is used. The endpoint URL contains a secret, so it is
read from the environment by the caller and never logged here.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


class McpHaError(RuntimeError):
    """The Home Assistant MCP endpoint refused the call or was unreachable."""


class McpHaClient:
    """Minimal JSON-RPC client for the HA-MCP streamable-HTTP endpoint."""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("endpoint must be an absolute http(s) URL")
        self._url = url
        self._timeout = timeout
        self._session: str | None = None
        self._next_id = 0
        self._initialized = False

    # ------------------------------------------------------------- internals
    def _post(self, payload: dict) -> tuple[dict, dict]:
        request = urllib.request.Request(  # noqa: S310 - scheme validated in __init__
            self._url,
            data=json.dumps(payload).encode("utf8"),
            method="POST",  # noqa: S310 - scheme validated in __init__
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        if self._session:
            request.add_header("Mcp-Session-Id", self._session)
        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme checked in __init__
                request, timeout=self._timeout
            ) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                body = response.read().decode("utf8", "replace")
        except urllib.error.HTTPError as error:
            raise McpHaError(f"HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise McpHaError(f"unreachable: {error.reason}") from error
        session = headers.get("mcp-session-id")
        if session:
            self._session = session
        return self._parse(body), headers

    @staticmethod
    def _parse(body: str) -> dict:
        text = body.strip()
        if not text:
            return {}
        if text.startswith("{") or text.startswith("["):
            return json.loads(text)
        # streamable HTTP may answer with server-sent events
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise McpHaError("unrecognised response body")

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        answer, _ = self._post(payload)
        if "error" in answer:
            raise McpHaError(str(answer["error"])[:200])
        return answer.get("result") or {}

    async def initialize(self) -> str:
        if self._initialized:
            return "ready"
        result = await asyncio.to_thread(
            self._rpc,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nabu-core", "version": "0.1"},
            },
        )
        self._initialized = True
        info = result.get("serverInfo") or {}
        return str(info.get("name") or "ha-mcp")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        await self.initialize()
        result = await asyncio.to_thread(
            self._rpc, "tools/call", {"name": name, "arguments": arguments}
        )
        if result.get("isError"):
            raise McpHaError(f"tool {name} failed")
        blocks = result.get("content") or []
        for block in blocks:
            if block.get("type") == "text":
                try:
                    return json.loads(block.get("text") or "{}")
                except json.JSONDecodeError:
                    return {"text": block.get("text")}
        return {}


class McpHaMediaClient:
    """Media client with the same surface as the REST client."""

    def __init__(self, client: McpHaClient) -> None:
        self._client = client

    async def play_media(self, entity_id: str, media: tuple[str, str]) -> None:
        media_type, media_id = media
        await self._client.call_tool(
            "ha_call_service",
            {
                "domain": "media_player",
                "service": "play_media",
                "entity_id": entity_id,
                "data": {"media_content_id": media_id, "media_content_type": media_type},
                "wait": False,
            },
        )

    async def stop(self, entity_id: str) -> None:
        await self._client.call_tool(
            "ha_call_service",
            {
                "domain": "media_player",
                "service": "media_stop",
                "entity_id": entity_id,
                "wait": False,
            },
        )

    async def get_state(self, entity_id: str) -> str:
        payload = await self._client.call_tool(
            "ha_get_state", {"entity_id": entity_id, "fields": ["state"]}
        )
        try:
            return str(payload["data"]["states"][entity_id]["state"])
        except (KeyError, TypeError):
            single = payload.get("data") or {}
            if isinstance(single, dict) and "state" in single:
                return str(single["state"])
            raise McpHaError("state response had no state field") from None

    async def ping(self) -> str:
        return await self._client.initialize()


def endpoint_from_env() -> str:
    """Read the configured endpoint without exposing it in logs."""
    url = os.environ.get("HA_MCP_URL", "").strip()
    if not url:
        raise McpHaError("HA_MCP_URL is not configured")
    return url
