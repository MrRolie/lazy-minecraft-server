"""Tests for the host-maintenance decision logic (mc-netwatch, mc-maint, mc-alert) and the wiring
between setup.sh, check.sh and the files they install. Run: uv run --with pytest pytest host/
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parent
MIN = 60
T0 = 1_800_000_000.0  # an arbitrary wall-clock origin

# Loading host/bin/* must not drop a __pycache__ there: setup.sh installs what is in bin/.
sys.dont_write_bytecode = True


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(HOST / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


nw = load("mc-netwatch")
mt = load("mc-maint")
REAL_DISK_USAGE = mt.disk_usage
REAL_BRIDGE_STATE = mt.bridge_state
al = load("mc-alert")


OK, LINK_DOWN, UPSTREAM_DOWN = (True, True), (False, False), (True, False)  # (gw_ok, ts_ok)


def run_checks(minutes, obs=LINK_DOWN, at=None, state=None):
    """Drive decide() through checks at the given minutes. `obs` is the default (gw_ok, ts_ok);
    `at` maps minute -> (gw_ok, ts_ok) overrides. Returns [(minute, action, alert)], final state."""
    state = state or {}
    at = at or {}
    out = []
    for m in minutes:
        gw_ok, ts_ok = at.get(m, obs)
        action, state, alert = nw.decide(T0 + m * MIN, gw_ok, ts_ok, state)
        out.append((m, action, alert))
    return out, state


def fired(out):
    return [(m, a) for m, a, _ in out if a]


# ---- mc-netwatch: parsing -------------------------------------------------------------------

def test_tailscale_online_only_on_literal_true():
    assert nw.tailscale_online(json.dumps({"Self": {"Online": True}}))
    assert not nw.tailscale_online(json.dumps({"Self": {"Online": False}}))
    assert not nw.tailscale_online(json.dumps({"Self": {}}))
    assert not nw.tailscale_online(json.dumps({"Self": {"Online": "true"}}))
    assert not nw.tailscale_online("")  # tailscaled down: no JSON at all
    assert not nw.tailscale_online("not json")
    assert not nw.tailscale_online("[]")


def test_default_gateway_takes_lowest_metric():
    routes = [
        {"dst": "default", "gateway": "192.168.1.1", "dev": "wlp2s0", "metric": 600},
        {"dst": "default", "gateway": "192.168.1.254", "dev": "enx001122334455", "metric": 100},
    ]
    assert nw.default_gateway(json.dumps(routes)) == "192.168.1.254"
    # NM's connectivity penalty (+20000) hands the default to Wi-Fi
    routes[1]["metric"] = 20100
    assert nw.default_gateway(json.dumps(routes)) == "192.168.1.1"


def test_default_gateway_none_when_no_route():
    assert nw.default_gateway("[]") is None
    assert nw.default_gateway("") is None
    assert nw.default_gateway("garbage") is None
    assert nw.default_gateway(json.dumps([{"dst": "default", "dev": "wg0"}])) is None


# ---- mc-netwatch: escalation ----------------------------------------------------------------

EVERY_2_MIN_24H = [m for m in range(0, 24 * 60, 2)]


def test_healthy_with_no_history_does_nothing():
    action, state, alert = nw.decide(T0, True, True, {})
    assert (action, alert) == (None, None)
    assert state == {"last_check": T0, "last_gw_ok": T0}


@pytest.mark.parametrize("gw_ok,ts_ok,kind", [
    (True, True, None), (False, True, None),  # Tailscale online: reachable, nothing to fix
    (True, False, nw.UPSTREAM), (False, False, nw.LINK),
])
def test_fault_kind(gw_ok, ts_ok, kind):
    assert nw.fault(gw_ok, ts_ok) == kind


def test_link_ladder_fires_each_rung_once_in_order():
    out, state = run_checks(range(0, 20, 2))
    assert fired(out) == [(4, nw.WIRED_UP), (8, nw.RESTART_TAILSCALED), (16, nw.REBOOT)]
    assert state["last_reboot"] == T0 + 16 * MIN


def test_no_action_below_four_minutes():
    out, _ = run_checks([0, 2, 3.9])
    assert fired(out) == []


def test_link_rung_not_repeated_within_a_stretch():
    out, _ = run_checks([0, 4, 6, 7])
    assert fired(out) == [(4, nw.WIRED_UP)]


def test_lowest_undone_rung_first_even_when_late():
    state = {"first_failure": T0, "last_check": T0 + 20 * MIN, "link_since": T0, "actions": []}
    got = []
    for m in (21, 23, 25):
        action, state, _ = nw.decide(T0 + m * MIN, False, False, state)
        got.append(action)
    assert got == [nw.WIRED_UP, nw.RESTART_TAILSCALED, nw.REBOOT]


def test_upstream_fault_24h_never_reboots_or_bounces_the_link():
    """Gateway answers, Tailscale offline for a day (WAN or control-plane outage)."""
    out, state = run_checks(EVERY_2_MIN_24H, obs=UPSTREAM_DOWN)
    assert fired(out) == [(30, nw.RESTART_TAILSCALED_IDLE)]  # once, at 30 min
    assert "last_reboot" not in state


def test_upstream_rung_retries_while_players_are_on_then_fires_once():
    state, got = {}, []
    for m in range(0, 40, 2):
        action, state, _ = nw.decide(T0 + m * MIN, True, False, state)
        if action == nw.RESTART_TAILSCALED_IDLE and m < 34:  # mc-maint --idle says: players on
            state = nw.undo_action(state, action, T0 + m * MIN, nw.TAILSCALED_SKIPPED, "players on")
        got.append(action)
    assert [m for m, a in zip(range(0, 40, 2), got) if a] == [30, 32, 34]
    acts = [a["action"] for a in state["actions"]]
    assert acts == [nw.TAILSCALED_SKIPPED, nw.RESTART_TAILSCALED_IDLE]  # skip noted once


def test_link_down_24h_reboots_exactly_once():
    out, _ = run_checks(EVERY_2_MIN_24H)
    assert [m for m, a in fired(out) if a == nw.REBOOT] == [16]


def test_second_reboot_needs_the_gateway_back_and_six_hours():
    _, state = run_checks(range(0, 18, 2))  # reboot at 16
    # gateway back at 30 (Tailscale still offline), then the link drops again for good
    minutes = list(range(30, 20 * 60, 2))
    out, state = run_checks(minutes, at={30: UPSTREAM_DOWN}, state=state)
    reboots = [m for m, a in fired(out) if a == nw.REBOOT]
    assert reboots == [16 + 360]  # not before the 6 h cap, and only once more
    assert [m for m, a in fired(out) if a == nw.WIRED_UP] == [36]  # new link stretch, rungs restart


def test_reboot_gate_without_gateway_recovery_blocks_even_after_six_hours():
    assert nw.reboot_allowed(T0, None, None)
    assert not nw.reboot_allowed(T0 + 7 * 3600, T0, T0 - 60)  # gateway last seen before the reboot
    assert not nw.reboot_allowed(T0 + 5 * 3600, T0, T0 + 3600)  # gateway back, but under 6 h
    assert nw.reboot_allowed(T0 + 6 * 3600, T0, T0 + 3600)


def test_episode_continues_across_our_own_reboot():
    _, state = run_checks(range(0, 18, 2))  # reboot recorded at 16
    action, state, _ = nw.decide(T0 + 23 * MIN, False, False, state)  # boot + 5 min, still down
    assert action is None
    assert state["first_failure"] == T0
    assert [a["action"] for a in state["actions"]] == [nw.WIRED_UP, nw.RESTART_TAILSCALED, nw.REBOOT]


def test_long_gap_between_checks_starts_a_new_episode():
    _, state = run_checks([0, 2, 4])  # wired-up done
    action, state, _ = nw.decide(T0 + 24 * 3600, False, False, state)
    assert action is None
    assert state["first_failure"] == T0 + 24 * 3600
    assert state["actions"] == [] and state["link_done"] == []


def test_recovery_alert_after_link_fault_names_duration_and_actions():
    out, _ = run_checks(range(0, 14, 2), at={12: OK})
    assert out[-1][2] == ("network recovered after 12 min (link down); "
                          "actions: wired-up (+4m), restart-tailscaled (+8m)")


def test_recovery_alert_after_long_upstream_fault_even_without_actions():
    # players on the whole time: the rung only ever skips; recovery at 50 min still alerts
    state = {}
    for m in range(0, 50, 2):
        action, state, _ = nw.decide(T0 + m * MIN, True, False, state)
        if action:
            state = nw.undo_action(state, action, T0 + m * MIN, nw.TAILSCALED_SKIPPED, "players on")
    _, _, alert = nw.decide(T0 + 50 * MIN, True, True, state)
    assert alert == ("network recovered after 50 min (Tailscale offline, gateway up); "
                     "actions: tailscaled-restart-skipped (+30m)")


def test_short_blip_recovers_silently():
    out, state = run_checks([0, 2, 3], at={3: OK})
    assert out[-1][2] is None
    assert state == {"last_check": T0 + 3 * MIN, "last_gw_ok": T0 + 3 * MIN}


def test_gates_survive_recovery():
    _, state = run_checks(range(0, 18, 2))
    _, state, _ = nw.decide(T0 + 22 * MIN, True, True, state)
    assert state["last_reboot"] == T0 + 16 * MIN and state["last_gw_ok"] == T0 + 22 * MIN
    assert "first_failure" not in state and "actions" not in state


def test_decide_does_not_mutate_its_input():
    state = {"first_failure": T0, "last_check": T0 + 2 * MIN, "actions": [], "link_since": T0}
    snapshot = json.dumps(state, sort_keys=True)
    nw.decide(T0 + 4 * MIN, False, False, state)
    assert json.dumps(state, sort_keys=True) == snapshot


def test_deferred_reboot_restores_gate_and_is_recorded_once():
    _, state = run_checks(range(0, 18, 2))
    now = T0 + 16 * MIN
    state = nw.reboot_deferred(state, None, now, "mc-maint deferred")
    assert "last_reboot" not in state
    action, state, _ = nw.decide(now + 2 * MIN, False, False, state)
    assert action == nw.REBOOT  # still due
    state = nw.reboot_deferred(state, None, now + 2 * MIN, "mc-maint deferred")
    assert [a["action"] for a in state["actions"]] == [nw.WIRED_UP, nw.RESTART_TAILSCALED, nw.REBOOT_DEFERRED]


def test_deferred_reboot_restores_previous_reboot_time():
    prev = T0 - 7 * 3600
    state = {"first_failure": T0, "last_check": T0 + 14 * MIN, "last_reboot": prev, "last_gw_ok": T0 - 3600,
             "link_since": T0, "link_done": [nw.WIRED_UP, nw.RESTART_TAILSCALED],
             "actions": [{"action": nw.WIRED_UP, "at": T0 + 4 * MIN},
                         {"action": nw.RESTART_TAILSCALED, "at": T0 + 8 * MIN}]}
    action, state, _ = nw.decide(T0 + 16 * MIN, False, False, state)
    assert action == nw.REBOOT and state["last_reboot"] == T0 + 16 * MIN
    state = nw.reboot_deferred(state, prev, T0 + 16 * MIN, "mc-maint deferred")
    assert state["last_reboot"] == prev


def test_state_roundtrip_and_corrupt_file(tmp_path):
    p = tmp_path / "netwatch.json"
    nw.save_state({"first_failure": T0, "actions": []}, str(p))
    assert nw.load_state(str(p)) == {"first_failure": T0, "actions": []}
    p.write_text("{half a file")
    assert nw.load_state(str(p)) == {}
    assert nw.load_state(str(tmp_path / "missing.json")) == {}


@pytest.mark.parametrize("bad", [
    [],  # not an object
    {"last_reboot": "yesterday"},
    {"first_failure": True},  # bool is not a time
    {"last_gw_ok": [1]},
    {"actions": "wired-up"},
    {"actions": [{"action": "wired-up"}]},  # missing "at"
    {"actions": [{"action": 3, "at": T0}]},
    {"actions": [{"action": "reboot", "at": T0, "why": 7}]},
    {"link_done": "wired-up"},
    {"kinds": [None]},
])
def test_wrong_shape_state_is_replaced_by_empty(tmp_path, bad, capsys):
    p = tmp_path / "netwatch.json"
    p.write_text(json.dumps(bad))
    assert nw.load_state(str(p)) == {}
    assert "wrong shape" in capsys.readouterr().out


def test_state_decide_writes_is_always_valid():
    for obs in (LINK_DOWN, UPSTREAM_DOWN):
        _, state = run_checks(range(0, 60, 2), obs=obs)
        assert nw.valid_state(state)
        _, state, _ = nw.decide(T0 + 3600, True, True, state)
        assert nw.valid_state(state)


def test_netwatch_logs_whether_the_alert_was_delivered(monkeypatch, capsys):
    monkeypatch.setattr(nw, "run", lambda cmd, timeout: (0, "", "mc-alert: no webhook (/etc/mc-host/webhook: "
                                                                  "No such file or directory); alert not sent"))
    nw.notify("network recovered after 12 min")
    assert "mc-alert exit 0: mc-alert: no webhook" in capsys.readouterr().out


def test_upstream_rung_asks_mc_maint_and_skips_when_not_idle(monkeypatch):
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        return (3, "idle=False: 2 online", "") if cmd[-1] == "--idle" else (0, "", "")

    monkeypatch.setattr(nw, "run", fake_run)
    _, state = run_checks(range(0, 32, 2), obs=UPSTREAM_DOWN)  # due at 30
    new = nw.act(nw.RESTART_TAILSCALED_IDLE, state, None, T0 + 30 * MIN)
    assert calls == [[nw.MAINT, "--idle"]]  # no systemctl restart
    assert [a["action"] for a in new["actions"]] == [nw.TAILSCALED_SKIPPED]


# ---- mc-maint -------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _healthy_disk(monkeypatch):
    """mc-maint's daily run watches the real disk: a healthy one unless a test says otherwise."""
    monkeypatch.setattr(mt, "disk_usage", lambda repo=None: (500 * 1024 ** 3, 0))
    monkeypatch.setattr(mt, "bridge_state", lambda: ("running", "healthy"))


