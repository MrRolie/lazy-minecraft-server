"""Tests for mod/check_extras.py and its wiring in host/check.sh. Run: uv run --with pytest pytest mod/"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_extras", HERE / "check_extras.py")
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

JAR = b"PK\x03\x04 a jar"
SHA = hashlib.sha256(JAR).hexdigest()
NAME = "ve-commands-1.0.4.jar"
ROOT = os.geteuid() == 0


EXTRAS = {"type": "bind", "source": "@REPO@/extras", "target": "/extras", "read_only": True, "bind": {}}


def compose(mods=f"/extras/{NAME}", extras=EXTRAS):
    """A `docker compose config --format json` shape; "@REPO@" is the test repo (filled in by bind_repo)."""
    env = {"STOP_DURATION": "110"}
    if mods is not None:
        env["MODS"] = mods
    vols = [{"type": "bind", "source": "@REPO@/mc_data", "target": "/data", "bind": {}}]
    if extras is not None:
        vols.append(extras)
    return {"services": {"minecraft": {"environment": env, "volumes": vols}}}


def bind_repo(cfg, root):
    return json.loads(json.dumps(cfg).replace("@REPO@", str(root)))


def find(cfg, root):
    """findings() minus the compose-mount line (pinned by its own tests below)."""
    return [f for f in ce.findings(bind_repo(cfg, root), root) if "/extras read-only" not in f[1]]


def repo(tmp_path, pin=f"{SHA}  {NAME}\n", jar=JAR, deployed=()):
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "RELEASE.sha256").write_text(pin)
    (tmp_path / "extras").mkdir()
    if jar is not None:
        (tmp_path / "extras" / NAME).write_bytes(jar)
        (tmp_path / "extras" / NAME).chmod(0o644)
    if deployed is not None:
        (tmp_path / "mc_data" / "mods").mkdir(parents=True)
        for name, data in deployed:
            (tmp_path / "mc_data" / "mods" / name).write_bytes(data)
    return tmp_path


def states(compose_cfg, root):
    return [st for st, _ in find(compose_cfg, root)]


def test_present_pinned_and_deployed_passes(tmp_path):
    assert states(compose(), repo(tmp_path, deployed=[(NAME, JAR)])) == ["PASS", "PASS"]


def test_not_yet_booted_warns_but_does_not_fail(tmp_path):
    assert states(compose(), repo(tmp_path, deployed=None)) == ["PASS", "WARN"]
    (tmp_path / "b").mkdir()
    assert states(compose(), repo(tmp_path / "b", deployed=[])) == ["PASS", "WARN"]


def test_missing_jar_fails_with_the_boot_consequence(tmp_path):
    (st, msg), _ = find(compose(), repo(tmp_path, jar=None, deployed=[]))
    assert st == "FAIL" and "will not start" in msg


@pytest.mark.skipif(ROOT, reason="root reads anything; the uid-1000 readability check is covered below")
def test_unreadable_jar_fails_not_crashes(tmp_path):
    root = repo(tmp_path, deployed=[])
    (root / "extras" / NAME).chmod(0)
    assert states(compose(), root)[0] == "FAIL"
    (root / "extras").chmod(0)
    try:
        assert states(compose(), root)[0] == "FAIL"
    finally:
        (root / "extras").chmod(0o755)


def test_jar_uid_1000_cannot_read_fails_even_for_a_reader_who_can(tmp_path, monkeypatch):
    root = repo(tmp_path, deployed=[])
    real = os.lstat

    def as_root_600(p, *a, **k):
        st = real(p, *a, **k)
        if str(p).endswith(f"extras/{NAME}"):
            return os.stat_result((0o100600, *st[1:4], 0, 0, *st[6:]))  # root:root 600
        return st
    monkeypatch.setattr(ce.os, "lstat", as_root_600)
    (st, msg), _ = find(compose(), root)
    assert st == "FAIL" and "not readable by uid 1000" in msg


def test_wrong_bytes_fail(tmp_path):
    assert states(compose(), repo(tmp_path, jar=b"rebuilt differently", deployed=[]))[0] == "FAIL"


def test_unpinned_name_fails(tmp_path):
    assert states(compose(), repo(tmp_path, pin=f"{SHA}  ve-commands-1.0.0.jar\n")) == ["FAIL"]


def test_symlinked_jar_fails(tmp_path):
    root = repo(tmp_path, jar=None, deployed=[])
    (root / "real.jar").write_bytes(JAR)
    (root / "extras" / NAME).symlink_to(root / "real.jar")
    assert states(compose(), root)[0] == "FAIL"


def test_deployed_copy_must_match_the_pin(tmp_path):
    # a same-name same-size rebuild: itzg skips it, the old bytes keep running
    root = repo(tmp_path, deployed=[(NAME, b"PK\x03\x04 a jaR")])
    assert states(compose(), root) == ["PASS", "FAIL"]


def test_old_jar_still_deployed_fails(tmp_path):
    root = repo(tmp_path, deployed=[("ve-commands-1.0.1.jar", b"old")])
    (_, _), (st, msg) = find(compose(), root)
    assert st == "FAIL" and "up -d minecraft" in msg


def test_two_ve_commands_jars_deployed_fails(tmp_path):
    root = repo(tmp_path, deployed=[(NAME, JAR), ("ve-commands-1.0.1.jar", b"old")])
    assert "FAIL" in states(compose(), root)


def test_no_mods_or_outside_extras_fails(tmp_path):
    root = repo(tmp_path)
    assert states(compose(None), root) == ["FAIL"]
    assert states(compose("/data/ve.jar"), root) == ["FAIL"]
    assert states(compose("/extras/../x.jar"), root) == ["FAIL"]
    assert states({"error": "bad json"}, root) == ["FAIL"]


def test_main_fails_closed_and_sets_the_exit_code(tmp_path):
    root = repo(tmp_path, deployed=[(NAME, JAR)])
    run = lambda cfg, r=root: subprocess.run([sys.executable, str(HERE / "check_extras.py"), str(r)],
                                            input=cfg, capture_output=True, text=True)
    ok = run(json.dumps(bind_repo(compose(), root)))
    assert ok.returncode == 0 and ok.stdout.startswith("PASS\t")
    bad = run("not json")
    assert bad.returncode == 1 and bad.stdout.startswith("FAIL\t")
    crash = run(json.dumps(bind_repo(compose(), root)), r=tmp_path / "no-such-repo")
    assert crash.returncode == 1 and crash.stdout.startswith("FAIL\t")


def test_the_repo_pin_names_the_jar_compose_deploys():
    compose_text = (HERE.parent / "docker-compose.yml").read_text()
    pins = (HERE / "RELEASE.sha256").read_text().split()
    name = pins[1]
    assert f'MODS: "/extras/{name}"' in compose_text
    assert len(pins) == 2 and len(pins[0]) == 64


# ---- host/check.sh wiring: the block between "# >>> extras" and "# <<< extras", run as check.sh runs it ----

CHECK_SH = (HERE.parent / "host" / "check.sh").read_text()


def extras_block():
    m = re.search(r"^# >>> extras.*?^# <<< extras\n", CHECK_SH, re.S | re.M)
    assert m, "host/check.sh lost its extras block"
    return m.group(0)


def run_block(tmp_path, root, checker=None, compose_rc=0):
    """Runs the block with a stub `docker` (prints the compose JSON) and check.sh's own res(); returns (FAILS, out)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(f"#!/bin/sh\ncat <<'J'\n{json.dumps(bind_repo(compose(), root))}\nJ\nexit {compose_rc}\n")
    (bin_dir / "docker").chmod(0o755)
    (root / "mod").mkdir(exist_ok=True)
    if checker is None:
        shutil.copy(HERE / "check_extras.py", root / "mod" / "check_extras.py")
    else:
        (root / "mod" / "check_extras.py").write_text(checker)
    res = re.search(r"^res\(\) \{.*?^\}\n", CHECK_SH, re.S | re.M).group(0)
    header = re.search(r"^set -.*$", CHECK_SH, re.M).group(0)  # check.sh's own shell options, not ours
    script = f"{header}\nREPO={root}\nFAILS=0\n{res}{extras_block()}echo \"FAILS=$FAILS\"\n"
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    fails = int(re.search(r"FAILS=(\d+)", p.stdout).group(1))
    return fails, p.stdout


