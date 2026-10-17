"""pack/spike.sh against a fake docker: PASS only on "Done (" with ve-commands loaded AND nothing left behind; a
teardown on every path; never next to the real server. Its overlay is read through Compose (the docker CLI). The live
proof is the run in docs/kit/CUSTOM-PACK.md. Run: uv run --with pytest pytest pack/"""
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parent
ROOT = PACK.parent
LIVE = "sky-isles-1.2.0-ch1.mrpack"
DONE_LOG = ("minecraft-1  | [main/INFO]: Loading 9 mods:\n"
            "minecraft-1  | \t- chunky 1.5.3\n"
            "minecraft-1  | \t- fabric-api 0.155.3+26.1.2\n"
            "minecraft-1  | \t   \\-- fabric-api-base 1.0.0\n"
            "minecraft-1  | \t- ve-commands 1.0.4\n"
            "minecraft-1  | [Server thread/INFO]: Done (3.210s)! For help, type \"help\"\n")

# $1.. = docker's argv. State lives in files under $SPIKE_T: calls (every call), log, state, real, left.
DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$SPIKE_T/calls"
if [ "$1" = compose ]; then
  case " $* " in
    *" up -d minecraft "*)  # what the server would find at its boot: a seeded world, and a --pack override
      [ -f "$MC_SPIKE_DATA/world/level.dat" ] && echo "world seeded: $(cat "$MC_SPIKE_DATA/world/level.dat")" >> "$SPIKE_T/calls"
      [ -e "$MC_SPIKE_DATA/dist" ] || [ -e "$MC_SPIKE_DATA/provenance" ] && echo "pack record unpacked" >> "$SPIKE_T/calls"
      for f in spike/*.pack.yml; do [ -f "$f" ] && cat "$f" >> "$SPIKE_T/pack_ovr"; done
      exit 0 ;;
    *" logs "*) cat "$SPIKE_T/log" 2>/dev/null; exit 0 ;;
    *" ps -a "*) cat "$SPIKE_T/state" 2>/dev/null; exit 0 ;;
    *" down "*) exit 0 ;;
  esac
  exit 0
fi
if [ "$1" = ps ] && [ "$2" = -q ]; then cat "$SPIKE_T/real" 2>/dev/null; exit 0; fi
if [ "$1" = ps ] && [ "$2" = -aq ]; then cat "$SPIKE_T/left" 2>/dev/null; exit 0; fi
exit 0
"""


@pytest.fixture
def spike(tmp_path):
    repo = tmp_path / "checkout"
    for rel in ("pack/spike.sh", "pack/spike.compose.yml", "pack/build.py", "kit/kitconfig.py", "docker-compose.yml"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, repo / rel)
    (repo / "docker-compose.override.yml").write_text(f'      MODRINTH_MODPACK: "/pack/{LIVE}"\n')
    (repo / "dist").mkdir()
    (repo / "dist" / LIVE).write_bytes(b"PK")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(DOCKER)
    (bin_ / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "SPIKE_T": str(tmp_path), "MC_SPIKE_TIMEOUT": "3"}

    def run(log=DONE_LOG, state="running", real="", left="", args=(), mode=None):
        for name, text in (("log", log), ("state", state), ("real", real), ("left", left)):
            (tmp_path / name).write_text(text)
        (tmp_path / "calls").write_text("")
        (tmp_path / "pack_ovr").write_text("")
        run_env = dict(env)
        if mode is not None:  # kit.toml for pack/build.py --mode: the tests' example config with this pack.mode
            kit = tmp_path / "kit.toml"
            text = (ROOT / "kit" / "fixtures" / "example.toml").read_text()
            assert text.count('[pack]\nmode = "vanilla-extended"') == 1
            kit.write_text(text.replace('[pack]\nmode = "vanilla-extended"', f'[pack]\nmode = "{mode}"'))
            run_env["MC_KIT_TOML"] = str(kit)
        r = subprocess.run(["bash", str(repo / "pack" / "spike.sh"), *args], env=run_env, capture_output=True,
                           text=True)
        return r, (tmp_path / "calls").read_text().splitlines()
    run.pack_ovr = lambda: (tmp_path / "pack_ovr").read_text()
    run.repo = repo
    return run


def _compose(calls, verb):
    return [c for c in calls if c.startswith("compose ") and f" {verb}" in c]


def test_a_boot_that_reaches_done_with_ve_commands_and_leaves_nothing_passes(spike):
    r, calls = spike()
    assert r.returncode == 0, r.stderr
    assert f"spike: PASS: dist/{LIVE} reached \"Done (3.210s)!" in r.stdout and "(Loading 9 mods)" in r.stdout
    assert "Fabric loaded: chunky 1.5.3 fabric-api 0.155.3+26.1.2 ve-commands 1.0.4" in r.stdout
    (up,) = _compose(calls, "up")
    project = up.split()[2]
    assert project.startswith("mc-spike-") and project != "minecraft-ai-host"
    files = up.split(" -f ")[1:]
    assert [f.split()[0] for f in files] == ["docker-compose.yml", "docker-compose.override.yml",
                                             "pack/spike.compose.yml"]  # same env as the real server, then the spike
    assert _compose(calls, "down") and not (spike.repo / "spike").exists()


@pytest.mark.parametrize("log,state,says", [
    ("minecraft-1  | crash\n", "exited", "did not boot"),     # the server died
    ("minecraft-1  | still loading\n", "running", "did not boot"),  # never Done before the timeout
    (DONE_LOG.replace("\t- ve-commands 1.0.4\n", ""), "running", "ve-commands is not in Fabric's list"),
])
def test_a_boot_that_fails_is_a_fail_and_is_still_torn_down(spike, log, state, says):
    r, calls = spike(log=log, state=state)
    assert r.returncode == 1 and says in r.stderr
    assert _compose(calls, "down") and not (spike.repo / "spike").exists()


def test_two_spikes_started_in_the_same_second_never_share_a_project(spike, tmp_path):
    """The project, its data dir and its pack override are named from the start time: to the second, two spikes
    shared all three, and one's teardown removed the other's container and world copy (codex review 2026-10-16)."""
    date = tmp_path / "bin" / "date"
    date.write_text('#!/bin/sh\n[ "$1" = +%Y%m%d%H%M%S ] && { echo 20261016120000; exit 0; }\n'
                    'exec /usr/bin/date "$@"\n')  # pins only the project's stamp: the boot timer runs on real time
    date.chmod(0o755)
    projects = set()
    for _ in range(2):
        r, calls = spike()
        assert r.returncode == 0, r.stderr
        (up,) = _compose(calls, "up")
        projects.add(up.split()[2])
    assert len(projects) == 2 and all(p.startswith("mc-spike-20261016120000-") for p in projects), projects


def test_anything_left_behind_is_a_fail(spike):
    r, calls = spike(left="abc123\n")
    assert r.returncode == 1 and "the spike left these behind (remove them by hand): abc123" in r.stderr


# ---- --if-custom: KIT.md P5's gate on every kit (review 2026-10-16: a custom-only block could be skipped) ----------

def test_if_custom_skips_vanilla_extended_and_boots_nothing(spike):
    r, calls = spike(args=["--if-custom"], mode="vanilla-extended")
    assert r.returncode == 0, r.stderr
    assert r.stdout == ("spike: skipped: kit.toml pack.mode is vanilla-extended, not custom; P8's first start is "
                        "that pack's boot\n")
    assert calls == []


def test_if_custom_boots_a_custom_pack(spike):
    r, calls = spike(args=["--if-custom"], mode="custom")
    assert r.returncode == 0, r.stderr
    assert f"spike: PASS: dist/{LIVE} reached" in r.stdout and _compose(calls, "up")


def test_if_custom_refuses_a_custom_pack_render_has_not_pointed_compose_at(spike):
    """Without the override compose runs Vanilla Extended: a PASS would be for the wrong pack."""
    (spike.repo / "docker-compose.override.yml").unlink()
    r, calls = spike(args=["--if-custom"], mode="custom")
    assert r.returncode == 1 and "docker-compose.override.yml is missing" in r.stderr and calls == []


def test_if_custom_refuses_when_it_cannot_read_the_mode(spike):
    r, calls = spike(args=["--if-custom"])  # no kit.toml in the checkout
    assert r.returncode == 1 and "cannot read pack.mode from kit.toml" in r.stderr and calls == []


@pytest.mark.parametrize("mode", ["vanilla-extended", "custom"])
@pytest.mark.parametrize("order", [["--if-custom", "--pack", LIVE], ["--pack", LIVE, "--if-custom"]])
def test_if_custom_and_pack_together_are_refused(spike, mode, order):
    """--if-custom decides from kit.toml whether to boot; --pack says to boot a named pack. Together, kit.toml was
    never read and a Vanilla Extended kit booted a spike instead of "skipped" (codex review 2026-10-16)."""
    r, calls = spike(args=order, mode=mode)
    assert r.returncode == 1 and "--if-custom and --pack" in r.stderr and calls == [], r.stdout + r.stderr


# ---- --pack and --world: a custom pack's later change, rehearsed before compose runs it ----------------------------

NEW = "sky-isles-1.3.0-ch1.mrpack"


def test_pack_boots_the_named_file_and_leaves_the_live_override_alone(spike):
    """The live override is what mc-server.service boots after a power cut: it must still name the old pack while
    the new one is only a candidate (review 2026-10-16)."""
    (spike.repo / "dist" / NEW).write_bytes(b"PK")
    live = (spike.repo / "docker-compose.override.yml").read_text()
    r, calls = spike(args=["--pack", NEW])
    assert r.returncode == 0, r.stderr
    assert f"spike: PASS: dist/{NEW} reached" in r.stdout
    (up,) = _compose(calls, "up")
    files = [f.split()[0] for f in up.split(" -f ")[1:]]
    assert files[:2] == ["docker-compose.yml", "docker-compose.override.yml"] and files[3] == "pack/spike.compose.yml"
    assert re.fullmatch(r"spike/mc-spike-\d+-\d+\.pack\.yml", files[2])  # after the live override: its pack wins
    assert spike.pack_ovr() == f'services:\n  minecraft:\n    environment:\n      MODRINTH_MODPACK: "/pack/{NEW}"\n'
    assert (spike.repo / "docker-compose.override.yml").read_text() == live
    assert not (spike.repo / "spike").exists()  # the spike's override went with it


@pytest.mark.parametrize("arg,says", [(NEW, f"dist/{NEW} is missing"),
                                      ("../x.mrpack", "--pack takes a file name in dist/"),
                                      ("x.zip", "--pack takes a file name in dist/")])
def test_pack_refuses_a_file_it_cannot_boot(spike, arg, says):
    r, calls = spike(args=["--pack", arg])
    assert r.returncode == 1 and says in r.stderr and not _compose(calls, "up")


def backup_tar(path, level="world"):
    """A deploy/backup.sh tar: the world, config/, server.properties, and the pack record and provenance."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in ((f"{level}/level.dat", b"friends-world"), ("config/chunky/config.json", b"{}"),
                           ("server.properties", b"motd=x\n"), (f"dist/{LIVE}", b"PK"),
                           ("provenance/build.txt", b"MODS=x\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_world_seeds_the_spike_with_a_copy_of_the_backup(spike, tmp_path):
    """A mod that boots on a fresh world can still fail on the friends' saved data: the rehearsal loads a copy."""
    (spike.repo / "dist" / NEW).write_bytes(b"PK")
    tar = backup_tar(tmp_path / "world-20261016-040000.tar.gz")
    r, calls = spike(args=["--pack", NEW, "--world", str(tar)])
    assert r.returncode == 0, r.stderr
    assert "world seeded: friends-world" in calls and "pack record unpacked" not in calls
    assert f"a throwaway copy of the world in {tar}" in r.stdout
    assert tar.exists() and not (spike.repo / "spike").exists()  # the copy is gone, the backup is not


def test_world_refuses_a_tar_without_the_world(spike, tmp_path):
    tar = backup_tar(tmp_path / "other.tar.gz", level="elsewhere")
    r, calls = spike(args=["--world", str(tar)])
    assert r.returncode == 1 and "holds no world/level.dat" in r.stderr and not _compose(calls, "up")
    assert not (spike.repo / "spike").exists()
    r, calls = spike(args=["--world", str(tmp_path / "nope.tar.gz")])
    assert r.returncode == 1 and "is not a file" in r.stderr


def test_it_refuses_next_to_the_running_server(spike):
    r, calls = spike(real="def456\n")
    assert r.returncode == 1 and "the real server is running" in r.stderr
    assert not _compose(calls, "up")


def test_the_overlay_keeps_the_servers_env_but_no_port_no_restart_and_not_its_world():
    """Read through Compose itself (the docker CLI, not the daemon; it FAILS without it): the spike's minecraft is the
    real one's image and environment, with no published port, no restart, and only a spike/ dir as /data."""
    assert shutil.which("docker"), "docker CLI missing: the overlay can't be checked"
    d = Path(tempfile.mkdtemp(prefix="test-compose-spike-", dir=ROOT))  # snap Docker cannot read /tmp
    try:
        shutil.copy(ROOT / "docker-compose.yml", d / "docker-compose.yml")
        (d / "pack").mkdir()
        shutil.copy(ROOT / "pack" / "spike.compose.yml", d / "pack" / "spike.compose.yml")
        (d / ".env").write_text("RCON_PASSWORD=stub-rcon\nLEVEL=world\n")

        def config(*files):
            r = subprocess.run(["docker", "compose", "-p", "mc-spike-test", *sum((["-f", f] for f in files), []),
                                "config", "--format", "json"], cwd=d, capture_output=True, text=True,
                               env={**os.environ, "MC_SPIKE_DATA": "./spike/mc-spike-test"})
            assert r.returncode == 0, r.stderr
            return json.loads(r.stdout)["services"]["minecraft"]
        real = config("docker-compose.yml")
        spike = config("docker-compose.yml", "pack/spike.compose.yml")
    finally:
        shutil.rmtree(d)
    assert real["ports"] and real["restart"] == "unless-stopped"  # what the overlay takes away
    assert not spike.get("ports") and spike["restart"] == "no"
    assert spike["image"] == real["image"] and spike["environment"] == real["environment"]
    assert [(v["source"][len(str(d)):], v["target"]) for v in spike["volumes"]] == [
        ("/spike/mc-spike-test", "/data"), ("/dist", "/pack"), ("/extras", "/extras")]


def test_the_procedure_names_files_that_exist_and_the_gate_this_script_is():
    """docs/kit/CUSTOM-PACK.md is what the person's Claude follows: every tracked path it names exists, its spike
    gate runs this script under sg docker, and it states the claim the operator ruled (2026-10-14)."""
    doc = (ROOT / "docs" / "kit" / "CUSTOM-PACK.md").read_text()
    named = set(re.findall(r"`((?:pack|kit|mod|deploy|discord|docs)/[\w./-]+\.(?:py|sh|md|json|toml))`", doc))
    named = (named | set(re.findall(r"cp (pack/[\w./-]+) ", doc))) - {"kit/probe.json"}  # P0's output, untracked
    assert "pack/spike.sh" in named and all((ROOT / p).is_file() for p in named), named
    assert "python3 kit/record.py gate P5 -- sg docker -c 'bash pack/spike.sh --if-custom'" in doc
    assert "python3 kit/record.py gate P5 -- sg docker -c 'bash pack/spike.sh --if-custom'" in (
        ROOT / "docs" / "kit" / "KIT.md").read_text()  # the same gate, in the runbook
    assert ('**"passes the build/lint gates and booted once in a recorded test; not the path the kit\'s proof run '
            'tested."**') in " ".join(doc.split())


CUSTOM_DOC = (ROOT / "docs" / "kit" / "CUSTOM-PACK.md").read_text()


def change_problems(doc):
    """'Changing the pack later' (review 2026-10-16): the override mc-server.service boots names the new pack only
    after that pack booted on a copy of the friends' world, and mc-heal stands down while the server is stopped."""
    later = doc[doc.index("## Changing the pack later"):]
    order = ["bash deploy/backup.sh", 'bash deploy/world.sh maintenance on "pack change"', "`python3 pack/build.py`",
             "bash pack/spike.sh --pack <new file> --world backups/world-<time>.tar.gz",
             "If it passes: `python3 kit/render.py`", "bash deploy/world.sh maintenance off"]
    at = [later.find(s) for s in order]
    problems = [f"missing: {s}" for s, i in zip(order, at) if i < 0]
    if not problems and at != sorted(at):
        problems.append(f"out of order: {at}")
    if "The spike is the rehearsal" in later or "Do **not** run `kit/render.py` yet" not in later:
        problems.append("render can run before the rehearsal")
    if "tar -xzf backups/world-<time>.tar.gz pack-custom/mods.toml pack-custom/lock.json" not in later:
        problems.append("a failed change has no way back to the old pack's source")
    return problems


def test_a_later_pack_change_rehearses_on_a_copy_before_compose_runs_it():
    assert change_problems(CUSTOM_DOC) == []


@pytest.mark.parametrize("old,new", [
    ("3. `python3 pack/build.py`. It ends with `written dist/<new file>`. Do **not** run `kit/render.py` yet: the\n"
     "   override still names the old pack.",
     "3. `python3 pack/build.py`, then `python3 kit/render.py`."),
    (" --world backups/world-<time>.tar.gz", ""),
    ('`bash deploy/world.sh maintenance on "pack change"`', "nothing"),
])
def test_the_change_pin_catches_its_mutant(old, new):
    assert CUSTOM_DOC.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert change_problems(CUSTOM_DOC.replace(old, new))


def test_the_modrinth_commands_let_curl_encode_the_brackets():
    """Bare brackets in a curl URL are curl's own glob patterns: exit 3, and under -s nothing at all, which reads as
    "no version" (verified against Modrinth 2026-10-16; the encoded form returned AppleSkin's 26.1.2 build)."""
    curls = [ln.strip() for ln in CUSTOM_DOC.splitlines() if ln.strip().startswith("curl ")]
    assert len(curls) == 2
    for c in curls:
        assert c.startswith('curl -sfG -A "minecraft-ai-host kit (custom pack)" --data-urlencode ')
        assert "[" not in c.split()[-1] and "?" not in c.split()[-1]  # nothing to glob in the URL itself
    assert "A non-zero exit is an error" in CUSTOM_DOC and "an empty list means no" not in CUSTOM_DOC


def test_it_refuses_without_the_pack_compose_runs(spike):
    (spike.repo / "dist" / LIVE).unlink()
    r, calls = spike()
    assert r.returncode == 1 and f"dist/{LIVE} is missing" in r.stderr and not _compose(calls, "up")