@pytest.mark.parametrize("out,count", [
    ("There are 0 of a max of 12 players online: ", 0),
    ("There are 3 of a max of 12 players online: alice, bob, carol", 3),
    ("§6There are 1 of a max of 12 players online: §rdave", 1),
    ("", None),
    ("Failed to connect to RCON", None),
    (None, None),
])
def test_parse_player_count(out, count):
    assert mt.parse_player_count(out) == count


@pytest.mark.parametrize("container,players,server_active,busy,action", [
    (mt.STOPPED, None, False, [], mt.REBOOT),
    (mt.STOPPED, None, True, [], mt.REBOOT),
    (mt.RUNNING, 0, True, [], mt.REBOOT),
    (mt.RUNNING, 0, False, [], mt.STOP_THEN_REBOOT),  # live-restore: docker will not stop it
    (mt.RUNNING, 2, True, [], mt.DEFER),
    (mt.RUNNING, 2, False, [], mt.DEFER),
    (mt.RUNNING, None, True, [], mt.DEFER),  # RCON unreadable: fail safe
    (mt.UNKNOWN, None, True, [], mt.DEFER),  # docker/pgrep unreadable: fail safe
    (mt.UNKNOWN, 0, True, [], mt.DEFER),
    (mt.ORPHAN, None, True, [], mt.DEFER),  # JVM docker lost track of
    (mt.STOPPED, None, True, ["apt-daily-upgrade.service"], mt.DEFER),  # unattended-upgrade running
    (mt.RUNNING, 0, True, ["mc-backup.service"], mt.DEFER),  # backup mid-tar
])
def test_maint_decide(container, players, server_active, busy, action):
    assert mt.decide(container, players, server_active, busy)[0] == action


@pytest.mark.parametrize("docker_ok,running,runner,state", [
    (True, False, False, mt.STOPPED),  # the only way to STOPPED: all three agree
    (True, True, True, mt.RUNNING),
    (True, True, False, mt.RUNNING),
    (True, True, None, mt.RUNNING),
    (True, False, True, mt.ORPHAN),  # runner alive, docker shows nothing running
    (True, False, None, mt.UNKNOWN),  # pgrep error
    (True, None, False, mt.UNKNOWN),  # inspect error other than "no such container"
    (False, False, False, mt.UNKNOWN),  # docker does not answer (live-restore: may still run)
    (False, None, True, mt.UNKNOWN),
])
def test_classify_container(docker_ok, running, runner, state):
    assert mt.classify_container(docker_ok, running, runner) == state


@pytest.mark.parametrize("container,players,idle", [
    (mt.STOPPED, None, True),
    (mt.RUNNING, 0, True),
    (mt.RUNNING, 1, False),
    (mt.RUNNING, None, False),
    (mt.UNKNOWN, None, False),
    (mt.ORPHAN, None, False),
])
def test_is_idle(container, players, idle):
    assert mt.is_idle(container, players)[0] is idle


def stub_bin(tmp_path, **scripts):
    """Write executable stubs into tmp_path/bin and return a PATH that finds them first."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name, body in scripts.items():
        f = d / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    return f"{d}:/usr/bin:/bin"


@pytest.mark.parametrize("active_state,busy", [
    ("activating", True),  # a running oneshot: `systemctl is-active` would say no
    ("active", True), ("reloading", True), ("deactivating", True),
    ("inactive", False), ("failed", False),
])
def test_unit_busy_counts_a_running_oneshot(tmp_path, monkeypatch, active_state, busy):
    monkeypatch.setenv("PATH", stub_bin(tmp_path, systemctl=f'echo {active_state}'))
    assert mt.unit_busy("apt-daily-upgrade.service") is busy


def test_unit_busy_fails_safe_on_systemctl_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", stub_bin(tmp_path, systemctl="echo boom >&2; exit 1"))
    assert mt.unit_busy("mc-backup.service") is True


DOCKER_STUB = """
case "$1" in
  version) [ -f "$S/docker_down" ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }; echo 29.8.1;;
  inspect) [ -f "$S/inspect_err" ] && { echo "permission denied" >&2; exit 1; }
           [ -f "$S/running" ] && { echo true; exit 0; }
           [ -f "$S/exists" ] && { echo false; exit 0; }
           echo "Error: No such object: $4" >&2; exit 1;;
