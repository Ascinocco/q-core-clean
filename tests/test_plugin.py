"""The q-core plugin (split, part 3): life skills, MCP connection, self-update.

An installed plugin is a COPY of plugin/ in Claude Code's cache: nothing
outside it travels, and a symlink out of it is refused. These tests pin what
the copy must contain and how it keeps itself current.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
SELF_UPDATE = PLUGIN / "scripts" / "self-update.sh"
SERVER = PLUGIN / "scripts" / "q-core-server.sh"


def _json(path):
    return json.loads(path.read_text())


def test_manifest_tracks_commits_and_keeps_the_token_in_the_keychain():
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "q-core"
    assert "version" not in manifest  # a pinned version would freeze every machine
    config = manifest["userConfig"]
    assert config["api_token"]["sensitive"] is True and config["api_token"]["required"] is True
    assert config["server_url"]["required"] is True and "sensitive" not in config["server_url"]
    # The helper's inputs, substituted into statement-intake's backup step.
    assert config["server_ssh"]["type"] == "string" and "required" not in config["server_ssh"]
    assert config["server_checkout"]["default"] == "~/q-core"
    intake = (PLUGIN / "skills" / "statement-intake" / "SKILL.md").read_text()
    assert '--ssh "${user_config.server_ssh}" --checkout "${user_config.server_checkout}" backup' in intake


def test_marketplace_lists_the_plugin_by_relative_path_without_a_version():
    market = _json(REPO / ".claude-plugin" / "marketplace.json")
    assert market["name"] == "q-core"
    [entry] = market["plugins"]
    assert entry == {**entry, "name": "q-core", "source": "./plugin"} and "version" not in entry


def test_mcp_connects_to_the_configured_server_with_the_keychain_token():
    server = _json(PLUGIN / ".mcp.json")["mcpServers"]["q-core"]
    assert server == {"type": "http", "url": "${user_config.server_url}/mcp/",
                      "headers": {"Authorization": "Bearer ${user_config.api_token}"}}


def test_every_plugin_root_reference_in_a_skill_exists_in_the_plugin():
    skills = list((PLUGIN / "skills").glob("*/SKILL.md"))
    assert len(skills) == 6
    referenced = set()
    for skill in skills:
        referenced |= set(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)", skill.read_text()))
    assert referenced and all((PLUGIN / ref).is_file() for ref in referenced), referenced


def test_skills_do_not_point_at_repository_paths_the_copy_lacks():
    bare = re.compile(r"(?<![/{}A-Za-z_'])`((?:runbooks|docs)/[A-Za-z0-9_./-]+\.md|\.claude/skills/[^`]*)`")
    for skill in (PLUGIN / "skills").glob("*/SKILL.md"):
        text = skill.read_text()
        found = [m.group(1) for m in bare.finditer(text) if not text[max(0, m.start() - 9):m.start()].endswith("q-core's ")]
        assert not found, (skill, found)


def test_the_plugin_contains_no_symlinks_and_the_repo_runbooks_point_into_it():
    assert not [p for p in PLUGIN.rglob("*") if p.is_symlink()]
    for link in (p for p in (REPO / "runbooks").iterdir() if p.is_symlink()):
        assert link.resolve().is_relative_to(PLUGIN / "runbooks"), link


def test_session_start_hook_runs_the_executable_self_update():
    [entry] = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]["SessionStart"]
    assert entry["matcher"] == "startup"
    assert entry["hooks"][0]["command"] == '"${CLAUDE_PLUGIN_ROOT}/scripts/self-update.sh"'
    assert os.access(SELF_UPDATE, os.X_OK) and os.access(SERVER, os.X_OK)


@pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code CLI not installed")
def test_claude_validates_the_plugin_and_marketplace_strictly(tmp_path):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)}
    for target in (PLUGIN, REPO):
        result = subprocess.run(["claude", "plugin", "validate", str(target), "--json"],
                                capture_output=True, text=True, timeout=60, env=env)
        report = json.loads(result.stdout)
        findings = [report["manifest"]] + report["contents"]
        warnings = [(w["path"], w["message"]) for f in findings for w in f["warnings"]]
        assert report["success"] and not [e for f in findings for e in f["errors"]], report
        # The only tolerated warning: no `version`, deliberately, so installs track commits.
        assert all(path.endswith("version") for path, _ in warnings), warnings


# --- self-update.sh -------------------------------------------------------


@pytest.fixture
def fake_claude(tmp_path):
    """A `claude` that records its arguments and succeeds."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    (bin_dir / "claude").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n')
    (bin_dir / "claude").chmod(0o755)
    data = tmp_path / "data"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PLUGIN_DATA": str(data)}
    return env, calls, data


