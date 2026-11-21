"""Installer safety tests use fake ssh: they never contact Home Assistant."""

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/install_d2.sh"


def invoke(tmp_path, case):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "manifest.json").write_text(json.dumps({"domain": "kinetic_voice_router"}))
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls"
    ssh = fake / "ssh"
    ssh.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"diff -r"*) [ "$CASE" != rollback_fail ] || exit 48 ;;
  *"mv /config/custom_components/kinetic_voice_router"*)
    [ "$CASE" != swap_fail ] && [ "$CASE" != rollback_fail ] || exit 46 ;;
  *"ha core start"*) [ "$CASE" != start_fail ] || exit 47 ;;
  *"tar -x"*) [ "$CASE" != stage_fail ] || exit 44 ;;
  *"ha core logs"*)
    [ "$CASE" != logs_fail ] || exit 45
    if [ "$CASE" = logs_error ]; then
      printf "ERROR custom_components.kinetic_voice_router\n"; exit 0
    fi ;;
  *jq*) [ "$CASE" != duplicate ] || exit 42 ;;
  *'ha core stop'*) [ "$CASE" != stop_fail ] || exit 43 ;;
esac
printf '1\\n'
""")
    ssh.chmod(0o755)
    for name in ("sleep", "tar"):
        p = fake / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(fake) + os.pathsep + os.environ["PATH"],
        CASE=case,
        CALL_LOG=str(log),
        HA_TARGET="root@testhost",
        HA_SSH_KEY="/nonexistent/test-key",
    )
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), str(stage), "/config/backups/test-backup"],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )
    return proc, log.read_text() if log.exists() else ""


def test_duplicate_domain_stops_before_stopping_ha(tmp_path):
    proc, calls = invoke(tmp_path, "duplicate")
    assert proc.returncode != 0
    assert "jq" in calls
    assert "ha core stop" not in calls


def test_failed_ha_stop_aborts_before_copy_or_start(tmp_path):
    proc, calls = invoke(tmp_path, "stop_fail")
    assert proc.returncode != 0
    assert "ha core stop" in calls
    assert "mv /config/custom_components/kinetic_voice_router" not in calls
    assert "ha core start" not in calls


def test_stage_never_copied_into_loader_directory(tmp_path):
    proc, calls = invoke(tmp_path, "ok")
    assert proc.returncode == 10
    assert "/config/custom_components/kv-new" not in calls


def test_domains_verified_again_before_start(tmp_path):
    proc, calls = invoke(tmp_path, "ok")
    assert proc.returncode == 10
    start = calls.index("ha core start")
    assert calls[:start].count("jq -er") >= 2


def check_real_manifest_guard(root):
    content = SCRIPT.read_text()
    function = content.split("check_domains() {", 1)[1].split("\n}", 1)[0]
    guard = function.split("'", 2)[1]
    guard = guard.replace("/config/custom_components", str(root))
    return subprocess.run(["/bin/sh", "-c", guard], text=True, capture_output=True, timeout=5)


def manifest(root, directory, contents):
    path = root / directory / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents)


def test_real_guard_allows_one_domain_with_whitespace(tmp_path):
    manifest(tmp_path, "kinetic_voice_router", '{"domain" : "kinetic_voice_router"}')
    manifest(tmp_path, "another", '{"domain":"other"}')
    assert check_real_manifest_guard(tmp_path).returncode == 0


def test_real_guard_rejects_duplicate_in_unrelated_directory(tmp_path):
    manifest(tmp_path, "kinetic_voice_router", '{"domain":"kinetic_voice_router"}')
    manifest(tmp_path, "kv-old", '{"domain":"kinetic_voice_router"}')
    assert check_real_manifest_guard(tmp_path).returncode != 0


def test_real_guard_rejects_corrupt_manifest(tmp_path):
    manifest(tmp_path, "kinetic_voice_router", '{"domain":"kinetic_voice_router"}')
    manifest(tmp_path, "broken", "{invalid")
    assert check_real_manifest_guard(tmp_path).returncode != 0


def test_real_guard_rejects_missing_original(tmp_path):
    manifest(tmp_path, "other", '{"domain":"other"}')
    assert check_real_manifest_guard(tmp_path).returncode != 0


def test_rejects_missing_backup_before_any_remote_call(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "manifest.json").write_text('{"domain":"kinetic_voice_router"}')
    fake = tmp_path / "bin"
    fake.mkdir()
    ssh = fake / "ssh"
    ssh.write_text('#!/bin/sh\nprintf contacted > "$CALL_LOG"\nexit 99\n')
    ssh.chmod(0o755)
    log = tmp_path / "calls"
    env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"], CALL_LOG=str(log))
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), str(stage)], env=env, text=True, capture_output=True, timeout=5
    )
    assert proc.returncode != 0
    assert not log.exists()
    assert "backup" in proc.stderr.lower() or "backup" in proc.stdout.lower()


def test_installer_requires_explicit_target_and_key_before_remote_call(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "manifest.json").write_text('{"domain":"kinetic_voice_router"}')
    fake = tmp_path / "bin"
    fake.mkdir()
    ssh = fake / "ssh"
    ssh.write_text('#!/bin/sh\nprintf contacted > "$$CALL_LOG"\nexit 99\n'.replace("$$", "$"))
    ssh.chmod(0o755)
    log = tmp_path / "calls"
    env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"], CALL_LOG=str(log))
    env.pop("HA_TARGET", None)
    env.pop("HA_SSH_KEY", None)
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), str(stage), "/config/backups/test"],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert proc.returncode != 0
    assert not log.exists()


def check_real_backup_guard(root):
    line = next(
        line for line in SCRIPT.read_text().splitlines() if "sha256sum -c SHA256SUMS" in line
    )
    guard = line.split('"')[3].replace("'$BACKUP'", "'" + str(root) + "'")
    return subprocess.run(["/bin/sh", "-c", guard], text=True, capture_output=True, timeout=5)


def backup_fixture(root, omit_config_entry=False):
    import hashlib

    files = {
        "storage/core.config_entries": b"settings",
        "nabu_core_data/alarms.sqlite": b"database",
        "kinetic_voice_router/manifest.json": b"manifest",
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    recorded = dict(files)
    if omit_config_entry:
        recorded.pop("storage/core.config_entries")
        recorded["storage/core.config_entries.bak"] = b"other"
        (root / "storage/core.config_entries.bak").write_bytes(b"other")
    lines = [
        f"{hashlib.sha256(contents).hexdigest()}  ./{name}\n" for name, contents in recorded.items()
    ]
    (root / "SHA256SUMS").write_text("".join(lines))


def test_real_backup_guard_accepts_exact_complete_hash_manifest(tmp_path):
    backup_fixture(tmp_path)
    assert check_real_backup_guard(tmp_path).returncode == 0


def test_real_backup_guard_rejects_similar_but_not_exact_manifest_path(tmp_path):
    backup_fixture(tmp_path, omit_config_entry=True)
    assert check_real_backup_guard(tmp_path).returncode != 0


def test_real_backup_guard_rejects_changed_file(tmp_path):
    backup_fixture(tmp_path)
    (tmp_path / "nabu_core_data/alarms.sqlite").write_text("changed")
    assert check_real_backup_guard(tmp_path).returncode != 0


def test_stage_failure_happens_before_ha_stop(tmp_path):
    proc, calls = invoke(tmp_path, "stage_fail")
    assert proc.returncode != 0
    assert "tar -x" in calls
    assert "ha core stop" not in calls


def test_log_failure_never_claims_completed_installation(tmp_path):
    proc, calls = invoke(tmp_path, "logs_fail")
    assert proc.returncode != 0
    assert "ha core logs" in calls
    assert proc.returncode not in (0, 10)
    assert "Installation abgeschlossen" not in proc.stdout


def test_installer_has_error_recovery_after_successful_stop():
    script = SCRIPT.read_text()
    assert "trap" in script
    assert "rollback" in script.lower() or "recovery" in script.lower()


def test_swap_failure_recovers_original_before_restart(tmp_path):
    proc, calls = invoke(tmp_path, "swap_fail")
    assert proc.returncode != 0
    assert "diff -r" in calls
    assert "ha core start" in calls
    assert calls.index("diff -r") < calls.index("ha core start")


def test_unverified_rollback_never_starts_ha(tmp_path):
    proc, calls = invoke(tmp_path, "rollback_fail")
    assert proc.returncode != 0
    assert "diff -r" in calls
    assert "ha core start" not in calls


def test_uncertain_start_does_not_mutate_files_again(tmp_path):
    proc, calls = invoke(tmp_path, "start_fail")
    assert proc.returncode != 0
    assert calls.count("ha core start") == 1
    assert "diff -r" not in calls
    assert "Startstatus unklar" in proc.stderr


def test_logged_router_error_is_not_verified_success(tmp_path):
    proc, calls = invoke(tmp_path, "logs_error")
    assert "ha core logs" in calls
    assert proc.returncode not in (0, 10)
    assert "Router-Initialisierung" in proc.stderr
