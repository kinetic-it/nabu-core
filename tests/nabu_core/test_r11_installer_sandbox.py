"""Real shell install and recovery against a local, network-isolated filesystem."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/install_d2.sh"


OPTIONS = json.dumps(
    {
        "data": {
            "entries": [
                {
                    "domain": "kinetic_voice_router",
                    "data": {"url": "lab"},
                    "options": {"volume": 0.3},
                },
                {"domain": "other_integration", "data": {"keep": 1}},
            ]
        }
    }
).encode()
ROLLBACK = SCRIPT.with_name("rollback_d2.sh")


def run_install(
    tmp_path,
    fail_swap=False,
    lie_stop=False,
    lock_occupied=False,
    lost_after_swap=False,
    stalled_guard=False,
    extra_backup=False,
    stale_db=False,
    lose_lock=False,
    lose_lock_before_stop=False,
    fail_start=False,
    fail_log=False,
    stage_symlink=False,
    stage_fifo=False,
    backup_symlink=False,
    find_error=False,
):
    root = tmp_path / "sandbox"
    cfg = root / "config"
    current = cfg / "custom_components/kinetic_voice_router"
    current.mkdir(parents=True)
    old = b'{"domain":"kinetic_voice_router","version":"old"}'
    (current / "manifest.json").write_bytes(old)
    (current / "translations").mkdir()
    (current / "translations/de.json").write_bytes(b"old translation")
    (current / "translations/de.json").chmod(0o640)

    if lock_occupied:
        lock = cfg / "backups/nabu-old-integrations/install.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text("other-run")
    (cfg / "nabu_core").mkdir()
    (cfg / "nabu_core/alarms.sqlite").write_bytes(b"db")
    (cfg / "nabu_core/nested").mkdir()
    (cfg / "nabu_core/nested/state").write_bytes(b"old nested state")
    (cfg / "nabu_core/nested/state").chmod(0o640)
    (cfg / ".storage").mkdir()
    (cfg / ".storage/core.config_entries").write_bytes(OPTIONS)
    backup = cfg / "backups/test"
    for name, body in {
        "kinetic_voice_router/manifest.json": old,
        "kinetic_voice_router/translations/de.json": b"old translation",
        "nabu_core_data/nested/state": b"old nested state",
        "storage/core.config_entries": OPTIONS,
        "nabu_core_data/alarms.sqlite": b"db",
    }.items():
        dest = backup / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    (backup / "kinetic_voice_router/translations/de.json").chmod(0o640)
    (backup / "nabu_core_data/nested/state").chmod(0o640)
    files = sorted(p for p in backup.rglob("*") if p.is_file())
    (backup / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(backup)}\n"
            for p in files
        )
    )
    if backup_symlink:
        (backup / "kinetic_voice_router/link").symlink_to("manifest.json")
    if extra_backup:
        (backup / "unlisted").write_bytes(b"not checksummed")
    if stale_db:
        (cfg / "nabu_core/alarms.sqlite").write_bytes(b"changed")
    stage = tmp_path / "stage"
    stage.mkdir()
    new = b'{"domain":"kinetic_voice_router","version":"new"}'
    (stage / "manifest.json").write_bytes(new)
    if stage_symlink:
        (stage / "link").symlink_to("manifest.json")
    if stage_fifo:
        os.mkfifo(stage / "pipe")
    fake = tmp_path / "bin"
    fake.mkdir()
    ssh = fake / "ssh"
    ssh.write_text("""#!/usr/bin/python3
import os,subprocess,sys,time
original = sys.argv[-1]
if os.environ.get('LOSE_LOCK_STOP') == 'yes' and 'ha core stop' in original:
    lock = os.environ['SANDBOX'] + '/config/backups/nabu-old-integrations/install.lock/owner'
    open(lock, 'w').write('other-run')
if os.environ.get('LOSE_LOCK') == 'yes' and 'ha core stats' in original:
    if open(os.environ['HA_STATE']).read() == 'stopped':
        lock = os.environ['SANDBOX'] + '/config/backups/nabu-old-integrations/install.lock/owner'
        open(lock, 'w').write('other-run')
if os.environ.get('STALL_GUARD') == 'yes' and 'ha core stats' in original:
    if open(os.environ['HA_STATE']).read() == 'stopped': time.sleep(4)
command = original.replace('/config/', os.environ['SANDBOX'] + '/config/')
shell = ['/usr/bin/busybox', 'sh'] if os.environ.get('NABU_TEST_BUSYBOX') == '1' else ['/bin/sh']
if '/bin/sh -s' in original:
    if os.environ.get('NABU_TEST_BUSYBOX') == '1':
        command = command.replace('/bin/sh -s', '/usr/bin/busybox sh -s')
    prefix = (os.environ['SANDBOX'] + '/config/').encode()
    script = sys.stdin.buffer.read().replace(b'/config/', prefix)
    result = subprocess.run(shell + ['-c',command], input=script)
