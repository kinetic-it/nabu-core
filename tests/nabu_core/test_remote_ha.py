"""Tests for the remote Home Assistant media client.

A local HTTP server stands in for Home Assistant, so the real request path,
authorisation header, payload shape and error handling are exercised without a
network or a live instance.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nabu_core.remote_ha import RemoteHaError, RemoteHaMediaClient

TOKEN = "test-token-1234567890"  # noqa: S105 - synthetic test credential


class FakeHomeAssistant:
    def __init__(self):
        self.calls: list[dict] = []
        self.state = "idle"
        self.fail_status = None
        self.seen_auth: list[str] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the test output
                return

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw or b"{}")

            def _send(self, status, payload):
                body = json.dumps(payload).encode("utf8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                recorder.seen_auth.append(self.headers.get("Authorization", ""))
                if recorder.fail_status:
                    self._send(recorder.fail_status, {"message": "boom"})
                    return
                if self.path == "/api/":
                    self._send(200, {"message": "API running."})
                    return
                entity = self.path.removeprefix("/api/states/")
                self._send(200, {"entity_id": entity, "state": recorder.state})

            def do_POST(self):
                recorder.seen_auth.append(self.headers.get("Authorization", ""))
                data = self._body()
                recorder.calls.append({"path": self.path, "data": data})
                if recorder.fail_status:
                    self._send(recorder.fail_status, {"message": "boom"})
                    return
                if data.get("entity_id"):
                    recorder.state = "playing" if self.path.endswith("play_media") else "idle"
                self._send(200, [{"entity_id": data.get("entity_id"), "state": recorder.state}])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake_ha():
    server = FakeHomeAssistant()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.asyncio
async def test_play_media_posts_the_expected_payload(fake_ha):
    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)

    await client.play_media("media_player.echo", ("music", "alarm.wav"))

    assert fake_ha.calls[0]["path"] == "/api/services/media_player/play_media"
    assert fake_ha.calls[0]["data"] == {
        "entity_id": "media_player.echo",
        "media_content_id": "alarm.wav",
        "media_content_type": "music",
    }


@pytest.mark.asyncio
async def test_stop_posts_media_stop(fake_ha):
    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)

    await client.stop("media_player.echo")

    assert fake_ha.calls[0]["path"] == "/api/services/media_player/media_stop"
    assert fake_ha.calls[0]["data"]["entity_id"] == "media_player.echo"


@pytest.mark.asyncio
async def test_state_is_read_from_the_states_endpoint(fake_ha):
    fake_ha.state = "playing"
    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)

    assert await client.get_state("media_player.echo") == "playing"


@pytest.mark.asyncio
async def test_token_is_sent_as_bearer(fake_ha):
    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)

    await client.get_state("media_player.echo")

    assert fake_ha.seen_auth[0] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_http_error_becomes_a_domain_error(fake_ha):
    fake_ha.fail_status = 401
    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)

    with pytest.raises(RemoteHaError):
        await client.play_media("media_player.echo", ("music", "alarm.wav"))


@pytest.mark.asyncio
async def test_unreachable_instance_raises_remote_error():
    client = RemoteHaMediaClient("http://127.0.0.1:1", TOKEN, timeout=1.0)

    with pytest.raises(RemoteHaError):
        await client.get_state("media_player.echo")


def test_constructor_rejects_missing_token_and_relative_url():
    with pytest.raises(ValueError):
        RemoteHaMediaClient("http://127.0.0.1:8123", "")
    with pytest.raises(ValueError):
        RemoteHaMediaClient("127.0.0.1:8123", TOKEN)


@pytest.mark.asyncio
async def test_ping_uses_the_api_root(fake_ha):
    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)

    assert "API running" in await client.ping()


@pytest.mark.asyncio
async def test_full_play_stop_cycle_through_the_adapter(fake_ha):
    from nabu_core.devices import DevicePolicy
    from nabu_core.player import MediaPlayerAdapter, PlaybackStatus

    client = RemoteHaMediaClient(fake_ha.base_url, TOKEN)
    adapter = MediaPlayerAdapter(
        client,
        DevicePolicy(allowed={"media_player.echo"}, denied={"media_player.kuchendot"}),
        media=("music", "alarm.wav"),
    )

    accepted = await adapter.start("media_player.echo")
    playing = await adapter.confirm_playing("media_player.echo", wait=1.0, interval=0.05)
    stopped = await adapter.stop("media_player.echo", wait=1.0, interval=0.05)

    assert accepted.status is PlaybackStatus.ACCEPTED
    assert playing.status is PlaybackStatus.PLAYING_CONFIRMED
    assert stopped.status is PlaybackStatus.STOPPED_CONFIRMED
    assert fake_ha.state == "idle"


@pytest.mark.asyncio
async def test_denied_device_never_reaches_the_network(fake_ha):
    from nabu_core.devices import DeviceDenied, DevicePolicy

    policy = DevicePolicy(allowed={"media_player.echo"}, denied={"media_player.kuchendot"})

    with pytest.raises(DeviceDenied):
        policy.check("media_player.kuchendot")
    assert fake_ha.calls == []