def _run_update(env):
    result = subprocess.run([str(SELF_UPDATE)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def _wait_for(predicate, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_self_update_refreshes_the_marketplace_then_the_plugin_detached(fake_claude):
    env, calls, data = fake_claude
    _run_update(env)
    assert _wait_for(lambda: calls.exists() and len(calls.read_text().splitlines()) == 2)
    assert calls.read_text().splitlines() == ["plugin marketplace update q-core", "plugin update q-core@q-core"]
    assert _wait_for(lambda: not (data / "update.lock").exists())
    assert "exit 0" in (data / "self-update.log").read_text()


def test_self_update_is_throttled_across_sessions(fake_claude):
    env, calls, data = fake_claude
    _run_update(env)
    assert _wait_for(lambda: calls.exists() and len(calls.read_text().splitlines()) == 2)
    _run_update(env)  # within the throttle window: nothing new
    time.sleep(0.5)
    assert len(calls.read_text().splitlines()) == 2
    _run_update({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    assert _wait_for(lambda: len(calls.read_text().splitlines()) == 4)


def test_a_held_lock_skips_and_a_stale_one_is_broken(fake_claude):
    env, calls, data = fake_claude
    data.mkdir()
    (data / "update.lock").mkdir()
    (data / "update.lock" / "at").write_text(str(int(time.time())))
    _run_update({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    time.sleep(0.5)
    assert not calls.exists()  # another session holds it
    (data / "update.lock" / "at").write_text("0")  # killed long ago
    _run_update({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    assert _wait_for(lambda: calls.exists() and len(calls.read_text().splitlines()) == 2)


def test_self_update_without_claude_on_path_warns(tmp_path):
    """Review R2-F1(a): no CLI means no update can ever run; that must be visible."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    if shutil.which("claude", path=env["PATH"]):
        pytest.skip("a system claude is on /usr/bin")
    result = subprocess.run([str(SELF_UPDATE)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "not on the SessionStart hook's PATH" in json.loads(result.stdout)["systemMessage"]
    assert not (tmp_path / "data").exists()


def test_a_hung_update_is_killed_after_the_timeout(fake_claude, tmp_path):
    env, calls, data = fake_claude
    (tmp_path / "bin" / "claude").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\nexec sleep 30\n')
    started = time.time()
    _run_update({**env, "Q_CORE_UPDATE_TIMEOUT_SECONDS": "1"})
    assert time.time() - started < 5  # the session never waits
    assert _wait_for(lambda: not (data / "update.lock").exists(), seconds=10)
    assert calls.read_text().splitlines() == ["plugin marketplace update q-core"]  # the plugin update never ran


# --- q-core-server.sh ------------------------------------------------------


def _server(*args, env=None):
    return subprocess.run([str(SERVER), *args], capture_output=True, text=True, timeout=20,
                          env=env or {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})


@pytest.mark.parametrize("args", [
    ["--ssh", "-oProxyCommand=evil", "backup"],
    ["--ssh", "host;rm", "backup"],
    ["--checkout", "q-core; rm -rf ~", "backup"],
    ["--checkout", "$(whoami)", "backup"],
    ["unknown"],
    [],
])
def test_server_helper_refuses_unsafe_or_unknown_input(args):
    result = _server(*args)
    assert result.returncode == 2 and json.loads(result.stderr)["error"] == "usage"


def test_server_helper_runs_backup_in_the_local_checkout_on_the_server(tmp_path):
    checkout = tmp_path / "q-core"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    python = checkout / ".venv" / "bin" / "python"
    python.write_text('#!/bin/sh\n[ "$1 $2" = "-m api.backup" ] && echo \'{"path":"data/backups/x.db","integrity":"ok"}\'\n')
    python.chmod(0o755)
    result = _server("--ssh", "", "--checkout", str(checkout), "backup")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["integrity"] == "ok"


def test_server_helper_treats_unexpanded_placeholders_as_unset(tmp_path):
    """An unset userConfig option can be substituted as its literal placeholder."""
    checkout = tmp_path / "q-core"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    python = checkout / ".venv" / "bin" / "python"
    python.write_text('#!/bin/sh\necho \'{"path":"x","integrity":"ok"}\'\n')
    python.chmod(0o755)
    result = _server("--ssh", "${user_config.server_ssh}", "--checkout", str(checkout), "backup")
    assert result.returncode == 0, result.stderr  # local, not an SSH attempt or a usage error


def _installed_cli(tmp_path):
    """A PATH with the NixOS module's q-core-cli and a sudo that records its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "q-core-cli").write_text("#!/bin/sh\nexit 99\n")  # never run directly
    (bin_dir / "sudo").write_text(
        '#!/bin/sh\necho "$*" > "$(dirname "$0")/../sudo.args"\n'
        'echo \'{"path":"/srv/q-core/data/backups/x.db","integrity":"ok"}\'\n')
    for tool in ("q-core-cli", "sudo"):
        (bin_dir / tool).chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}


def test_server_helper_uses_the_installed_cli_as_the_service_user(tmp_path):
    env = _installed_cli(tmp_path)
    result = _server("--ssh", "", "--checkout", str(tmp_path / "no-checkout"), "backup", env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["integrity"] == "ok"
    assert (tmp_path / "sudo.args").read_text().split() == ["-n", "-u", "q-core", "q-core-cli", "backup"]


def test_server_helper_push_uses_the_installed_cli_over_ssh(tmp_path):
    env = _installed_cli(tmp_path)
    intake = tmp_path / "intake"
    (intake / "2026-09").mkdir(parents=True)
    (intake / "2026-09" / "a.csv").write_text("x,y\n")
    ssh = tmp_path / "bin" / "ssh"
    # Run the remote command locally, as sshd would, discarding the tar stream.
    ssh.write_text('#!/bin/bash\nwhile [ "$1" != "--" ]; do shift; done; shift; shift; cat >/dev/null; bash -c "$1"\n')
    ssh.chmod(0o755)
    result = subprocess.run([str(SERVER), "--ssh", "owner@q-core", "--checkout", "~/q-core", "push",
                             "--intake-dir", str(intake), "2026-09"],
                            capture_output=True, text=True, timeout=20, env=env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sudo.args").read_text().split() == [
        "-n", "-u", "q-core", "q-core-cli", "intake-receive", "2026-09", "--expect-files", "1", "--expect-bytes", "4"]


# --- a failing self-update is never silent (review R1-F1) ------------------


def _session_start(env):
    """One SessionStart: exit 0 always; returns the parsed warning, or None when silent."""
    result = subprocess.run([str(SELF_UPDATE)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ""
    return json.loads(result.stdout) if result.stdout else None


def _finished(data):
    return _wait_for(lambda: (data / "last-attempt").exists() and not (data / "update.lock").exists()
                     and ((data / "last-success").exists() or (data / "last-failure").exists()))


def test_a_failed_update_warns_at_every_session_start_until_one_succeeds(fake_claude, tmp_path):
    env, calls, data = fake_claude
    claude = tmp_path / "bin" / "claude"
    claude.write_text('#!/bin/sh\necho "fatal: Could not read from remote repository (git@github.com:x/y.git)" >&2\nexit 1\n')
    now = {**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"}
    assert _session_start(now) is None  # nothing known yet
    assert _finished(data)
    warning = _session_start({**env})  # throttled, but the warning still shows
    assert "last self-update failed" in warning["systemMessage"] and "Could not read from remote" in warning["systemMessage"]
    assert warning["hookSpecificOutput"] == {"hookEventName": "SessionStart", "additionalContext": warning["systemMessage"]}
    claude.write_text('#!/bin/sh\nexit 0\n')  # the key is fixed
    (data / "last-update").write_text("0")
    _session_start(now)
    assert _wait_for(lambda: (data / "last-success").exists())
    assert _wait_for(lambda: _session_start({**env}) is None)


def test_a_stale_last_success_warns(fake_claude):
    env, calls, data = fake_claude
    data.mkdir()
    (data / "last-attempt").write_text(str(int(time.time())))
    (data / "last-success").write_text(str(int(time.time()) - 3 * 86400))
    (data / "last-update").write_text(str(int(time.time())))  # throttled: only the report runs
    warning = _session_start(env)
    assert "no successful self-update since" in warning["systemMessage"]


def test_the_failure_reason_is_valid_json_without_url_credentials(fake_claude, tmp_path):
    env, calls, data = fake_claude
    (tmp_path / "bin" / "claude").write_text(
        '#!/bin/sh\necho \'error: "quoted" \\\\ at https://user:s3cret-invented@example.invalid/repo\' >&2\nexit 1\n')
    _session_start({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    assert _finished(data)
    warning = _session_start(env)
    assert '"quoted"' in warning["systemMessage"] and "s3cret-invented" not in warning["systemMessage"]


# --- push: a local intake folder to the server over OpenSSH ----------------


@pytest.fixture
def fake_server(tmp_path):
    """A 'server' checkout plus an `ssh` that runs the remote command there.

    The stand-in checks the helper passes BatchMode and `--`, then runs the
    remote command with sh, as sshd would; the real python receives the tar.
    """
    import sys
    server = tmp_path / "server"
    (server / ".venv" / "bin").mkdir(parents=True)
    python = server / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\ncd "{REPO}" && exec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text('#!/bin/sh\n'
                                 'case "$*" in *BatchMode=yes*) ;; *) echo "no BatchMode" >&2; exit 99 ;; esac\n'
                                 'while [ "$1" != "--" ]; do shift; done; shift; target=$1; shift\n'
                                 'echo "$target" > "$(dirname "$0")/ssh-target"\n'
                                 'exec sh -c "$1"\n')
    (bin_dir / "ssh").chmod(0o755)
    intake = tmp_path / "local-intake"
    (intake / "example_batch").mkdir(parents=True)
    (intake / "example_batch" / "statement.csv").write_text("invented,row\n")
    (intake / "example_batch" / "card.pdf").write_bytes(b"%PDF-invented")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "Q_CORE_API_TOKEN": "test-token", "Q_CORE_INTAKE_DIR": str(server / "intake")}
    return server, intake, bin_dir, env


def _push(fake, name="example_batch", ssh="owner@q-core"):
    server, intake, bin_dir, env = fake
    return subprocess.run([str(SERVER), "--ssh", ssh, "--checkout", str(server), "push", "--intake-dir", str(intake), name],
                          capture_output=True, text=True, timeout=60, env=env)


def test_push_streams_the_folder_and_prints_only_names_and_counts(fake_server):
    server, intake, bin_dir, env = fake_server
    result = _push(fake_server)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {"folder": "example_batch", "files": ["card.pdf", "statement.csv"], "count": 2, "bytes": 26}
    assert "invented" not in result.stdout + result.stderr
    landed = server / "intake" / "example_batch"
    assert (landed / "statement.csv").read_text() == "invented,row\n"
    assert stat.S_IMODE(landed.stat().st_mode) == 0o700 and stat.S_IMODE((landed / "statement.csv").stat().st_mode) == 0o600
    assert not list(landed.glob("._*"))  # no macOS AppleDouble files
    assert (bin_dir / "ssh-target").read_text().strip() == "owner@q-core"


def test_push_twice_is_refused_by_the_server(fake_server):
    assert _push(fake_server).returncode == 0
    again = _push(fake_server)
    assert again.returncode == 3 and json.loads(again.stderr)["error"] == "refused"


def test_push_refuses_symlinks_missing_folders_and_bad_names(fake_server, tmp_path):
    server, intake, bin_dir, env = fake_server
    (intake / "example_batch" / "sneaky").symlink_to("/etc/hosts")
    assert _push(fake_server).returncode == 3
    assert _push(fake_server, name="nope").returncode == 4
    (intake / "linked").symlink_to(intake / "example_batch")
    assert _push(fake_server, name="linked").returncode == 4
    for bad in ("../etc", "a/b", "x;rm"):
        assert _push(fake_server, name=bad).returncode == 2
    assert not (server / "intake").exists()


def test_push_on_the_server_itself_is_skipped(fake_server):
    result = _push(fake_server, ssh="")
    assert result.returncode == 0 and json.loads(result.stdout)["skipped"]
    assert not (fake_server[0] / "intake").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-0 file")
def test_push_refuses_an_unreadable_file_and_nothing_lands(fake_server):
    """Review R1-F1: tar skipped the unreadable file and the server committed the rest."""
    server, intake, bin_dir, env = fake_server
    secret = intake / "example_batch" / "locked.pdf"
    secret.write_bytes(b"%PDF-invented")
    secret.chmod(0)
    try:
        result = _push(fake_server)
    finally:
        secret.chmod(0o600)
    assert result.returncode == 3 and "not readable" in result.stderr
    assert not (server / "intake" / "example_batch").exists()


@pytest.mark.parametrize("args", [
    ["--ssh"], ["--ssh", "t", "--checkout"], ["--ssh", "t", "push", "--intake-dir"],
    ["--ssh", "t", "push", "--intake-dir", "/tmp", "one", "two"], ["--ssh", "t", "backup", "extra"],
])
def test_server_helper_never_hangs_on_a_missing_or_extra_argument(args):
    result = _server(*args)  # _server has a 20 s timeout; a hang would raise
    assert result.returncode == 2 and json.loads(result.stderr)["error"] == "usage"
def test_an_attempt_that_never_finished_warns(fake_claude):
    """Review R2-F1(b): killed mid-run, no success ever recorded."""
    env, calls, data = fake_claude
    data.mkdir()
    (data / "last-attempt").write_text(str(int(time.time()) - 3600))
    (data / "last-update").write_text(str(int(time.time())))  # throttled: only the report runs
    warning = _session_start(env)
    assert "never finished" in warning["systemMessage"]


def test_a_failure_without_keywords_still_gives_a_reason(fake_claude, tmp_path):
    env, calls, data = fake_claude
    (tmp_path / "bin" / "claude").write_text('#!/bin/sh\necho "something odd happened" >&2\nexit 7\n')
    _session_start({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    assert _finished(data)
    assert "exit 7" in _session_start(env)["systemMessage"]


def test_the_log_stays_bounded(fake_claude):
    env, calls, data = fake_claude
    data.mkdir()
    (data / "self-update.log").write_text("old line\n" * 1000)
    _session_start({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    assert _finished(data)
    assert _wait_for(lambda: len((data / "self-update.log").read_text().splitlines()) <= 400)


def test_the_raw_log_carries_no_url_credentials(fake_claude, tmp_path):
    env, calls, data = fake_claude
    (tmp_path / "bin" / "claude").write_text('#!/bin/sh\necho "fetch https://user:s3cret-invented@example.invalid/x" >&2\nexit 1\n')
    _session_start({**env, "Q_CORE_UPDATE_THROTTLE_SECONDS": "0"})
    assert _finished(data)
    log = (data / "self-update.log").read_text()
    assert "example.invalid" in log and "s3cret-invented" not in log