def test_wiring_passes_a_healthy_jar(tmp_path):
    (tmp_path / "r").mkdir()
    fails, out = run_block(tmp_path, repo(tmp_path / "r", deployed=[(NAME, JAR)]))
    assert fails == 0 and out.count("PASS") == 3, out


@pytest.mark.skipif(ROOT, reason="root reads anything")
def test_wiring_counts_an_unreadable_jar_as_a_fail(tmp_path):
    (tmp_path / "r").mkdir()
    root = repo(tmp_path / "r", deployed=[])
    (root / "extras" / NAME).chmod(0)
    fails, out = run_block(tmp_path, root)
    assert fails >= 1, out


@pytest.mark.parametrize("checker", [
    "raise SystemExit(0)\n",                                  # silent, exit 0
    "raise SystemExit(3)\n",                                  # silent, exit 3
    "import sys\nprint('PASS\\tlooks fine')\nraise RuntimeError('boom')\n",  # a PASS, then a crash (traceback)
    "print('PASS\\tok')\nraise SystemExit(1)\n",              # a PASS, then a non-zero exit with no FAIL line
    "print('all good')\n",                                    # unparseable output
])
def test_wiring_fails_closed_on_a_broken_checker(tmp_path, checker):
    (tmp_path / "r").mkdir()
    fails, out = run_block(tmp_path, repo(tmp_path / "r", deployed=[(NAME, JAR)]), checker=checker)
    assert fails >= 1, out