else:
    result = subprocess.run(shell + ['-c',command])
if (os.environ.get('LOST_SWAP') == 'yes'
        and 'mv /config/custom_components/kinetic_voice_router' in original):
    if result.returncode == 0: sys.exit(255)
sys.exit(result.returncode)
""")
    ha = fake / "ha"
    ha.write_text("""#!/bin/sh
printf '%s\n' "$*" >> "$HA_LOG"
case "$*" in
  'core stop'*)
    if [ "$LIE_STOP" != yes ]; then printf stopped > "$HA_STATE"; fi
    printf '{"result":"ok","data":{}}\n' ;;
  'core start'*)
    if [ "$FAIL_START" = yes ]; then exit 43; fi
    printf started > "$HA_STATE"; printf '{"result":"ok","data":{}}\n' ;;
  'core stats'*)
    if [ "$(cat "$HA_STATE")" = stopped ]; then
      printf '{"result":"error","error_key":"homeassistant_not_running_error"}\n'
    else printf '{"result":"ok","data":{"memory_usage":1}}\n'; fi ;;
  'supervisor info'*) printf '{"result":"ok","data":{"healthy":true}}\n' ;;
  'core logs')
    if [ "$FAIL_LOG" = yes ]; then
      printf 'ERROR custom_components.kinetic_voice_router setup failed\n'
    else printf 'custom integration kinetic_voice_router\n'; fi ;;
  *) exit 1 ;;
esac
""")
    mv = fake / "mv"
    mv.write_text("""#!/bin/sh
case "$1" in
  */stage/core.config_entries)
    if [ "$FAIL_RECOVERY_ENTRIES" = yes ]; then exit 48; fi ;;
  */stage/nabu_core)
    if [ "$FAIL_RECOVERY_MOVE" = yes ]; then exit 47; fi ;;
  */kv-new-*)
    if [ "$FAIL_SWAP" = yes ] && [ ! -e "$FAIL_MARK" ]; then
      touch "$FAIL_MARK"
      exit 46
    fi ;;
