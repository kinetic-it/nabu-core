"""Tests for the HA-MCP media channel.

A local HTTP server impersonates the MCP endpoint, so the JSON-RPC envelope,
tool arguments and response parsing are verified without touching a real
Home Assistant.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nabu_core.mcp_ha import McpHaClient, McpHaError, McpHaMediaClient


class FakeMcp:
    def __init__(self):
        self.calls: list[dict] = []
        self.state = "idle"
        self.fail = False
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if recorder.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                method = payload.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "ha-mcp", "version": "test"},
                        "capabilities": {},
                    }
                elif method == "tools/call":
                    params = payload.get("params") or {}
                    recorder.calls.append(params)
                    if params.get("name") == "ha_get_state":
                        entity = params["arguments"]["entity_id"]
                        result = {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "data": {
                                                "success": True,
                                                "states": {entity: {"state": recorder.state}},
                                            }
                                        }
                                    ),
                                }
                            ]
                        }
                    else:
                        data = params["arguments"].get("data") or {}
                        if params["arguments"].get("service") == "play_media":
                            recorder.state = "playing"
                        elif params["arguments"].get("service") == "media_stop":
                            recorder.state = "idle"
                        result = {
                            "content": [{"type": "text", "text": json.dumps({"result": data})}]
                        }
                else:
                    result = {}
                body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})
                raw = body.encode("utf8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/secret-path"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def tool(self, name):
        return [call for call in self.calls if call.get("name") == name]


@pytest.fixture()
def fake_mcp():
    server = FakeMcp()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.asyncio
async def test_initialize_reports_server_name(fake_mcp):
    client = McpHaClient(fake_mcp.url)

    assert await client.initialize() == "ha-mcp"


@pytest.mark.asyncio
async def test_play_media_sends_home_assistant_arguments(fake_mcp):
    media = McpHaMediaClient(McpHaClient(fake_mcp.url))

    await media.play_media("media_player.echo", ("audio/wav", "media-source://local/x.wav"))

    call = fake_mcp.tool("ha_call_service")[0]["arguments"]
    assert call["domain"] == "media_player"
    assert call["service"] == "play_media"
    assert call["entity_id"] == "media_player.echo"
    assert call["data"] == {
        "media_content_id": "media-source://local/x.wav",
        "media_content_type": "audio/wav",
    }
    assert call["wait"] is False


@pytest.mark.asyncio
async def test_stop_uses_media_stop(fake_mcp):
    media = McpHaMediaClient(McpHaClient(fake_mcp.url))

    await media.stop("media_player.echo")

    call = fake_mcp.tool("ha_call_service")[0]["arguments"]
    assert call["service"] == "media_stop"


@pytest.mark.asyncio
async def test_get_state_reads_the_nested_state(fake_mcp):
    fake_mcp.state = "playing"
    media = McpHaMediaClient(McpHaClient(fake_mcp.url))

    assert await media.get_state("media_player.echo") == "playing"


@pytest.mark.asyncio
async def test_http_failure_raises_domain_error(fake_mcp):
    fake_mcp.fail = True
    media = McpHaMediaClient(McpHaClient(fake_mcp.url, timeout=2.0))

    with pytest.raises(McpHaError):
        await media.get_state("media_player.echo")


def test_constructor_rejects_relative_url():
    with pytest.raises(ValueError):
        McpHaClient("ha.example.invalid:9583/mcp")


@pytest.mark.asyncio
async def test_full_cycle_through_the_adapter(fake_mcp):
    from nabu_core.devices import DevicePolicy
    from nabu_core.player import MediaPlayerAdapter, PlaybackStatus

    media = McpHaMediaClient(McpHaClient(fake_mcp.url))
    adapter = MediaPlayerAdapter(
        media,
        DevicePolicy(allowed={"media_player.echo"}, denied={"media_player.kuchendot"}),
        media=("audio/wav", "media-source://local/x.wav"),
        max_seconds=0.3,
    )

    accepted = await adapter.start("media_player.echo")
    playing = await adapter.confirm_playing("media_player.echo", wait=1.0, interval=0.05)
    stopped = await adapter.stop("media_player.echo", wait=1.0, interval=0.05)

    assert accepted.status is PlaybackStatus.ACCEPTED
    assert playing.status is PlaybackStatus.PLAYING_CONFIRMED
    assert stopped.status is PlaybackStatus.STOPPED_CONFIRMED