def test_wiring_fails_when_compose_config_fails(tmp_path):
    (tmp_path / "r").mkdir()
    fails, out = run_block(tmp_path, repo(tmp_path / "r", deployed=[(NAME, JAR)]), compose_rc=1)
    assert fails >= 1, out


# ---- the compose mount (rv3 M8) and the pin/MODS pairing ----

def mount(cfg_extras, tmp_path):
    root = repo(tmp_path, deployed=[(NAME, JAR)])
    return [f for f in ce.findings(bind_repo(compose(extras=cfg_extras), root), root) if "/extras" in f[1] and "MODS" not in f[1]]


def test_the_extras_bind_read_only_passes(tmp_path):
    assert [st for st, _ in mount(EXTRAS, tmp_path)] == ["PASS"]


@pytest.mark.parametrize("bad", [
    None,                                                    # the volume dropped
    {**EXTRAS, "read_only": False},                          # mounted read-write
    {k: v for k, v in EXTRAS.items() if k != "read_only"},   # read_only absent
    {**EXTRAS, "source": "@REPO@/extra"},                    # a mistyped source
    {**EXTRAS, "target": "/extra"},                          # a mistyped target
    {**EXTRAS, "type": "volume"},                            # a named volume, not the bind
])
def test_a_missing_or_wrong_extras_mount_fails(tmp_path, bad):
    (st, msg), = mount(bad, tmp_path)
    assert st == "FAIL", msg


def test_mods_must_name_the_pinned_file(tmp_path):
    root = repo(tmp_path, pin=f"{SHA}  ve-commands-9.9.9.jar\n", deployed=[(NAME, JAR)])
    assert "FAIL" in states(compose(), root)


def test_a_non_string_mods_fails(tmp_path):
    assert states({"services": {"minecraft": {"environment": {"MODS": ["/extras/x.jar"]}}}}, repo(tmp_path)) == ["FAIL"]


def test_main_turns_any_crash_inside_findings_into_a_fail(monkeypatch, capsys):
    class Boom(BaseException):  # not an Exception: only `except BaseException` catches it
        pass

    def explode(*a):
        raise Boom("simulated")
    monkeypatch.setattr(ce, "findings", explode)
    monkeypatch.setattr(sys, "argv", ["check_extras.py", "/nowhere"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{}"))
    with pytest.raises(SystemExit) as e:
        ce.main()
    assert e.value.code == 1
    assert capsys.readouterr().out.startswith("FAIL\tcheck_extras crashed")