esac
exec /bin/mv "$@"
""")
    find_cmd = fake / "find"
    find_cmd.write_text(
        '#!/bin/sh\nif [ "$FAIL_FIND" = yes ]; then exit 9; fi\nexec /usr/bin/find "$@"\n'
    )
    sleep = fake / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    for path in (ssh, ha, mv, sleep, find_cmd):
        path.chmod(0o755)
    log = tmp_path / "ha.log"
    state = tmp_path / "ha.state"
    state.write_text("started")
    env = dict(
        os.environ,
        PATH=str(fake) + os.pathsep + os.environ["PATH"],
        SANDBOX=str(root),
        HA_LOG=str(log),
        HA_STATE=str(state),
        FAIL_FIND="yes" if find_error else "no",
        FAIL_LOG="yes" if fail_log else "no",
        FAIL_START="yes" if fail_start else "no",
        FAIL_SWAP="yes" if fail_swap else "no",
        LIE_STOP="yes" if lie_stop else "no",
        FAIL_MARK=str(tmp_path / "mark"),
        LOSE_LOCK_STOP="yes" if lose_lock_before_stop else "no",
        LOSE_LOCK="yes" if lose_lock else "no",
        STALL_GUARD="yes" if stalled_guard else "no",
        LOST_SWAP="yes" if lost_after_swap else "no",
        HA_SSH_TIMEOUT="1" if stalled_guard else "90",
        HA_TARGET="root@testhost",
        HA_SSH_KEY="/nonexistent/test-key",
    )
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), str(stage), "/config/backups/test"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc, current, old, new, state, log


def test_real_shell_swap_and_start(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10, (proc.stdout, proc.stderr)
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "started"
    assert "core stop" in log.read_text()
    assert "core start" in log.read_text()
    assert "Installation abgeschlossen" not in proc.stdout


def test_real_shell_recovers_after_partial_swap(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, fail_swap=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert state.read_text() == "started", (proc.stdout, proc.stderr)
    assert "core stop" in log.read_text()
    assert "core start" in log.read_text()


def test_still_running_core_prevents_swap(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, lie_stop=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert state.read_text() == "started"


def test_existing_lock_stops_before_touching_ha(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, lock_occupied=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert state.read_text() == "started"
    assert not log.exists() or "core stop" not in log.read_text()


def test_lost_ssh_after_remote_swap_never_runs_conflicting_rollback(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, lost_after_swap=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "stopped"
    assert "core start" not in log.read_text()
    assert (tmp_path / "sandbox/config/backups/nabu-old-integrations/install.lock").is_dir()


def test_timeout_after_stop_keeps_lock_and_no_swap(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, stalled_guard=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert state.read_text() == "stopped"
    assert "core start" not in log.read_text()
    assert (tmp_path / "sandbox/config/backups/nabu-old-integrations/install.lock").is_dir()


def test_unlisted_backup_file_aborts_before_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, extra_backup=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert not log.exists() or "core stop" not in log.read_text()


def test_stale_database_backup_aborts_before_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, stale_db=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert not log.exists() or "core stop" not in log.read_text()


def test_changed_lock_owner_prevents_any_swap(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, lose_lock=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert "core start" not in log.read_text()


def test_lost_lock_immediately_before_stop_does_not_stop_ha(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, lose_lock_before_stop=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert state.read_text() == "started"
    assert not log.exists() or "core stop" not in log.read_text()


def run_rollback(
    tmp_path,
    token,
    *,
    lie_stop=False,
    fail_recovery_move=False,
    fail_recovery_entries=False,
    find_error=False,
):
    env = dict(
        os.environ,
        PATH=str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
        SANDBOX=str(tmp_path / "sandbox"),
        HA_LOG=str(tmp_path / "ha.log"),
        HA_STATE=str(tmp_path / "ha.state"),
        FAIL_FIND="yes" if find_error else "no",
        FAIL_START="no",
        FAIL_RECOVERY_ENTRIES="yes" if fail_recovery_entries else "no",
        FAIL_RECOVERY_MOVE="yes" if fail_recovery_move else "no",
        FAIL_SWAP="no",
        LIE_STOP="yes" if lie_stop else "no",
        FAIL_MARK=str(tmp_path / "mark"),
        STALL_GUARD="no",
        LOST_SWAP="no",
        LOSE_LOCK="no",
        LOSE_LOCK_STOP="no",
        HA_TARGET="root@testhost",
        HA_SSH_KEY="/nonexistent/test-key",
    )
    return subprocess.run(
        ["/bin/bash", str(ROLLBACK), "--confirmed-failure", "/config/backups/test", token],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_confirmed_failure_restores_data_and_only_nabu_entry(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10, (proc.stdout, proc.stderr)
    cfg = tmp_path / "sandbox/config"
    token = (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text()
    (cfg / "nabu_core/alarms.sqlite").write_bytes(b"changed by candidate")
    config = json.loads(OPTIONS)
    config["data"]["entries"][0]["options"]["volume"] = 0.9
    config["data"]["entries"][1]["data"]["keep"] = 2
    (cfg / ".storage/core.config_entries").write_text(json.dumps(config))
    restored = run_rollback(tmp_path, token)
    assert restored.returncode == 10, (restored.stdout, restored.stderr)
    assert (current / "manifest.json").read_bytes() == old
    assert (cfg / "nabu_core/alarms.sqlite").read_bytes() == b"db"
    assert (cfg / "nabu_core/nested/state").read_bytes() == b"old nested state"
    assert (cfg / "nabu_core/nested/state").stat().st_mode & 0o777 == 0o640
    assert (current / "translations/de.json").read_bytes() == b"old translation"
    assert (current / "translations/de.json").stat().st_mode & 0o777 == 0o640
    config = json.loads((cfg / ".storage/core.config_entries").read_text())
    assert config["data"]["entries"][0] == json.loads(OPTIONS)["data"]["entries"][0]
    assert config["data"]["entries"][1]["data"]["keep"] == 2
    assert state.read_text() == "started"
    assert (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text() == token
    assert (
        cfg / f"backups/nabu-old-integrations/recovery-{token}/failed/nabu_core/alarms.sqlite"
    ).read_bytes() == b"changed by candidate"


def test_recovery_rejects_wrong_owner_before_stopping(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    old_log = log.read_text()
    result = run_rollback(tmp_path, "20261121-000000-999999")
    assert result.returncode != 0
    assert log.read_text() == old_log
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "started"


def test_recovery_rejects_corrupt_backup_before_stopping(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    old_log = log.read_text()
    cfg = tmp_path / "sandbox/config"
    token = (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text()
    (cfg / "backups/test/nabu_core_data/alarms.sqlite").write_bytes(b"corrupt")
    result = run_rollback(tmp_path, token)
    assert result.returncode != 0
    assert log.read_text() == old_log
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "started"


def test_recovery_wont_swap_without_verified_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    cfg = tmp_path / "sandbox/config"
    token = (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text()
    result = run_rollback(tmp_path, token, lie_stop=True)
    assert result.returncode != 0
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "started"


def test_recovery_refuses_unsettled_install_after_lost_ssh(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, lost_after_swap=True)
    assert proc.returncode != 0
    cfg = tmp_path / "sandbox/config"
    token = (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text()
    result = run_rollback(tmp_path, token)
    assert result.returncode != 0
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "stopped"
    assert "core start" not in log.read_text()


def test_partial_recovery_keeps_core_stopped_and_preserves_failed_data(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    cfg = tmp_path / "sandbox/config"
    token = (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text()
    (cfg / "nabu_core/alarms.sqlite").write_bytes(b"changed by candidate")
    log_before = log.read_text()
    result = run_rollback(tmp_path, token, fail_recovery_move=True)
    assert result.returncode != 0
    assert state.read_text() == "stopped"
    assert "core start" not in log.read_text()[len(log_before) :]
    assert (cfg / "backups/nabu-old-integrations/install.lock/owner").read_text() == token
    saved = cfg / f"backups/nabu-old-integrations/recovery-{token}/failed/nabu_core/alarms.sqlite"
    assert saved.read_bytes() == b"changed by candidate"


def test_failed_start_never_marks_install_settled(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, fail_start=True)
    assert proc.returncode != 0
    cfg = tmp_path / "sandbox/config"
    lock = cfg / "backups/nabu-old-integrations/install.lock"
    assert not (lock / "settled").exists()
    result = run_rollback(tmp_path, (lock / "owner").read_text())
    assert result.returncode != 0
    assert (current / "manifest.json").read_bytes() == new
    assert state.read_text() == "stopped"


def test_recovery_rejects_symlink_in_backup_before_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    before = log.read_text()
    cfg = tmp_path / "sandbox/config"
    lock = cfg / "backups/nabu-old-integrations/install.lock"
    (cfg / "backups/test/kinetic_voice_router/sneaky").symlink_to("manifest.json")
    result = run_rollback(tmp_path, (lock / "owner").read_text())
    assert result.returncode != 0
    assert log.read_text() == before
    assert (current / "manifest.json").read_bytes() == new


def test_failed_final_config_swap_never_starts_ha(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    before = log.read_text()
    cfg = tmp_path / "sandbox/config"
    lock = cfg / "backups/nabu-old-integrations/install.lock"
    result = run_rollback(tmp_path, (lock / "owner").read_text(), fail_recovery_entries=True)
    assert result.returncode != 0
    assert state.read_text() == "stopped"
    assert "core start" not in log.read_text()[len(before) :]
    assert lock.exists()


def test_confirmed_router_log_error_allows_verified_recovery(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, fail_log=True)
    assert proc.returncode == 1
    cfg = tmp_path / "sandbox/config"
    lock = cfg / "backups/nabu-old-integrations/install.lock"
    token = (lock / "owner").read_text()
    assert (lock / "settled").read_text() == token
    (cfg / "nabu_core/alarms.sqlite").write_bytes(b"changed by candidate")
    result = run_rollback(tmp_path, token)
    assert result.returncode == 10, (result.stdout, result.stderr)
    assert (current / "manifest.json").read_bytes() == old
    assert (cfg / "nabu_core/alarms.sqlite").read_bytes() == b"db"
    assert state.read_text() == "started"


def test_install_rejects_stage_symlink_before_any_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, stage_symlink=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert not log.exists() or "core stop" not in log.read_text()


def test_install_rejects_stage_fifo_before_any_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, stage_fifo=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert not log.exists() or "core stop" not in log.read_text()


def test_install_rejects_backup_symlink_before_any_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, backup_symlink=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert not log.exists() or "core stop" not in log.read_text()


def test_recovery_rejects_symlinked_backup_storage_before_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    before = log.read_text()
    cfg = tmp_path / "sandbox/config"
    lock = cfg / "backups/nabu-old-integrations/install.lock"
    storage = cfg / "backups/test/storage/core.config_entries"
    alternate = tmp_path / "alternate-options"
    alternate.write_bytes(storage.read_bytes())
    storage.unlink()
    storage.symlink_to(alternate)
    result = run_rollback(tmp_path, (lock / "owner").read_text())
    assert result.returncode != 0
    assert log.read_text() == before
    assert (current / "manifest.json").read_bytes() == new


def test_install_find_error_fails_closed_before_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path, find_error=True)
    assert proc.returncode != 0
    assert (current / "manifest.json").read_bytes() == old
    assert not log.exists() or "core stop" not in log.read_text()


def test_recovery_find_error_fails_closed_before_stop(tmp_path):
    proc, current, old, new, state, log = run_install(tmp_path)
    assert proc.returncode == 10
    before = log.read_text()
    cfg = tmp_path / "sandbox/config"
    lock = cfg / "backups/nabu-old-integrations/install.lock"
    result = run_rollback(tmp_path, (lock / "owner").read_text(), find_error=True)
    assert result.returncode != 0
    assert log.read_text() == before
    assert (current / "manifest.json").read_bytes() == new