esac
"""


@pytest.mark.parametrize("files,state", [
    ([], mt.STOPPED),  # no container, no runner
    (["exists"], mt.STOPPED),  # container stopped, no runner
    (["running", "runner"], mt.RUNNING),
    (["runner"], mt.ORPHAN),  # compose/dockerd lost the container, the JVM lives
    (["docker_down"], mt.UNKNOWN),
    (["inspect_err"], mt.UNKNOWN),
    (["pgrep_err"], mt.UNKNOWN),
])
def test_container_state_needs_docker_inspect_and_pgrep_to_agree(tmp_path, monkeypatch, files, state):
    s = tmp_path / "s"
    s.mkdir()
    for f in files:
        (s / f).touch()
    monkeypatch.setenv("S", str(s))
    monkeypatch.setenv("PATH", stub_bin(
        tmp_path, docker=DOCKER_STUB,
        pgrep='[ -f "$S/pgrep_err" ] && exit 2; [ -f "$S/runner" ] && { echo 4242; exit 0; }; exit 1'))
    assert mt.container_state() == state


def test_idle_mode_exit_codes(monkeypatch):
    monkeypatch.setattr(mt, "container_state", lambda: mt.RUNNING)
    monkeypatch.setattr(mt, "player_count", lambda: 0)
    assert mt.main(["--idle"]) == 0
    monkeypatch.setattr(mt, "player_count", lambda: 3)
    assert mt.main(["--idle"]) == mt.EXIT_DEFERRED
    monkeypatch.setattr(mt, "player_count", lambda: None)
    assert mt.main(["--idle"]) == mt.EXIT_DEFERRED


def test_orphan_alerts_even_in_reboot_mode(monkeypatch):
    monkeypatch.setattr(mt, "observe", lambda: (mt.ORPHAN, None, mt.DEFER, "orphaned server"))
    alerts = []
    monkeypatch.setattr(mt, "alert", alerts.append)
    monkeypatch.setattr(mt, "reboot", lambda *a: pytest.fail("rebooted an orphan"))
    assert mt.main(["--reboot", "network down 16 min"]) == mt.EXIT_DEFERRED
    assert len(alerts) == 1 and "orphaned server" in alerts[0]


def test_maint_alert_logs_mc_alert_outcome(monkeypatch, capsys):
    monkeypatch.setattr(mt, "run", lambda cmd, timeout, cwd=None: (0, "", "mc-alert: alert not sent: HTTP Error 429"))
    mt.alert("hello")
    out = capsys.readouterr().out
    assert "mc-alert exit 0: mc-alert: alert not sent: HTTP Error 429" in out


def test_compose_project_name_is_pinned_to_the_container_name_mc_maint_uses():
    compose = (HOST.parent / "docker-compose.yml").read_text()
    name = re.search(r"^name: (\S+)$", compose, re.M).group(1)
    assert mt.CONTAINER == f"{name}-minecraft-1"


def test_maint_deferral_reason_names_player_count():
    assert mt.decide(mt.RUNNING, 4, True, []) == (mt.DEFER, "deferred: 4 online")


def test_maint_does_nothing_without_reboot_required(monkeypatch, capsys):
    monkeypatch.setattr(mt, "REBOOT_REQUIRED", "/nonexistent/reboot-required")

    def boom(*a, **k):
        raise AssertionError("must not observe or act when no reboot is required")

    monkeypatch.setattr(mt, "observe", boom)
    monkeypatch.setattr(mt, "reboot", boom)
    assert mt.main([]) == 0
    assert "no reboot required" in capsys.readouterr().out


def test_maint_defer_alerts_once_and_does_not_reboot(monkeypatch, tmp_path):
    flag = tmp_path / "reboot-required"
    flag.write_text("*** System restart required ***\n")
    monkeypatch.setattr(mt, "REBOOT_REQUIRED", str(flag))
    monkeypatch.setattr(mt, "observe", lambda: (mt.RUNNING, 2, mt.DEFER, "deferred: 2 online"))
    alerts = []
    monkeypatch.setattr(mt, "alert", alerts.append)
    monkeypatch.setattr(mt, "reboot", lambda *a: pytest.fail("rebooted while deferred"))
    assert mt.main([]) == 0
    assert len(alerts) == 1 and "deferred: 2 online" in alerts[0]


def test_maint_reboot_mode_exits_3_when_deferred_without_alert(monkeypatch):
    monkeypatch.setattr(mt, "observe", lambda: (mt.RUNNING, 1, mt.DEFER, "deferred: 1 online"))
    monkeypatch.setattr(mt, "alert", lambda m: pytest.fail("alerted in --reboot mode"))
    monkeypatch.setattr(mt, "reboot", lambda *a: pytest.fail("rebooted while deferred"))
    assert mt.main(["--reboot", "network down 16 min"]) == mt.EXIT_DEFERRED


def test_maint_dry_run_never_acts(monkeypatch, tmp_path):
    flag = tmp_path / "reboot-required"
    flag.write_text("")
    monkeypatch.setattr(mt, "REBOOT_REQUIRED", str(flag))
    monkeypatch.setattr(mt, "observe", lambda: (mt.STOPPED, None, mt.REBOOT, "minecraft verifiably not running"))
    monkeypatch.setattr(mt, "reboot", lambda *a: pytest.fail("dry run rebooted"))
    monkeypatch.setattr(mt, "alert", lambda m: pytest.fail("dry run alerted"))
    assert mt.main(["--dry-run"]) == 0


# ---- mc-alert -------------------------------------------------------------------------------

def test_unit_failure_message_fits_and_keeps_newest_lines():
    journal = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    msg = al.format_unit_failure("mc-backup.service", "asus-node", journal)
    assert len(msg) <= al.LIMIT
    assert msg.startswith("**mc-backup.service failed on asus-node**")
    assert "line 199 " in msg and "line 0 " not in msg
    assert msg.endswith("```")


def test_unit_failure_message_without_journal():
    assert al.format_unit_failure("mc-maint.service", "asus-node", "") == "**mc-maint.service failed on asus-node**"


def test_unit_failure_message_with_one_huge_line_still_fits():
    msg = al.format_unit_failure("u.service", "h", "y" * 10_000)
    assert len(msg) <= al.LIMIT


def test_trim():
    assert al.trim("short") == "short"
    assert len(al.trim("z" * 5000)) == al.LIMIT


def test_stderr_is_journal_compares_dev_inode(tmp_path):
    f = tmp_path / "stream"
    f.write_text("")
    with open(f, "w") as fh:
        st = os.fstat(fh.fileno())
        assert al.stderr_is_journal(f"{st.st_dev}:{st.st_ino}", fh.fileno())
        assert not al.stderr_is_journal(f"{st.st_dev}:{st.st_ino + 1}", fh.fileno())  # inherited, captured
        assert not al.stderr_is_journal("", fh.fileno())
        assert not al.stderr_is_journal("garbage", fh.fileno())


def test_webhook_missing_or_bogus_is_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(al.subprocess, "run", lambda *a, **k: None)  # keep logger out of the test
    assert al.read_webhook(str(tmp_path / "none")) is None
    bogus = tmp_path / "webhook"
    bogus.write_text("http://example.com/hook\n")
    assert al.read_webhook(str(bogus)) is None
    good = tmp_path / "good"
    good.write_text("https://discord.com/api/webhooks/1/abc\n")
    assert al.read_webhook(str(good)) == "https://discord.com/api/webhooks/1/abc"


def test_alert_exits_zero_without_webhook(tmp_path, monkeypatch):
    monkeypatch.setattr(al.subprocess, "run", lambda *a, **k: None)  # no logger calls
    monkeypatch.setattr(al, "WEBHOOK_FILE", str(tmp_path / "none"))
    monkeypatch.setattr(al, "read_webhook", lambda path=None: None)
    monkeypatch.setattr(al, "post", lambda *a: pytest.fail("posted without a webhook"))
    assert al.main(["hello"]) == 0


def test_alert_exits_zero_when_post_fails(monkeypatch):
    monkeypatch.setattr(al.subprocess, "run", lambda *a, **k: None)  # no logger calls
    monkeypatch.setattr(al, "read_webhook", lambda path=None: "https://discord.com/api/webhooks/1/x")

    def fail(*a):
        raise OSError("network unreachable")

    monkeypatch.setattr(al, "post", fail)
    assert al.main(["hello"]) == 0


# ---- wiring: what setup.sh installs is what check.sh checks ---------------------------------

SYSTEMD = HOST / "systemd"


def unit_text(name):
    return (SYSTEMD / name).read_text()


def test_every_exec_path_is_installed_by_setup():
    installed = {f"/usr/local/sbin/{p.name}" for p in (HOST / "bin").glob("mc-*")}
    for unit in SYSTEMD.glob("*.service"):
        for path in re.findall(r"^Exec\w+=(/usr/local/sbin/\S+)", unit.read_text(), re.M):
            assert path in installed, f"{unit.name} runs {path}, which setup.sh does not install"


@pytest.mark.parametrize("unit", ["mc-backup.service", "mc-maint.service", "mc-server.service", "mc-netwatch.service",
                                  "mc-heal.service",
                                  "mc-bridge-fence.service"])
def test_on_failure_alert_on(unit):
    assert "OnFailure=mc-alert@%n.service" in unit_text(unit)


def test_mc_server_unit_contract():
    t = unit_text("mc-server.service")
    for line in ["User=mm-mike", "WorkingDirectory=/home/mm-mike/minecraft-ai-host", "Type=oneshot",
                 "RemainAfterExit=yes", "After=docker.service network-online.target",
                 "ExecStart=/usr/bin/docker compose up -d minecraft",
                 "ExecStartPost=-/usr/local/sbin/mc-bridge-up",
                 "ExecStop=/usr/bin/docker compose stop -t 120",
                 "TimeoutStopSec=180"]:
        assert line in t.splitlines(), line
    # Requires= would propagate a docker restart (docker-ce upgrade) into a Minecraft restart
    assert not re.search(r"^Requires=", t, re.M)
    # the bridge never gates Minecraft: exactly one ExecStart, and it names minecraft only (a bare `up -d`
    # aborts every service on a bridge build failure); the bridge starts ONLY via mc-bridge-up, failure ignored
    starts = [ln for ln in t.splitlines() if ln.startswith("ExecStart=")]
    assert starts == ["ExecStart=/usr/bin/docker compose up -d minecraft"]
    bridge_lines = [ln for ln in t.splitlines() if ln.startswith("Exec") and "bridge" in ln]
    assert bridge_lines == ["ExecStartPost=-/usr/local/sbin/mc-bridge-up"]


BRIDGE_UP = HOST / "bin" / "mc-bridge-up"


def test_mc_bridge_up_is_gated_bounded_and_never_builds():
    t = BRIDGE_UP.read_text()
    budget = int(re.search(r"^TimeoutStartSec=(\d+)min$", unit_text("mc-server.service"), re.M).group(1)) * 60
    timeout = int(re.search(r"^TIMEOUT=(\d+)\b", t, re.M).group(1))
    assert timeout < budget  # a hung start can't eat the unit's start budget
    runs = [ln for ln in t.splitlines() if "compose up" in ln and not ln.lstrip().startswith("#")
            and "fail " not in ln]
    assert runs == ['/usr/bin/timeout "$TIMEOUT" "$DOCKER" compose up -d --no-build bridge >/dev/null 2>&1']
    assert "STAMP=/run/mc-host/bridge-fence.ok" in t.splitlines()


def run_bridge_up(tmp_path, stamp=True, docker_rc=0):
    """mc-bridge-up with its constants pointed at stubs (copied and rewritten: the real one has no knobs)."""
    log = tmp_path / "calls"
    for name, body in [("docker", f'echo "docker $*" >> {log}; echo "SECRET-TOKEN-TEXT" >&2; exit {docker_rc}'),
                       ("alert", f'echo "alert $*" >> {log}')]:
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / name).chmod(0o755)
    st = tmp_path / "stamp"
    if stamp:
        st.write_text("ok\n")
    script = BRIDGE_UP.read_text()
    for k, v in {"STAMP": st, "ALERT": tmp_path / "alert", "DOCKER": tmp_path / "docker"}.items():
        script = re.sub(rf"^{k}=.*$", f"{k}={v}", script, count=1, flags=re.M)
    (tmp_path / "up").write_text(script)
    r = subprocess.run(["bash", str(tmp_path / "up")], capture_output=True, text=True)
    return r, (log.read_text() if log.exists() else "")


def test_mc_bridge_up_without_the_fence_alerts_and_never_calls_docker(tmp_path):
    r, calls = run_bridge_up(tmp_path, stamp=False)
    assert r.returncode == 1
    assert "docker" not in calls and calls.startswith("alert Discord bridge NOT started: no egress fence")


def test_mc_bridge_up_alerts_on_a_failed_start_without_quoting_compose(tmp_path):
    r, calls = run_bridge_up(tmp_path, docker_rc=1)
    assert r.returncode == 1
    assert "docker compose up -d --no-build bridge" in calls and "alert Discord bridge NOT started" in calls
    assert "SECRET-TOKEN-TEXT" not in calls + r.stdout + r.stderr  # compose errors can quote .env.bridge


def test_mc_bridge_up_starts_quietly_when_fenced(tmp_path):
    r, calls = run_bridge_up(tmp_path)
    assert r.returncode == 0 and "alert" not in calls


def test_bridge_fence_unit_runs_before_docker_and_never_removes_on_stop():
    t = unit_text("mc-bridge-fence.service")
    lines = t.splitlines()
    assert "Before=docker.service" in lines and "WantedBy=multi-user.target" in lines
    assert "ExecStart=/usr/local/sbin/mc-bridge-fence apply" in lines
    assert not re.search(r"^ExecStop", t, re.M)  # stopping must leave the drops in place (fail-closed)


def fence_print():
    out = subprocess.run(["bash", str(HOST / "bin" / "mc-bridge-fence"), "print"], capture_output=True, text=True,
                         check=True).stdout
    kv = [ln.split(" ", 1) for ln in out.splitlines()]
    return {k: v for k, v in kv if k != "drop"}, [v for k, v in kv if k == "drop"]


def test_bridge_fence_print_lists_every_non_public_range():
    import ipaddress
    conf, drops = fence_print()
    nets = [ipaddress.ip_network(d) for d in drops]
    for must in ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]:
        assert ipaddress.ip_network(must) in nets, must
    # the host's LAN, the tailnet and the docker networks all fall inside a dropped range
    for addr in ["192.168.1.5", "100.101.102.103", "172.17.0.1", conf["subnet"].split("/")[0]]:
        assert any(ipaddress.ip_address(addr) in n for n in nets), addr
    assert conf["stamp"] == "/run/mc-host/bridge-fence.ok"
    assert (HOST / "check.sh").read_text().count("mc-bridge-fence check") == 1


def test_backup_is_a_system_unit_as_mm_mike():
    t = unit_text("mc-backup.service")
    assert "User=mm-mike" in t and "%h" not in t
    assert "OnCalendar=*-*-* 04:00:00" in unit_text("mc-backup.timer")
    assert "Persistent=true" in unit_text("mc-backup.timer")
    assert not (HOST.parent / "deploy" / "mc-backup.service").exists()


def test_maint_timer_is_not_persistent():
    t = unit_text("mc-maint.timer")
    assert "OnCalendar=*-*-* 05:40:00" in t
    assert not re.search(r"^Persistent=true", t, re.M)


def test_netwatch_timer_schedule():
    t = unit_text("mc-netwatch.timer")
    assert "OnBootSec=5min" in t and "OnUnitActiveSec=2min" in t


@pytest.mark.parametrize("timer,at", [("apt-daily.timer", "04:30:00"), ("apt-daily-upgrade.timer", "05:00:00")])
def test_apt_dropins_reset_then_set_calendar(timer, at):
    lines = [ln for ln in (SYSTEMD / f"{timer}.d" / "10-mc-host.conf").read_text().splitlines()
             if ln and not ln.startswith("#")]
    cal = [ln for ln in lines if ln.startswith("OnCalendar=")]
    assert cal == ["OnCalendar=", f"OnCalendar=*-*-* {at}"]  # empty first: OnCalendar= is additive
    assert "RandomizedDelaySec=0" in lines


def conf_list(name):
    """The quoted entries of an apt list option in host/conf/apt-52mc-host."""
    text = (HOST / "conf" / "apt-52mc-host").read_text()
    block = re.search(rf"^Unattended-Upgrade::{name} \{{(.*?)\}};", text, re.M | re.S)
    assert block, name
    return re.findall(r'^\s*"([^"]+)";', block.group(1), re.M)


def test_only_tailscale_is_an_unattended_origin():
    assert conf_list("Origins-Pattern") == ["origin=Tailscale,label=Tailscale,codename=${distro_codename},component=main"]
    check = (HOST / "check.sh").read_text()
    assert "p='origin=Tailscale,label=Tailscale,codename=${distro_codename},component=main'" in check


UU_RUN = r"""
import importlib.machinery, importlib.util, json, sys
loader = importlib.machinery.SourceFileLoader("uu", "/usr/bin/unattended-upgrade")
spec = importlib.util.spec_from_loader("uu", loader)
uu = importlib.util.module_from_spec(spec)
loader.exec_module(uu)
class Origin:
    def __init__(self, fields):
        self.__dict__.update(dict(origin="", label="", archive="", codename="", component="", site=""), **fields)
req = json.loads(sys.stdin.read())
out = {"match": {o: [uu.match_whitelist_string(p, Origin(f)) for p in req["patterns"]] for o, f in req["origins"].items()},
       "blacklisted": {n: uu.is_pkgname_in_blacklist(n, req["blacklist"]) for n in req["packages"]}}
print(json.dumps(out))
"""


def uu_eval(**req):
    res = subprocess.run(["/usr/bin/python3", "-c", UU_RUN], input=json.dumps(req),
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


needs_uu = pytest.mark.skipif(not Path("/usr/bin/unattended-upgrade").exists(), reason="unattended-upgrades not installed")


@needs_uu
def test_origin_pattern_matches_tailscale_only():
    """The pattern against the repos' Release-file fields (fetched 2026-09-29), using u-u's own matcher.

    Runs under the system python3, where python3-apt lives (the uv venv has no `apt`).
    """
    patterns = [p.replace("${distro_codename}", "noble") for p in conf_list("Origins-Pattern")]
    origins = {
        "docker": dict(origin="Docker", label="Docker CE", archive="noble", component="stable",
                       site="download.docker.com"),
        "tailscale": dict(origin="Tailscale", label="Tailscale", codename="noble", component="main",
                          site="pkgs.tailscale.com"),
        "ubuntu": dict(origin="Ubuntu", label="Ubuntu", archive="noble-updates", codename="noble",
                       component="main"),
    }
    got = uu_eval(patterns=patterns, origins=origins, blacklist=[], packages=[])["match"]
    assert got == {"docker": [False], "tailscale": [True], "ubuntu": [False]}


@needs_uu
def test_blacklist_stops_every_docker_package_and_nothing_else():
    docker = ["docker-ce", "docker-ce-cli", "docker-ce-rootless-extras", "containerd.io",
              "docker-buildx-plugin", "docker-compose-plugin"]
    others = ["tailscale", "docker.io-doc", "linux-image-generic", "containerd", "python3-docker", "docker-cef"]
    got = uu_eval(patterns=[], origins={}, blacklist=conf_list("Package-Blacklist"), packages=docker + others)
    assert {n for n, b in got["blacklisted"].items() if b} == set(docker)


# ---- wired static IP: pure pieces (host/lib/net.sh) and the rollback round trip -------------

NET_LIB = HOST / "lib" / "net.sh"
TARGET = ("192.168.1.5/24", "192.168.1.1", "192.168.1.1 1.1.1.1")


def sh(script, *args):
    """Run bash with host/lib/net.sh sourced; returns (exit code, stdout stripped)."""
    res = subprocess.run(["bash", "-c", f'. "{NET_LIB}"; {script}', "sh", *args],
                         capture_output=True, text=True, check=False)
    return res.returncode, res.stdout.strip()


@pytest.mark.parametrize("raw,want", [
    ("192.168.1.1,1.1.1.1", "192.168.1.1 1.1.1.1"),  # nmcli -g form
    ("192.168.1.1 1.1.1.1", "192.168.1.1 1.1.1.1"),
    ("  192.168.1.1 ,  1.1.1.1 ", "192.168.1.1 1.1.1.1"),
    ("", ""),
])
def test_norm_list(raw, want):
    assert sh('norm_list "$1"', raw) == (0, want)


@pytest.mark.parametrize("method,addrs,gw,dns,at_target", [
    ("manual", "192.168.1.5/24", "192.168.1.1", "192.168.1.1,1.1.1.1", True),  # applied by hand, nmcli -g form
    ("manual", "192.168.1.5/24", "192.168.1.1", "192.168.1.1 1.1.1.1", True),
    ("auto", "", "", "", False),  # today: DHCP
    ("auto", "192.168.1.5/24", "192.168.1.1", "192.168.1.1,1.1.1.1", False),  # extra address on DHCP
    ("manual", "192.168.1.5/24", "192.168.1.1", "1.1.1.1,192.168.1.1", False),  # DNS order matters
    ("manual", "192.168.1.50/24", "192.168.1.1", "192.168.1.1,1.1.1.1", False),
    ("manual", "192.168.1.5/24", "", "192.168.1.1,1.1.1.1", False),
    ("manual", "192.168.1.5/24,192.168.1.6/24", "192.168.1.1", "192.168.1.1,1.1.1.1", False),
])
def test_wired_at_target(method, addrs, gw, dns, at_target):
    rc, _ = sh('wired_at_target "$@"', method, addrs, gw, dns, *TARGET)
    assert (rc == 0) == at_target


GW_OK = "192.168.1.1 lladdr 02:00:00:00:00:01 REACHABLE"


@pytest.mark.parametrize("control,answered,target,verdict", [
    (GW_OK, "0", "192.168.1.5 FAILED", "free"),
    (GW_OK, "0", "192.168.1.5 INCOMPLETE", "free"),
    (GW_OK, "0", "", "free"),
    (GW_OK, "1", "", "in-use"),  # answers ping
    (GW_OK, "0", "192.168.1.5 lladdr aa:bb:cc:dd:ee:ff REACHABLE", "in-use"),  # drops ping, answers ARP
    (GW_OK, "1", "192.168.1.5 lladdr aa:bb:cc:dd:ee:ff REACHABLE", "in-use"),
    ("", "0", "", "broken"),  # the control failed: "free" would be unproven
    ("192.168.1.1 FAILED", "0", "", "broken"),
    ("192.168.1.1 FAILED", "1", "192.168.1.5 lladdr aa:bb:cc:dd:ee:ff", "broken"),
])
def test_probe_verdict(control, answered, target, verdict):
    assert sh('probe_verdict "$@"', control, answered, target) == (0, verdict)


def test_prev_file_round_trips_through_the_rollback(tmp_path):
    """setup.sh's write_prev -> mc-net-commit rollback -> the exact nmcli calls, with a recording fake nmcli."""
    prev = tmp_path / "wired-ipv4.prev"
    rc, _ = sh('write_prev "$@"', str(prev), "Wired connection 1", "auto", "", "", "")
    assert rc == 0
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "calls"
    (fake / "nmcli").write_text(f'#!/bin/bash\nfor a in "$@"; do printf "[%s]" "$a"; done >> {calls}\necho >> {calls}\n')
    (fake / "nmcli").chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin", "MC_NET_PREV": str(prev), "MC_NET_FIRED": str(tmp_path / "fired")}
    res = subprocess.run(["bash", str(HOST / "bin" / "mc-net-commit"), "rollback"],
                         env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "fired").exists()  # the marker apply looks for
    assert calls.read_text().splitlines() == [
        "[connection][modify][Wired connection 1][ipv4.method][auto][ipv4.addresses][][ipv4.gateway][][ipv4.dns][]",
        "[connection][up][Wired connection 1]",
    ]


