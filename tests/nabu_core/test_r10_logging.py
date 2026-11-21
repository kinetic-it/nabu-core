import logging

import pytest

from tests.nabu_core.test_alarm_session import Device, session


@pytest.mark.asyncio
async def test_expected_idle_repeat_is_not_a_warning(caplog):
    device = Device(state="idle")
    sess = session(device, poll=0.005, max_restarts=1, restart_delay=0)
    with caplog.at_level(logging.DEBUG, logger="nabu_core.alarm_session"):
        await sess.run()
    repeats = [r for r in caplog.records if "ring ended early" in r.message]
    assert repeats
    assert all(r.levelno < logging.WARNING for r in repeats)


@pytest.mark.asyncio
async def test_exhausted_restarts_remain_visible_as_warning(caplog):
    device = Device(state="idle")
    sess = session(device, poll=0.005, max_restarts=1, restart_delay=0)
    with caplog.at_level(logging.WARNING, logger="nabu_core.alarm_session"):
        report = await sess.run()
    assert report.detail.startswith("player_idle")
    assert any("exhausted" in r.message and r.levelno >= logging.WARNING for r in caplog.records)