def test_prev_file_keeps_manual_multi_values(tmp_path):
    prev = tmp_path / "p"
    sh('write_prev "$@"', str(prev), "Wired connection 1", "manual", "192.168.1.7/24,10.0.0.2/8",
       "192.168.1.1", "192.168.1.1,9.9.9.9")
    assert prev.read_text().splitlines() == [
        "conn=Wired connection 1", "method=manual", "addresses=192.168.1.7/24,10.0.0.2/8",
        "gateway=192.168.1.1", "dns=192.168.1.1,9.9.9.9",
    ]


def test_mc_net_commit_refuses_bad_usage():
    res = subprocess.run(["bash", str(HOST / "bin" / "mc-net-commit"), "apply", "conn", "ip", "gw"],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 2


# ---- stop budget: runner < docker/compose < systemd -----------------------------------------

def _seconds(text, pattern):
    m = re.search(pattern, text, re.M)
    assert m, pattern
    return int(m.group(1))


def test_stop_budget_is_ordered_inner_to_outer():
    """STOP_DURATION (runner SIGKILLs java) < stop -t / stop_grace_period (docker SIGKILL) < TimeoutStopSec."""
    compose = (HOST.parent / "docker-compose.yml").read_text()
    runner = _seconds(compose, r'^\s+STOP_DURATION: "(\d+)"')
    grace_min = _seconds(compose, r"^\s+stop_grace_period: (\d+)m$")
    server = unit_text("mc-server.service")
    server_t = _seconds(server, r"^ExecStop=/usr/bin/docker compose stop -t (\d+)$")
    server_timeout = _seconds(server, r"^TimeoutStopSec=(\d+)$")
    maint_t = _seconds((HOST / "bin" / "mc-maint").read_text(), r'compose\("stop", "-t", "(\d+)"')
    scope_timeout = _seconds((SYSTEMD / "docker-.scope.d" / "10-mc-host.conf").read_text(), r"^TimeoutStopSec=(\d+)$")
    assert runner == 110
    assert runner < grace_min * 60 == server_t == maint_t == 120
    assert server_t < server_timeout == scope_timeout == 180


def test_scope_dropin_never_orders_before_mc_server():
    text = (SYSTEMD / "docker-.scope.d" / "10-mc-host.conf").read_text()
    live = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert live == ["[Scope]", "TimeoutStopSec=180"]


def test_setup_restarts_only_timers_for_changed_dropins():
    setup = (HOST / "setup.sh").read_text()
    assert '[[ "$unit" == *.timer ]] && timers_to_restart+=("$unit")' in setup


# ---- backup.sh: never tar a live world it could not flush; never leave a valid-looking partial --

def backup_env(tmp_path, running, rcon_ok_after):
    """A fake $HOME/minecraft-ai-host tree and a fake docker whose RCON answers after N `list` calls."""
    repo = tmp_path / "home" / "minecraft-ai-host"
    (repo / "mc_data" / "world" / "region").mkdir(parents=True)
    (repo / "mc_data" / "world" / "level.dat").write_bytes(b"level")
    (repo / "dist").mkdir()
    compose = (HOST.parent / "docker-compose.yml").read_text()
    pack_file = re.search(r'MODRINTH_MODPACK: *"/pack/([^"]+)"', compose).group(1)  # one home: compose names it
    (repo / "dist" / pack_file).write_bytes(b"pack")
    (repo / "pack").mkdir()
    (repo / "pack" / "lock.json").write_text("{}")
    (repo / ".env").write_text("LEVEL=world\n")
    (repo / "docker-compose.yml").write_text((HOST.parent / "docker-compose.yml").read_text())
    (repo / "mod").mkdir()
    (repo / "mod" / "RELEASE.sha256").write_text((HOST.parent / "mod" / "RELEASE.sha256").read_text())
    (repo / ".env.bridge").write_text("DISCORD_BRIDGE_TOKEN = malformed\n")  # breaks any compose call (rv3)
    calls = tmp_path / "calls"
    docker = f"""
echo "$*" >> {calls}
[ "$1" = compose ] && {{ echo "failed to parse env_file .env.bridge" >&2; exit 1; }}
if [ "$1" = ps ]; then {"echo abc123" if running else "true"}; exit 0; fi
if [ "$1" = exec ] && [ "$2" = abc123 ] && [ "$4" = list ]; then
  n=$(grep -c ' list$' {calls}); [ "$n" -gt {rcon_ok_after} ] && {{ echo "There are 0 of a max of 12 players online:"; exit 0; }}
  echo "Failed to connect to RCON" >&2; exit 1
fi
exit 0
"""
    env = {"HOME": str(tmp_path / "home"), "MC_BACKUP_RCON_WAIT": "15",
           "PATH": stub_bin(tmp_path, docker=docker, sleep="exit 0")}
    return repo, calls, env


def run_backup(env):
    return subprocess.run(["bash", str(HOST.parent / "deploy" / "backup.sh")], env=env,
                          capture_output=True, text=True, check=False)


def test_backup_waits_for_rcon_then_flushes_and_renames(tmp_path):
    repo, calls, env = backup_env(tmp_path, running=True, rcon_ok_after=2)
    res = run_backup(env)
    assert res.returncode == 0, res.stderr
    archives = sorted((repo / "backups").iterdir())
    assert len(archives) == 1 and archives[0].name.endswith(".tar.gz")
    assert subprocess.run(["gzip", "-t", str(archives[0])]).returncode == 0
    log = calls.read_text()
    assert log.index("save-off") > log.rindex(" list")  # flush only after RCON answered
    assert "save-all flush" in log and log.rstrip().endswith("save-on")
    assert not [ln for ln in log.splitlines() if ln.startswith("compose")]  # no compose parsing (rv3)
    assert "label=com.docker.compose.project=minecraft-ai-host" in log
    assert "label=com.docker.compose.service=minecraft" in log


def test_backup_records_which_ve_commands_build_the_world_ran(tmp_path):
    import tarfile
    repo, _, env = backup_env(tmp_path, running=False, rcon_ok_after=0)
    env["TMPDIR"] = str(tmp_path / "tmpdir")
    (tmp_path / "tmpdir").mkdir()
    assert run_backup(env).returncode == 0
    assert list((tmp_path / "tmpdir").iterdir()) == []  # the provenance staging dir is cleaned up
    archive = next((repo / "backups").glob("world-*.tar.gz"))
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        build = tar.extractfile("provenance/build.txt").read().decode()
        sha = tar.extractfile("provenance/RELEASE.sha256").read().decode()
    assert {"provenance/docker-compose.yml", "provenance/RELEASE.sha256", "provenance/build.txt"} <= names
    compose = (HOST.parent / "docker-compose.yml").read_text()
    mods = re.search(r'^ *MODS: *"([^"]+)"', compose, re.M).group(1)
    assert f"MODS={mods}\n" in build and "commit=" in build
    assert sha == (HOST.parent / "mod" / "RELEASE.sha256").read_text()


def test_backup_fails_loud_when_rcon_never_answers(tmp_path):
    repo, calls, env = backup_env(tmp_path, running=True, rcon_ok_after=10_000)
    res = run_backup(env)
    assert res.returncode == 1
    assert "RCON did not answer" in res.stderr
    assert list((repo / "backups").iterdir()) == []  # no archive, no partial
    assert "save-off" not in calls.read_text()


def test_backup_of_a_stopped_server_needs_no_rcon(tmp_path):
    repo, calls, env = backup_env(tmp_path, running=False, rcon_ok_after=10_000)
    res = run_backup(env)
    assert res.returncode == 0, res.stderr
    assert len(list((repo / "backups").glob("world-*.tar.gz"))) == 1
    assert "rcon-cli" not in calls.read_text()


def test_backup_drops_an_old_partial(tmp_path):
    repo, _, env = backup_env(tmp_path, running=False, rcon_ok_after=0)
    (repo / "backups").mkdir()
    (repo / "backups" / "world-20260101-040000.tar.gz.partial").write_bytes(b"truncated")
    assert run_backup(env).returncode == 0
    assert list((repo / "backups").glob("*.partial")) == []


def test_backup_unit_orders_after_mc_server():
    assert re.search(r"^After=.*\bmc-server\.service\b", unit_text("mc-backup.service"), re.M)


# ---- mc-net-commit apply: deadline, fresh streak, fired-rollback detection (fake clock) ------

NET_STUBS = {
    "date": '[ "$1" = "+%s" ] && { cat "$S/clock"; exit 0; }; exec /bin/date "$@"',
    "sleep": 'echo $(( $(cat "$S/clock") + ${1%.*} )) > "$S/clock"',
    # each ping costs 1 s; it fails while "down" exists or at any second listed in "flap"
    "ping": "\n".join([
        't=$(cat "$S/clock"); echo "$t $*" >> "$S/pings"; echo $((t + 1)) > "$S/clock"',
        '[ -f "$S/down" ] && exit 1',
        'grep -qx "$t" "$S/flap" 2>/dev/null && exit 1',
        'exit 0']),
    "ip": 'while read -r a; do echo "3: enx001122334455    inet $a brd 192.168.1.255 scope global"; done < "$S/addr"',
    "tailscale": 'echo \'{"Self": {"Online": true}}\'',
    "timeout": 'shift; exec "$@"',
    "nmcli": "\n".join([
        'if [ "$1" = --wait ]; then echo "$2" > "$S/nmcli_wait"; exit 0; fi',
        '[ "$1" = -g ] && echo enx001122334455']),
    "systemctl": "\n".join([
        'case "$1" in',
        '  is-active) [ -f "$S/timer_active" ];;',
        '  stop) echo "stop $2" >> "$S/systemctl"; rm -f "$S/timer_active"',
        '        [ -f "$S/fires_during_stop" ] && echo 123456 > "$S/exec_start"; exit 0;;',
        '  show) cat "$S/exec_start" 2>/dev/null || echo 0;;',
        'esac']),
}
CLOCK0, DEADLINE = 1000, 1180


def net_apply(tmp_path, addr="192.168.1.5/24", timer_active=True, clock=CLOCK0, **files):
    st = tmp_path / "s"
    st.mkdir()
    (st / "clock").write_text(f"{clock}\n")
    (st / "addr").write_text(addr + "\n")
    if timer_active:
        (st / "timer_active").touch()
    for name, content in files.items():
        (st / name).write_text(content)
    env = {"S": str(st), "PATH": stub_bin(tmp_path, **NET_STUBS), "MC_NET_FIRED": str(st / "fired")}
    res = subprocess.run(["bash", str(HOST / "bin" / "mc-net-commit"), "apply", "Wired connection 1",
                          "192.168.1.5", "192.168.1.1", str(DEADLINE)],
                         env=env, capture_output=True, text=True, check=False)
    pings = [ln.split() for ln in (st / "pings").read_text().splitlines()] if (st / "pings").exists() else []
    stopped = (st / "systemctl").read_text() if (st / "systemctl").exists() else ""
    return res, st, pings, stopped


def check_times(pings):
    """Start second of each healthy() check (each check sends its source-bound ping first)."""
    return [int(p[0]) for p in pings if p[-2] == "192.168.1.5"]


def test_confirms_only_after_three_checks_spanning_15s(tmp_path):
    res, _, pings, stopped = net_apply(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert stopped == "stop mc-net-rollback.timer\n"
    # a check costs 2 s of pings + 5 s sleep: 3 checks span only 14 s, so the 15 s rule needs a 4th
    assert check_times(pings) == [CLOCK0, CLOCK0 + 7, CLOCK0 + 14, CLOCK0 + 21]
    assert "confirmed:" in res.stdout


def test_pings_go_from_the_static_source_and_through_the_wired_device(tmp_path):
    _, _, pings, _ = net_apply(tmp_path)
    assert {tuple(p[1:-1]) for p in pings} == {("-c2", "-W1", "-I", "192.168.1.5"),
                                               ("-c2", "-W1", "-I", "enx001122334455")}


def test_nmcli_wait_is_bounded_by_the_deadline(tmp_path):
    _, st, _, _ = net_apply(tmp_path)
    assert int((st / "nmcli_wait").read_text()) == DEADLINE - 20 - CLOCK0


def test_never_healthy_gives_up_20s_before_the_deadline(tmp_path):
    res, _, pings, stopped = net_apply(tmp_path, down="")
    assert res.returncode == 1
    assert stopped == ""  # the rollback stays armed
    assert pings and max(int(p[0]) for p in pings) < DEADLINE - 20


def test_a_flap_restarts_the_streak(tmp_path):
    res, _, pings, _ = net_apply(tmp_path, flap=f"{CLOCK0 + 14}\n")  # the third check fails
    assert res.returncode == 0
    times = check_times(pings)
    assert CLOCK0 + 14 in times
    after = [t for t in times if t > CLOCK0 + 14]
    assert len(after) >= 3 and after[-1] - after[0] >= 14


def test_a_leftover_dhcp_address_is_not_healthy(tmp_path):
    res, _, _, stopped = net_apply(tmp_path, addr="192.168.1.5/24\n192.168.1.234/24")
    assert res.returncode == 1 and stopped == ""


@pytest.mark.parametrize("kw", [
    {"fired": ""},  # the rollback's own marker
    {"exec_start": "987654\n"},  # systemd saw the rollback service start
    {"fires_during_stop": ""},  # it started while the timer was being stopped
    {"timer_active": False},  # the timer already elapsed
])
def test_a_fired_rollback_is_failed_never_confirmed(tmp_path, kw):
    res, _, _, _ = net_apply(tmp_path, **kw)
    assert res.returncode == 4
    assert "FAILED" in res.stdout and "confirmed:" not in res.stdout


def test_no_time_left_does_nothing(tmp_path):
    res, st, pings, _ = net_apply(tmp_path, clock=DEADLINE - 20)
    assert res.returncode == 1
    assert not (st / "nmcli_wait").exists() and pings == []


# ---- check.sh conflict classification (host/lib/net.sh) --------------------------------------

NM_JOURNAL = "\n".join([
    "Sep 29 08:01:02 asus-node NetworkManager[812]: <warn>  [1759147262.1] device (enx001122334455): "
    "conflict detected for IP address 192.168.1.234 with host 02:00:00:00:00:02",
    "Sep 29 08:01:02 asus-node NetworkManager[812]: <info>  [1759147262.2] dhcp4 (enx001122334455): state changed",
    "Sep 29 21:40:00 asus-node NetworkManager[812]: <warn>  [1759196400.0] l3cfg[1,ifindex=3]: "
    "acd[192.168.1.50, used]: address conflict for 192.168.1.50 detected with 00:11:22:33:44:55",
    "Sep 29 21:41:00 asus-node NetworkManager[812]: <info>  [1759196460.0] manager: NetworkManager state is now CONNECTED_GLOBAL",
])


def test_conflict_lines_finds_nm_146_forms():
    rc, out = sh("conflict_lines <<<\"$1\"", NM_JOURNAL)
    assert rc == 0 and len(out.splitlines()) == 2


def test_todays_old_dhcp_conflict_does_not_count_for_192_168_1_5():
    _, conflicts = sh("conflict_lines <<<\"$1\"", NM_JOURNAL)
    assert sh('lines_for_ip 192.168.1.5 <<<"$1"', conflicts) == (0, "")  # neither .234 nor .50 is .5
    rc, mine = sh('lines_for_ip 192.168.1.234 <<<"$1"', conflicts)
    assert "192.168.1.234 with host" in mine and len(mine.splitlines()) == 1


def test_a_conflict_for_the_current_address_is_found():
    line = ("Oct 02 10:00:00 asus-node NetworkManager[812]: <warn> l3cfg[1,ifindex=3]: IPv4 address collision "
            "detection sees conflict on interface 3 (enx001122334455) for address 192.168.1.5 from host aa:bb:cc:dd:ee:ff")
    _, conflicts = sh("conflict_lines <<<\"$1\"", NM_JOURNAL + "\n" + line)
    assert sh('lines_for_ip 192.168.1.5 <<<"$1"', conflicts) == (0, line)


def test_check_sh_fails_on_the_current_address_and_only_informs_on_others():
    check = (HOST / "check.sh").read_text()
    assert 'res FAIL "address conflict for the wired address ${wired_ip}' in check
    assert 'res INFO "${others} conflict line(s) this boot for other addresses' in check


# ---- setup.sh step 10 and check.sh "bridge mailbox", run for real (as the current user) -----------------------

def section(path, start, stop):
    text = path.read_text()
    return text[text.index(start):text.index(stop)]


STEP10 = section(HOST / "setup.sh", "# ---- 10. bridge mailbox", "# ---- 11. bridge fence")
MAILBOX = section(HOST / "check.sh", "# ---- bridge mailbox", "# ---- bridge fence")
MAILBOX_DIRS = ["mc_data/mcve", "mc_data/mcve/out", "mc_data/mcve/in", "extras"]


def run_step10(repo):
    """Step 10 verbatim with setup.sh's helpers; MC_USER = whoever runs the test (install -o self needs no root)."""
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    script = (f'set -euo pipefail\nMC_USER={user}\nREPO={repo}\nNEXT=()\nABORTED=0\n'
              'step() { printf "== %s\\n" "$*"; }\nlog() { printf "   %s\\n" "$*"; }\n'
              'warn() { printf "   WARNING: %s\\n" "$*"; }\nnext() { NEXT+=("$*"); }\n'
              'abort_step() { warn "ABORTED: $*"; next "$*"; ABORTED=1; }\n' + STEP10 + '\necho "ABORTED=$ABORTED"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def run_mailbox_check(repo, path=None):
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    script = (f'MC_USER={user}\nREPO={repo}\nHOST_DIR={HOST}\n'
              'res() { printf "%s\\t%s\\n" "$1" "${*:2}"; }\n'
              'expect() { if [ "$3" = "$2" ]; then res PASS "$1 = $3"; else res FAIL "$1 = $3 (want $2)"; fi; }\n'
              'section() { :; }\n' + MAILBOX)
    env = {**os.environ, **({"PATH": path} if path else {})}
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    return [ln.split("\t", 1) for ln in out.splitlines() if "\t" in ln]


def test_step10_creates_the_mailbox_then_changes_nothing(tmp_path):
    first = run_step10(tmp_path)
    assert "ABORTED=0" in first
    for d in MAILBOX_DIRS:
        p = tmp_path / d
        assert p.is_dir() and not p.is_symlink() and oct(p.stat().st_mode & 0o777) == "0o755", d
    second = run_step10(tmp_path)
    assert second.count("unchanged") == len(MAILBOX_DIRS) and " set " not in second
    results = run_mailbox_check(tmp_path)
    assert [st for st, msg in results if "owner+mode" in msg] == ["PASS"] * len(MAILBOX_DIRS)


@pytest.mark.parametrize("link", ["mc_data", "mc_data/mcve"])
def test_step10_refuses_a_symlinked_parent_and_touches_nothing_through_it(tmp_path, link):
    world = tmp_path / "mc_data_world" if link == "mc_data" else tmp_path / "mc_data" / "world"
    world.mkdir(parents=True)
    (tmp_path / link).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / link).symlink_to(world)
    out = run_step10(tmp_path)
    assert "ABORTED=1" in out and "nothing created" in out
    assert sorted(p.name for p in world.rglob("*")) == []  # nothing created through the link
    assert not (tmp_path / "extras").exists()  # the whole step refused, not just the linked part


def test_check_fails_a_mailbox_that_runs_through_a_symlink(tmp_path):
    run_step10(tmp_path)
    mcve = tmp_path / "mc_data" / "mcve"
    world = tmp_path / "mc_data" / "world"
    mcve.rename(world)  # world/{out,in} exist, mm-mike 755: they would PASS if the link were followed
    mcve.symlink_to(world)
    results = run_mailbox_check(tmp_path)
    linked = [(st, msg) for st, msg in results if "symlink" in msg]
    assert {msg.split(" ", 1)[0] for st, msg in linked} == {str(tmp_path / d) for d in MAILBOX_DIRS[:3]}
    assert all(st == "FAIL" for st, _ in linked)


def test_check_alarms_on_a_large_inbox_dir(tmp_path):
    run_step10(tmp_path)
    big = tmp_path / "mc_data" / "mcve" / "in" / "inbox.jsonl"
    with open(big, "wb") as f:
        f.truncate(60 * 1024 * 1024)  # apparent size counts (du -sb)
    assert [st for st, msg in run_mailbox_check(tmp_path) if "mcve/in is" in msg] == ["WARN"]
    with open(big, "wb") as f:
        f.truncate(501 * 1024 * 1024)
    assert [st for st, msg in run_mailbox_check(tmp_path) if "mcve/in is" in msg] == ["FAIL"]


def test_world_init_refuses_a_symlinked_mailbox_before_creating_anything(tmp_path):
    (tmp_path / "deploy").mkdir()
    shutil.copy(HOST.parent / "deploy" / "world.sh", tmp_path / "deploy" / "world.sh")
    world = tmp_path / "mc_data" / "world"
    world.mkdir(parents=True)
    (tmp_path / "mc_data" / "mcve").symlink_to(world)
    r = subprocess.run(["bash", str(tmp_path / "deploy" / "world.sh"), "init"], capture_output=True, text=True)
    assert r.returncode == 1 and "symlink" in r.stderr
    assert list(world.iterdir()) == []


def test_world_sh_reaches_rcon_without_parsing_compose(tmp_path):
    (tmp_path / "deploy").mkdir()
    shutil.copy(HOST.parent / "deploy" / "world.sh", tmp_path / "deploy" / "world.sh")
    calls = tmp_path / "calls"
    docker = f"""
echo "$*" >> {calls}
[ "$1" = compose ] && {{ echo "failed to parse env_file .env.bridge" >&2; exit 1; }}
[ "$1" = ps ] && {{ echo abc123; exit 0; }}
case "$*" in *"worldborder get"*) echo "The world border is currently 6000.0 block(s) wide";; esac
exit 0
"""
    env = {"PATH": stub_bin(tmp_path, docker=docker), "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(tmp_path / "deploy" / "world.sh"), "status"], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    log = calls.read_text()
    assert not [ln for ln in log.splitlines() if ln.startswith("compose")]
    assert "exec abc123 rcon-cli execute in minecraft:overworld run worldborder get" in log


@pytest.mark.parametrize("props,ok", [
    ("pause-when-empty-seconds=60\n", False),   # paused server never unloads Chunky's chunks: OOM 2026-10-01
    ("pause-when-empty-seconds=-1\n", True),
    ("pause-when-empty-seconds=0\n", True),
    ("motd=x\n", False),                        # unreadable is a refusal, never a silent pass
    (None, False),
])
def test_world_ring_refuses_while_pause_when_empty_is_on(tmp_path, props, ok):
    (tmp_path / "deploy").mkdir()
    shutil.copy(HOST.parent / "deploy" / "world.sh", tmp_path / "deploy" / "world.sh")
    (tmp_path / "mc_data").mkdir()
    if props is not None:
        (tmp_path / "mc_data" / "server.properties").write_text(props)
    calls = tmp_path / "calls"
    docker = f"""
echo "$*" >> {calls}
[ "$1" = ps ] && {{ echo abc123; exit 0; }}
case "$*" in
  *"worldborder get"*) echo "The world border is currently 6000.0 block(s) wide";;
  *"chunky start"*) echo "[Chunky] Task started in minecraft:overworld (square, 3000)";;
esac
exit 0
"""
    env = {"PATH": stub_bin(tmp_path, docker=docker), "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(tmp_path / "deploy" / "world.sh"), "ring", "overworld", "3000"], env=env,
                       capture_output=True, text=True)
    log = calls.read_text() if calls.exists() else ""
    if ok:
        assert r.returncode == 0, r.stderr
        assert "chunky start" in log
    else:
        assert r.returncode == 1 and "refusing" in r.stderr
        assert "worldborder set" not in log and "chunky" not in log  # refused before touching the world


# ---- mc-maint disk watch: the unattended alarm on the accepted disk-fill residual (rv3) ----------------------

GiB, MiB = 1024 ** 3, 1024 ** 2


@pytest.mark.parametrize("free,inbox,n,word", [
    (100 * GiB, 1 * MiB, 0, None),
    (19 * GiB, 1 * MiB, 1, "GiB free"),
    (100 * GiB, 51 * MiB, 1, "mcve/in is 51 MiB"),
    (1 * GiB, 600 * MiB, 2, "GiB free"),
    (None, None, 1, "cannot read free space"),  # unreadable is an alert, never a silent pass
    (100 * GiB, None, 0, None),                 # no mailbox yet (pre-deploy): nothing to sum
])
def test_disk_problems(free, inbox, n, word):
    out = mt.disk_problems(free, inbox)
    assert len(out) == n and (word is None or word in out[0])


def test_disk_usage_sums_the_inbox_and_never_follows_a_link(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "disk_usage", REAL_DISK_USAGE)
    inbox = tmp_path / "mc_data" / "mcve" / "in"
    inbox.mkdir(parents=True)
    with open(inbox / "inbox.jsonl", "wb") as f:
        f.truncate(60 * MiB)
    free, size = mt.disk_usage(str(tmp_path))
    assert free and free > 0 and size == 60 * MiB
    (tmp_path / "elsewhere").mkdir()
    shutil.rmtree(inbox)
    inbox.symlink_to(tmp_path / "elsewhere")
    assert mt.disk_usage(str(tmp_path))[1] is None


def test_the_daily_run_alerts_on_the_disk_even_with_no_reboot_pending(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(mt, "disk_usage", lambda: (3 * GiB, 70 * MiB))
    monkeypatch.setattr(mt, "alert", sent.append)
    monkeypatch.setattr(mt, "REBOOT_REQUIRED", str(tmp_path / "no-reboot-required"))
    assert mt.main([]) == 0
    assert len(sent) == 2 and "GiB free" in sent[0] and "mcve/in" in sent[1]
    sent.clear()
    assert mt.main(["--dry-run"]) == 0 and sent == []  # dry run: printed, not sent


# ---- mc-bridge-fence, for real: apply / check / remove inside a throwaway network namespace (rv3) ------------
# A container with NET_ADMIN and --network none is its own netns: the real script, the real iptables. Packets
# were proven live on the workstation (bridge/README.md "Containment"); this pins the rule logic against regressions.
# Needs docker: without it these FAIL, never skip.

FENCE_IMAGE = "mc-fence-test:local"


@pytest.fixture(scope="module")
def fence_image():
    assert shutil.which("docker"), "docker missing: the fence logic can't be tested"
    r = subprocess.run(["docker", "build", "-q", "-t", FENCE_IMAGE, "-f",
                        str(HOST / "testdata" / "fence-test.Dockerfile"), str(HOST / "testdata")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return FENCE_IMAGE


def in_netns(image, script):
    r = subprocess.run(["docker", "run", "--rm", "--cap-add", "NET_ADMIN", "--network", "none",
                        "-v", f"{HOST / 'bin' / 'mc-bridge-fence'}:/fence:ro", image, "bash", "-c", script],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, "\n".join(ln for ln in r.stdout.splitlines() if not ln.startswith("# Warning"))


FENCE_DROPS = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]
STAMPED = 'test -f /run/mc-host/bridge-fence.ok && echo STAMP=yes || echo STAMP=no'


def test_fence_check_fails_before_apply_and_lists_every_missing_rule(fence_image):
    rc, out = in_netns(fence_image, "bash /fence check; echo rc=$?; " + STAMPED)
    assert "rc=1" in out and "STAMP=no" in out
    assert out.count("FENCE missing: v4 drop") == len(FENCE_DROPS)
    assert "FENCE missing: v4 jump from br-mcve" in out and "FENCE missing: v6 drop from br-mcve" in out


def test_fence_apply_installs_exactly_the_drops_first_and_writes_the_stamp(fence_image):
    # A rule already in raw PREROUTING (docker adds its own there): the fence must go IN FRONT of it, or an
    # earlier ACCEPT would end the raw table before the drops are reached.
    rc, out = in_netns(fence_image, "iptables -t raw -A PREROUTING -i br-mcve -j ACCEPT; "
                       "bash /fence apply >/dev/null; echo rc=$?; " + STAMPED +
                       "; iptables -t raw -S; ip6tables -t raw -S PREROUTING")
    assert "rc=0" in out and "STAMP=yes" in out
    lines = out.splitlines()
    pre = [ln for ln in lines if ln.startswith("-A PREROUTING")]
    assert pre[0] == "-A PREROUTING -i br-mcve -j MC_BRIDGE_FENCE"  # position 1: before anything else
    assert pre[1] == "-A PREROUTING -i br-mcve -j ACCEPT"  # the pre-seeded rule, now behind the fence
    chain = [ln for ln in lines if ln.startswith("-A MC_BRIDGE_FENCE")]
    assert chain == [f"-A MC_BRIDGE_FENCE -d {d} -j DROP" for d in FENCE_DROPS]
    assert "-A PREROUTING -i br-mcve -j DROP" in lines  # the v6 table, wholesale


def test_fence_reapply_is_idempotent_one_generation(fence_image):
    rc, out = in_netns(fence_image, "bash /fence apply >/dev/null; bash /fence apply >/dev/null; "
                       "iptables -t raw -S | grep -c '^-N MC_BRIDGE_FENCE'; "
                       "iptables -t raw -S PREROUTING | grep -c 'br-mcve'; ip6tables -t raw -S PREROUTING | grep -c br-mcve")
    assert out.split() == ["1", "1", "1"]


@pytest.mark.parametrize("delete", [*(f"iptables -t raw -D MC_BRIDGE_FENCE -d {d} -j DROP" for d in FENCE_DROPS),
                                    "iptables -t raw -D PREROUTING -i br-mcve -j MC_BRIDGE_FENCE",
                                    "ip6tables -t raw -D PREROUTING -i br-mcve -j DROP"])
def test_fence_check_fails_and_drops_the_stamp_when_any_one_rule_is_missing(fence_image, delete):
    rc, out = in_netns(fence_image, f"bash /fence apply >/dev/null; {delete}; bash /fence check; echo rc=$?; "
                       + STAMPED)
    assert "rc=1" in out and "STAMP=no" in out and "FENCE missing" in out


def test_fence_remove_leaves_no_rule_and_no_stamp(fence_image):
    rc, out = in_netns(fence_image, "bash /fence apply >/dev/null; bash /fence remove >/dev/null; "
                       "iptables -t raw -S | grep -c MC_BRIDGE; ip6tables -t raw -S | grep -c br-mcve; " + STAMPED)
    assert out.split() == ["0", "0", "STAMP=no"]



# ---- check.sh: the bridge must be HEALTHY, and its token file well-formed (values never shown) (rv3) ---------

def bridge_docker_stub(tmp_path, state):
    """docker that knows one bridge container in `state` ("running/healthy", ...), or none."""
    ps = "echo b1" if state else "true"
    return stub_bin(tmp_path, docker=f"""
case "$1" in
  ps) {ps};;
  inspect) echo "{state}";;
esac
""")


@pytest.mark.parametrize("state,want", [("running/healthy", "PASS"), ("running/unhealthy", "FAIL"),
                                        ("running/starting", "FAIL"), ("exited/unhealthy", "FAIL"),
                                        ("running/no-healthcheck", "FAIL"), ("", "WARN")])
def test_check_requires_the_bridge_healthy_not_just_running(tmp_path, state, want):
    run_step10(tmp_path)
    got = [st for st, msg in run_mailbox_check(tmp_path, bridge_docker_stub(tmp_path, state))
           if msg.startswith("bridge container")]
    assert got == [want]


@pytest.mark.parametrize("content,mode,fails", [
    ("DISCORD_BRIDGE_TOKEN=abc$def.x\nDISCORD_GUILD_ID=1\n# note\n\n", 0o600, []),
    ("DISCORD_BRIDGE_TOKEN = SECRET-VALUE\nDISCORD_GUILD_ID=1\n", 0o600, ["malformed line(s) 1", "no non-empty DISCORD_BRIDGE_TOKEN"]),
    ("DISCORD_BRIDGE_TOKEN=SECRET-VALUE \nDISCORD_GUILD_ID=1\n", 0o600, ["malformed line(s) 1",
                                                                  "no non-empty DISCORD_BRIDGE_TOKEN"]),  # trailing space
    ("DISCORD_BRIDGE_TOKEN=SECRET-VALUE\nDISCORD_GUILD_ID=1\n", 0o644, ["owner+mode"]),
    ("DISCORD_GUILD_ID=1\n", 0o600, ["no non-empty DISCORD_BRIDGE_TOKEN"]),
])
def test_check_validates_the_token_file_shape_without_showing_values(tmp_path, content, mode, fails):
    run_step10(tmp_path)
    envb = tmp_path / ".env.bridge"
    envb.write_text(content)
    envb.chmod(mode)
    results = run_mailbox_check(tmp_path, bridge_docker_stub(tmp_path, "running/healthy"))
    lines = [(st, msg) for st, msg in results if ".env.bridge" in msg]
    got = [msg for st, msg in lines if st == "FAIL"]
    assert len(got) == len(fails) and all(any(f in g for g in got) for f in fails), got
    assert not any("SECRET-VALUE" in msg or "abc$def" in msg for _, msg in results)


def test_check_flags_a_token_file_that_is_a_directory(tmp_path):
    run_step10(tmp_path)
    (tmp_path / ".env.bridge").mkdir()
    results = run_mailbox_check(tmp_path, bridge_docker_stub(tmp_path, "running/healthy"))
    assert ("FAIL", f"{tmp_path}/.env.bridge is not a regular file (a link or a directory breaks every compose call)") in [tuple(r) for r in results]


COMPOSE_CONFIG = section(HOST / "check.sh", "# A malformed .env.bridge breaks EVERY", "grace=")


@pytest.mark.parametrize("rc,want", [(0, "PASS"), (1, "FAIL")])
def test_check_fails_loud_when_compose_cannot_parse_and_never_echoes_its_error(tmp_path, rc, want):
    path = stub_bin(tmp_path, docker=f'echo "failed to parse env_file: SECRET-VALUE" >&2; exit {rc}')
    script = (f"REPO={tmp_path}\nres() {{ printf '%s\\t%s\\n' \"$1\" \"${{*:2}}\"; }}\n" + COMPOSE_CONFIG)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "PATH": path})
    st, msg = r.stdout.strip().split("\t", 1)
    assert st == want and "SECRET-VALUE" not in r.stdout + r.stderr
    if want == "FAIL":
        assert ".env.bridge" in msg



# ---- mc-maint: the daily alarm for a bridge that runs but isn't healthy (rv3) --------------------------------

@pytest.mark.parametrize("status,health,alerts", [
    ("running", "healthy", False),
    ("running", "unhealthy", True),   # the 600 s FATAL hold, or a hung pump
    ("running", "starting", True),
    ("restarting", "unhealthy", True),  # a restart loop between holds
    ("running", "", True),            # no healthcheck at all
    ("exited", "unhealthy", False),   # stopped: mc-bridge-up's alarm, not this one
    ("created", "", False),
])
def test_bridge_problem(status, health, alerts):
    assert (mt.bridge_problem(status, health) is not None) is alerts


def test_the_daily_run_alerts_on_an_unhealthy_bridge(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(mt, "bridge_state", lambda: ("running", "unhealthy"))
    monkeypatch.setattr(mt, "alert", sent.append)
    monkeypatch.setattr(mt, "REBOOT_REQUIRED", str(tmp_path / "no-reboot-required"))
    assert mt.main([]) == 0
    assert len(sent) == 1 and "Discord bridge is running but unhealthy" in sent[0]
    monkeypatch.setattr(mt, "bridge_state", lambda: None)  # not deployed yet: silent
    sent.clear()
    assert mt.main([]) == 0 and sent == []


def test_bridge_state_reads_status_and_health_by_label(monkeypatch):
    calls = []

    def fake_run(cmd, timeout, cwd=None):
        calls.append(cmd)
        if cmd[1] == "ps":
            return 0, "b1\n", ""
        return 0, "running unhealthy\n", ""
    monkeypatch.setattr(mt, "run", fake_run)
    assert REAL_BRIDGE_STATE() == ("running", "unhealthy")
    ps = calls[0]
    assert ps[:3] == ["docker", "ps", "-aq"]  # plain docker, not compose
    assert ps[ps.index("label=com.docker.compose.project=minecraft-ai-host") - 1] == "--filter"  # THIS project's
    assert ps[ps.index("label=com.docker.compose.service=bridge") - 1] == "--filter"
    monkeypatch.setattr(mt, "run", lambda cmd, timeout, cwd=None: (0, "", ""))
    assert REAL_BRIDGE_STATE() is None


@pytest.mark.parametrize("failing", ["ps", "inspect"])
def test_a_docker_that_does_not_answer_is_one_bridge_alert_not_silence(monkeypatch, tmp_path, failing):
    def fake_run(cmd, timeout, cwd=None):
        if cmd[1] == failing:
            return None, "", "timed out"
        return (0, "b1\n", "") if cmd[1] == "ps" else (0, "running healthy\n", "")
    monkeypatch.setattr(mt, "run", fake_run)
    state = REAL_BRIDGE_STATE()
    assert state[0] == "unknown" and "timed out" in state[1]
    sent = []
    monkeypatch.setattr(mt, "bridge_state", lambda: state)
    monkeypatch.setattr(mt, "alert", sent.append)
    monkeypatch.setattr(mt, "REBOOT_REQUIRED", str(tmp_path / "none"))
    assert mt.main([]) == 0
    assert len(sent) == 1 and sent[0].startswith("bridge watch: docker did not answer")



@pytest.mark.parametrize("shim", [
    'case " $* " in *" -C "*) exit 1;; esac; exec /usr/sbin/ip6tables "$@"',    # ip6tables: the v6 drop unverifiable
    'case " $* " in *" -C "*100.64.0.0/10*) exit 1;; esac; exec /usr/sbin/iptables "$@"',  # one v4 drop unverifiable
])
def test_fence_apply_fails_and_writes_no_stamp_when_its_own_check_fails(fence_image, shim):
    """apply's last step is check(): if any rule can't be verified, apply must fail and leave NO stamp (the
    stamp is what lets the bridge start)."""
    tool = "ip6tables" if "ip6tables" in shim else "iptables"
    rc, out = in_netns(fence_image, f"mkdir /shim; printf '#!/bin/sh\\n%s\\n' '{shim}' > /shim/{tool}; "
                       f"chmod +x /shim/{tool}; PATH=/shim:$PATH bash /fence apply; echo rc=$?; " + STAMPED)
    assert "STAMP=no" in out
    assert re.search(r"^rc=[1-9]", out, re.M), out


# ---- mc-heal: the hang watchdog ------------------------------------------------------------------------------

hl = load("mc-heal")
STARTED = "2026-01-01T00:00:00.123456789Z"  # a container start long before T0


def heal_checks(minutes, alive=False, at=None, state=None, started=STARTED):
    """Drive decide() through probes at the given minutes; `at` maps minute -> alive. Returns fired, state."""
    state, at, out = state or {}, at or {}, []
    for m in minutes:
        action, state, alert = hl.decide(T0 + m * MIN, at.get(m, alive), started, state)
        if action:
            out.append((m, action))
    return out, state


def test_heal_needs_three_failures_spanning_six_minutes():
    assert heal_checks([0, 2, 4])[0] == []                 # 3 failures, but over 4 min
    assert heal_checks([0, 2, 4, 6])[0] == [(6, hl.HEAL)]  # the 4th check of the 2-min timer
    assert heal_checks([0, 6])[0] == []                    # 6 min, but only 2 failures
    assert heal_checks([0, 1, 2, 3, 4, 5, 5.9])[0] == []   # 7 failures, 5.9 min
    assert heal_checks([0, 3, 6])[0] == [(6, hl.HEAL)]     # exactly 3 over exactly 6 min


def test_a_good_probe_ends_the_streak():
    fired, state = heal_checks([0, 2, 4, 6, 8, 10], at={4: True})
    assert fired == [] and state["streak"]["since"] == T0 + 6 * MIN
    assert heal_checks([0, 2, 4], at={4: True})[1].get("streak") is None


def test_a_gap_or_a_new_container_start_begins_a_new_streak():
    assert heal_checks([0, 2, 13, 15, 17])[0] == []                 # > 10 min since the last failure at 2
    assert heal_checks([0, 2, 13, 15, 17, 19])[0] == [(19, hl.HEAL)]
    assert heal_checks([0, 2, 12, 14, 16])[0] == [(12, hl.HEAL)]    # exactly 10 min: still one streak
    _, state = heal_checks([0, 2, 4])
    action, state, _ = hl.decide(T0 + 6 * MIN, False, "2026-01-02T00:00:00Z", state)  # restarted by someone
    assert action is None and state["streak"] == {"since": T0 + 6 * MIN, "count": 1, "last": T0 + 6 * MIN,
                                                  "started_at": "2026-01-02T00:00:00Z"}


def test_a_heal_is_counted_before_it_runs_and_the_cap_alerts_once():
    _, state = heal_checks([0, 2, 4, 6])
    assert state["heals"] == [T0 + 6 * MIN] and "streak" not in state
    two = {"heals": [T0 - 3 * 3600, T0 - 1 * 3600]}           # 2 heals in the last 6 h
    fired, state = heal_checks([0, 2, 4, 6, 8, 10], state=two)
    assert fired == [(6, hl.CAPPED)]                           # one alert, then quiet; never a heal
    assert state["heals"] == two["heals"]
    action, _, alert = hl.decide(T0 + 6 * MIN, False, STARTED, {**heal_checks([0, 2, 4], state=two)[1]})
    assert action == hl.CAPPED and alert.startswith("mc-heal: cap reached, not restarting")
    old = {"heals": [T0 - 7 * 3600, T0 - 1 * 3600]}            # one of them is out of the window
    assert heal_checks([0, 2, 4, 6], state=old)[0] == [(6, hl.HEAL)]


OK_OBS = dict(flag=None, uptime=3600.0, backup_busy=False, container="c0ffee", started=T0 - 3600)


@pytest.mark.parametrize("change,word", [
    ({}, None),
    ({"flag": "ring overworld r=3000"}, "maintenance flag set (ring overworld r=3000)"),
    ({"flag": ""}, "maintenance flag set"),                       # an empty flag file is still a flag
    ({"uptime": 599.0}, "since boot"),
    ({"uptime": None}, "time since boot"),
    ({"backup_busy": True}, "mc-backup.service"),
    ({"container": hl.UNKNOWN}, "docker did not answer"),
    ({"container": hl.NOT_RUNNING}, "no running minecraft container"),
    ({"started": None}, "when the container started"),
    ({"started": T0 - 599}, "container started"),
    ({"state": {"heals": [T0 - 29 * MIN]}}, "cooldown"),
    ({"state": {"heals": [T0 - 31 * MIN]}}, None),
])
def test_each_skip_condition(change, word):
    obs = {**OK_OBS, "state": {}, **change}
    reason = hl.skip_reason(T0, obs["flag"], obs["uptime"], obs["backup_busy"], obs["container"], obs["started"],
                            obs["state"])
    assert (reason is None) if word is None else (word in reason), reason


def test_parse_started_at():
    assert hl.parse_started_at("2026-10-02T05:45:05.123456789Z") == 1790919905
    assert hl.parse_started_at("2026-10-02T05:45:05Z") == 1790919905
    for bad in ("", "garbage", "0001-01-01T00:00:00Z", "2026-10-02 05:45:05"):
        assert hl.parse_started_at(bad) is None


@pytest.mark.parametrize("bad", [[], {"heals": "x"}, {"heals": [True]}, {"streak": {"since": 1}},
                                 {"streak": {"since": 1, "last": 2, "count": 1, "started_at": 5}},
                                 {"last_check": "now"}])
def test_heal_state_of_the_wrong_shape_is_replaced(tmp_path, bad, capsys):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(bad))
    assert hl.load_state(str(p)) == {}
    assert "wrong shape" in capsys.readouterr().out


def test_every_state_decide_writes_is_valid():
    for minutes, state in [([0, 2, 4, 6], {}), ([0, 2, 4, 6, 8], {"heals": [T0 - 60, T0 - 120]}), ([0, 2], {})]:
        _, s = heal_checks(minutes, state=state)
        assert hl.valid_state(s), s


HEAL_DOCKER = r"""
echo "docker $*" >> "$S/calls"
case "$1" in
  ps) [ -f "$S/ps_err" ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
      [ -f "$S/stopped" ] || echo c0ffee; exit 0;;
  inspect) cat "$S/started"; exit 0;;
  exec) [ -f "$S/hung" ] && exec sleep 5   # a SIGSTOPped JVM: RCON accepts, never answers
        echo "There are 0 of a max of 12 players online: "; exit 0;;
  logs) echo "[Server thread/INFO]: the last words"; exit 0;;
  compose) shift
    case "$*" in
      "stop -t 120 minecraft")
        if ls "$A"/*/docker-logs.txt "$A"/*/latest.log >/dev/null 2>&1; then echo "logs saved before stop" >> "$S/calls"; fi;;
      "up -d minecraft") cp "$S/now" "$S/started";;   # the container starts again, at "now"
    esac
    exit 0;;
esac
"""


@pytest.fixture
def heal_env(tmp_path, monkeypatch):
    """mc-heal's effects against stub docker/systemctl/mc-alert; returns (s dir, run(minute) -> calls so far)."""
    s, repo, archive = tmp_path / "s", tmp_path / "repo", tmp_path / "archive"
    s.mkdir()
    (repo / "mc_data" / "logs").mkdir(parents=True)
    (repo / "mc_data" / "logs" / "latest.log").write_text("LATEST LOG\n")
    (s / "started").write_text(STARTED + "\n")
    (s / "calls").write_text("")
    monkeypatch.setenv("S", str(s))
    monkeypatch.setenv("A", str(archive))
    monkeypatch.setenv("PATH", stub_bin(
        tmp_path, docker=HEAL_DOCKER,
        systemctl='echo "systemctl $*" >> "$S/calls"; [ -f "$S/sysctl_err" ] && exit 1; cat "$S/backup" 2>/dev/null '
                  '|| echo inactive',
        **{"mc-alert": 'echo "alert $*" >> "$S/calls"'}))
    monkeypatch.setattr(hl, "REPO", str(repo))
    monkeypatch.setattr(hl, "STATE_FILE", str(s / "state.json"))
    monkeypatch.setattr(hl, "ARCHIVE", str(archive))
    monkeypatch.setattr(hl, "ALERT", "mc-alert")
    monkeypatch.setattr(hl, "RCON_TIMEOUT", 0.3)
    monkeypatch.setattr(hl, "uptime", lambda: 86400.0)

    def run(minute):
        now = T0 + minute * MIN
        (s / "now").write_text(time.strftime("%Y-%m-%dT%H:%M:%S.5Z", time.gmtime(now)) + "\n")
        assert hl.check(now) == 0
        return (s / "calls").read_text()
    return s, run


def calls_of(text, prefix):
    return [ln for ln in text.splitlines() if ln.startswith(prefix)]


def test_a_rcon_that_times_out_heals_once_then_cools_down_then_caps(heal_env):
    """The SIGSTOP case: RCON accepts and never answers. Every probe is bounded by the timeout."""
    s, run = heal_env
    (s / "hung").touch()
    t = time.monotonic()
    for m in range(0, 7, 2):
        calls = run(m)
    assert len(calls_of(calls, "docker exec")) == 4
    assert calls_of(calls, "docker compose") == ["docker compose stop -t 120 minecraft",
                                                 "docker compose up -d minecraft"]
    heal_alerts = calls_of(calls, "alert ")
    assert len(heal_alerts) == 1 and heal_alerts[0].startswith("alert mc-heal: minecraft was hung")
    assert "restarted it" in heal_alerts[0] and "heal 1 of 2" in heal_alerts[0]
    # still hung after the restart: the new container's first 10 min, then the 30-min cooldown, are skips
    for m in range(8, 36, 2):
        calls = run(m)
    assert len(calls_of(calls, "docker exec")) == 4 and len(calls_of(calls, "docker compose up")) == 1
    for m in range(36, 43, 2):   # 36..42: heal #2
        calls = run(m)
    assert len(calls_of(calls, "docker compose up")) == 2
    for m in range(44, 100, 2):  # cooldown to 72, then 72..78 reaches the cap: alert once, never a 3rd heal
        calls = run(m)
    assert len(calls_of(calls, "docker compose up")) == 2
    alerts = calls_of(calls, "alert ")
    assert len(alerts) == 3 and alerts[2].startswith("alert mc-heal: cap reached, not restarting")
    assert time.monotonic() - t < 30  # each hung probe returned at its timeout


def test_the_heal_saves_logs_first_then_stops_with_the_budget_then_starts_minecraft_only(heal_env):
    s, run = heal_env
    (s / "hung").touch()
    for m in (0, 2, 4, 6):
        calls = run(m)
    seq = [ln for ln in calls.splitlines() if ln.startswith(("docker logs", "docker compose", "logs saved", "alert"))]
    assert seq == ["docker logs --tail 2000 c0ffee",
                   "docker compose stop -t 120 minecraft", "logs saved before stop",  # the stub saw them at stop
                   "docker compose up -d minecraft"] + seq[4:]
    assert len(seq) == 5 and seq[4].startswith("alert mc-heal:")
    (d,) = (s.parent / "archive").iterdir()
    assert (d / "latest.log").read_text() == "LATEST LOG\n"
    assert "the last words" in (d / "docker-logs.txt").read_text()
    assert str(d) in seq[4]


def test_mc_heal_never_runs_a_bare_up_and_never_touches_the_bridge():
    src = (HOST / "bin" / "mc-heal").read_text()
    calls = re.findall(r'\["docker", "compose", ([^\]]*)\]', src)
    assert calls == ['"stop", "-t", "120", "minecraft"', '"up", "-d", "minecraft"']
    assert "bridge" not in "".join(calls)


@pytest.mark.parametrize("setup,word,probes_docker_exec", [
    (lambda s, repo: (repo / "maintenance").write_text("ring overworld r=3000\n"), "maintenance flag", False),
    (lambda s, repo: (s / "backup").write_text("activating\n"), "mc-backup.service", False),
    (lambda s, repo: (s / "sysctl_err").touch(), "mc-backup.service", False),
    (lambda s, repo: (s / "stopped").touch(), "no running minecraft container", False),
    (lambda s, repo: (s / "ps_err").touch(), "docker did not answer", False),
])
def test_a_skip_never_probes_and_leaves_the_state_untouched(heal_env, capsys, setup, word, probes_docker_exec):
    s, run = heal_env
    (s / "hung").touch()
    seeded = {"last_check": T0 - 120, "heals": [T0 - 3 * 3600],
              "streak": {"since": T0 - 6 * MIN, "count": 3, "last": T0 - 2 * MIN, "started_at": STARTED}}
    (s / "state.json").write_text(json.dumps(seeded))
    setup(s, Path(hl.REPO))
    calls = run(0)
    assert f"skip: " in capsys.readouterr().out
    assert not calls_of(calls, "docker exec") and not calls_of(calls, "docker compose")
    assert json.loads((s / "state.json").read_text()) == seeded  # the history and the streak are kept as they were
    for f in ("maintenance",):
        (Path(hl.REPO) / f).unlink(missing_ok=True)
    for f in ("backup", "sysctl_err", "stopped", "ps_err"):
        (s / f).unlink(missing_ok=True)
    assert calls_of(run(2), "docker compose") == ["docker compose stop -t 120 minecraft",
                                                  "docker compose up -d minecraft"]  # the kept streak heals


def test_a_healthy_server_is_never_touched(heal_env):
    s, run = heal_env
    for m in range(0, 60, 2):
        calls = run(m)
    assert len(calls_of(calls, "docker exec")) == 30 and not calls_of(calls, "docker compose")
    assert not calls_of(calls, "alert")


def test_heal_logs_keep_the_newest_20_dirs(heal_env):
    s, run = heal_env
    archive = Path(hl.ARCHIVE)
    for i in range(25):
        (archive / f"2020-01-{i + 1:02d}_000000").mkdir(parents=True)
    (s / "hung").touch()
    for m in (0, 2, 4, 6):
        run(m)
    kept = sorted(p.name for p in archive.iterdir())
    assert len(kept) == 20 and kept[0] == "2020-01-07_000000"  # the 6 oldest went
    assert (archive / kept[-1] / "latest.log").exists()          # this heal's own dir is kept


def test_a_heal_that_dies_midway_is_still_counted(heal_env, monkeypatch):
    s, run = heal_env
    (s / "hung").touch()
    for m in (0, 2, 4):
        run(m)

    def die(*a):
        raise RuntimeError("killed mid-heal")
    monkeypatch.setattr(hl, "heal", die)
    with pytest.raises(RuntimeError):
        run(6)
    assert json.loads((s / "state.json").read_text())["heals"] == [T0 + 6 * MIN]  # the cap still sees it


@pytest.mark.parametrize("reply,alive", [
    ("echo 'There are 3 of a max of 12 players online: a, b, c'", True),
    ("echo 'Failed to connect to RCON serverError: dial tcp: connection refused'", False),  # exit 0, no count
    ("echo 'There are 0 of a max of 12 players online:'; exit 1", False),
    ("exec sleep 5", False),
])
def test_only_a_player_count_in_time_is_alive(tmp_path, monkeypatch, reply, alive):
    monkeypatch.setenv("PATH", stub_bin(tmp_path, docker=f'[ "$1 $3 $4" = "exec rcon-cli list" ] || exit 9; {reply}'))
    monkeypatch.setattr(hl, "RCON_TIMEOUT", 0.3)
    assert hl.probe("c0ffee")[0] is alive


def test_mc_heal_refuses_root(monkeypatch):
    monkeypatch.setattr(hl.os, "geteuid", lambda: 0)
    monkeypatch.setattr(hl, "check", lambda now: pytest.fail("ran as root"))
    assert hl.main([]) == 2


def test_heal_status_warns_on_the_flag_and_shows_the_history():
    lines = hl.status_lines(T0, {"heals": [T0 - 3600], "last_check": T0 - 120,
                                 "streak": {"since": T0 - 4 * MIN, "count": 2, "last": T0 - 2 * MIN}}, "restore")
    assert lines[0][0] == "WARN" and "maintenance flag set (restore)" in lines[0][1]
    assert lines[1][0] == "INFO" and "failing: 2 check(s) over 4 min" in lines[1][1]
    assert "1 heal(s) in the last 6 h" in lines[1][1]
    assert [lv for lv, _ in hl.status_lines(T0, {}, None)] == ["INFO"]


HEAL_SECTION = section(HOST / "check.sh", "# >>> heal state", "# <<< heal state")


@pytest.mark.parametrize("stub,want", [
    ('printf "INFO\\theal state: no failure streak\\n"', ["INFO"]),
    ('printf "WARN\\tmaintenance flag set\\nINFO\\theal state: x\\n"', ["WARN", "INFO"]),
    ("echo Traceback", ["FAIL"]),          # anything else is a FAIL, never a silent pass
    ("exit 1", ["FAIL"]),                  # no output: cannot show the state
])
def test_check_relays_the_heal_state_and_fails_closed(tmp_path, stub, want):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "mc-heal").write_text(f"#!/bin/bash\n{stub}\n")
    (tmp_path / "bin" / "mc-heal").chmod(0o755)
    script = (f'HOST_DIR={tmp_path}\nREPO={tmp_path}\nres() {{ printf "%s\\t%s\\n" "$1" "${{*:2}}"; }}\n'
              + HEAL_SECTION.replace("/usr/local/sbin/mc-heal", str(tmp_path / "none")))
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    assert [ln.split("\t")[0] for ln in out.splitlines()] == want


def test_mc_heal_units():
    svc, timer = unit_text("mc-heal.service"), unit_text("mc-heal.timer")
    for line in ["User=mm-mike", "Type=oneshot", "ExecStart=/usr/local/sbin/mc-heal", "StateDirectory=mc-heal",
                 "OnFailure=mc-alert@%n.service"]:
        assert line in svc.splitlines(), line
    assert "OnBootSec=10min" in timer and "OnUnitActiveSec=2min" in timer
    budget = int(re.search(r"^TimeoutStartSec=(\d+)min$", svc, re.M).group(1)) * 60
    src = (HOST / "bin" / "mc-heal").read_text()
    worst = hl.RCON_TIMEOUT + sum(int(x) for x in re.findall(r"\], (\d+), cwd=REPO\)", src)) + 60 + 60  # logs, alert
    assert worst < budget
    assert "StateDirectory=mc-host" not in svc  # the root units' dir would be chowned to mm-mike and back


def test_setup_enables_and_check_asserts_the_heal_timer():
    setup = (HOST / "setup.sh").read_text()
    assert "for t in mc-netwatch.timer mc-maint.timer mc-heal.timer; do" in section(
        HOST / "setup.sh", "# ---- 9. timers", "# ---- 10. bridge mailbox")
    check = (HOST / "check.sh").read_text()
    assert "for t in mc-netwatch.timer mc-maint.timer mc-heal.timer; do" in check
    assert "mc-heal.service" in check and setup.count("mc-heal") >= 1


@pytest.mark.parametrize("make", ["dir", "chmod000"])
def test_a_flag_that_exists_but_cannot_be_read_still_stands_mc_heal_down(heal_env, capsys, make):
    """A maintenance flag that is a directory, or unreadable (made by root), is still a flag: never probe or heal."""
    if make == "chmod000" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    s, run = heal_env
    (s / "hung").touch()
    seeded = {"last_check": T0 - 120, "heals": [],
              "streak": {"since": T0 - 6 * MIN, "count": 3, "last": T0 - 2 * MIN, "started_at": STARTED}}
    (s / "state.json").write_text(json.dumps(seeded))
    flag = Path(hl.REPO) / "maintenance"
    if make == "dir":
        flag.mkdir()
    else:
        flag.write_text("restore\n")
        flag.chmod(0)
    try:
        calls = run(0)  # a 4th failure 6 min into the streak would heal, were the flag ignored
    finally:
        flag.chmod(0o755)
    assert "skip: maintenance flag set (unreadable" in capsys.readouterr().out
    assert not calls_of(calls, "docker exec") and not calls_of(calls, "docker compose")
    assert json.loads((s / "state.json").read_text()) == seeded


def test_the_maintenance_flag_has_one_name_in_world_sh_and_mc_heal():
    world = (HOST.parent / "deploy" / "world.sh").read_text()
    assert re.search(r"^MAINT_FLAG=(\S+)$", world, re.M).group(1) == hl.MAINT_FLAG == "maintenance"
    assert "/maintenance" in (HOST.parent / ".gitignore").read_text().splitlines()


def world_ring(tmp_path, chunky_reply="[Chunky] Task started in minecraft:overworld (square, 3000)", props="-1",
               flag=None, fail_on=None):
    """world.sh ring against a stub docker; fail_on: an rcon command substring that the stub fails (exit 1)."""
    (tmp_path / "deploy").mkdir(exist_ok=True)
    shutil.copy(HOST.parent / "deploy" / "world.sh", tmp_path / "deploy" / "world.sh")
    (tmp_path / "mc_data").mkdir(exist_ok=True)
    (tmp_path / "mc_data" / "server.properties").write_text(f"pause-when-empty-seconds={props}\n")
    if flag is not None:
        (tmp_path / "maintenance").write_text(flag)
    calls = tmp_path / "calls"
    docker = f"""
echo "$*" >> {calls}
[ "$1" = ps ] && {{ echo abc123; exit 0; }}
case "$*" in
  *"{fail_on or '//never//'}"*) [ -e {tmp_path}/maintenance ] && echo "flag present at the failure" >> {calls}
     echo "rcon: connection reset" >&2; exit 1;;
esac
case "$*" in
  *"worldborder get"*) echo "The world border is currently 6000.0 block(s) wide";;
  *"chunky start"*) [ -e {tmp_path}/maintenance ] && echo "flag present at chunky start" >> {calls}; echo "{chunky_reply}";;
esac
exit 0
"""
    env = {"PATH": stub_bin(tmp_path, docker=docker), "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(tmp_path / "deploy" / "world.sh"), "ring", "overworld", "3000"], env=env,
                       capture_output=True, text=True)
    return r, (calls.read_text() if calls.exists() else ""), tmp_path / "maintenance"


def test_a_ring_sets_the_maintenance_flag_before_chunky_starts(tmp_path):
    r, calls, flag = world_ring(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "flag present at chunky start" in calls
    assert flag.read_text().startswith("ring overworld r=3000 since ")
    assert "deploy/world.sh maintenance off" in r.stdout


def test_a_refused_or_unconfirmed_ring_leaves_no_flag(tmp_path):
    r, calls, flag = world_ring(tmp_path, props="60")
    assert r.returncode == 1 and not flag.exists()
    other = tmp_path / "other"
    other.mkdir()
    r, calls, flag = world_ring(other, chunky_reply="[Chunky] Task already started")
    assert r.returncode == 1 and "NOT pre-generating" in r.stderr and not flag.exists()
    assert "flag present at chunky start" in calls  # it was set, then taken back


@pytest.mark.parametrize("fail_on", ["worldborder center", "worldborder set", "chunky world", "chunky radius",
                                     "chunky start"])
def test_a_ring_that_dies_after_setting_the_flag_takes_it_back(tmp_path, fail_on):
    """An rcon failure between the flag write and Chunky's confirmation exits under set -e: no flag may outlive it,
    or mc-heal stays off with no ring running and nobody finds out."""
    r, calls, flag = world_ring(tmp_path, fail_on=fail_on)
    assert r.returncode != 0 and "rcon: connection reset" in r.stderr
    assert "flag present at the failure" in calls  # the flag WAS set when the failure hit
    assert not flag.exists()
    if fail_on != "chunky start":
        assert "chunky start" not in calls


def test_a_ring_that_dies_keeps_a_flag_set_by_hand(tmp_path):
    r, calls, flag = world_ring(tmp_path, flag="restore since 2026-10-02\n", fail_on="worldborder set")
    assert r.returncode != 0 and "flag present at the failure" in calls
    assert flag.read_text() == "restore since 2026-10-02\n"  # not ours: never removed


def test_a_ring_keeps_a_flag_set_by_hand(tmp_path):
    r, calls, flag = world_ring(tmp_path, chunky_reply="nope", flag="restore since 2026-10-02\n")
    assert r.returncode == 1 and flag.read_text() == "restore since 2026-10-02\n"  # not ours: never removed


def test_world_maintenance_on_off(tmp_path):
    (tmp_path / "deploy").mkdir()
    shutil.copy(HOST.parent / "deploy" / "world.sh", tmp_path / "deploy" / "world.sh")

    def ws(*args):
        return subprocess.run(["bash", str(tmp_path / "deploy" / "world.sh"), "maintenance", *args],
                              capture_output=True, text=True)
    assert ws().stdout == "no maintenance flag set\n"
    assert ws("on").returncode != 0 and not (tmp_path / "maintenance").exists()  # a reason is required
    assert ws("on", "chapter 2").returncode == 0
    assert (tmp_path / "maintenance").read_text().startswith("chapter 2 since ")
    assert hl.status_lines(T0, {}, (tmp_path / "maintenance").read_text().strip())[0][0] == "WARN"
    r = ws("off")
    assert r.returncode == 0 and "cleared" in r.stdout and not (tmp_path / "maintenance").exists()
