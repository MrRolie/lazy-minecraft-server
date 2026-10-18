"""Tests for docs/kit/KIT.md itself (and the two pages beside it). Run: uv run --with pytest pytest kit/

KIT.md is a procedure Claude copies commands out of, so its commands are held to the code: every command that runs
a file of this repo names a file that exists here, with subcommands and flags that file's own parser accepts
(argparse, run for real up to the parse; a shell script's own `case` arms). The stops the verifiers asked for (the
backups before each server-side change, never a bare `up -d`, the docker group until the reboot, the P3 gate that
can fail, the retro logging of steps 1-6) are pinned, and each pin has a mutant of the page that must fail it.
"""
import importlib.util
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import record

KIT = Path(__file__).resolve().parent
REPO = KIT.parent
TEXT = record.KIT_MD.read_text()
STEPS_TEXT = record.STEPS_MD.read_text()
README = KIT / "README.md"
DOCS = {"docs/kit/KIT.md": TEXT, "docs/kit/HUMAN-STEPS.md": STEPS_TEXT, "kit/README.md": README.read_text()}


def section(head, text=TEXT):
    """The text from the '## <head>' heading to the next '## ' heading."""
    start = text.index(f"\n## {head}")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def fences(text):
    """Every ```sh block in text, dedented (a block inside a list item is indented as a whole)."""
    return [textwrap.dedent(m.group(2)) for m in re.finditer(r"^( *)```sh\n(.*?)^\1```", text, re.M | re.S)]


def prose(text):
    """text with the fenced blocks removed."""
    return re.sub(r"^( *)```sh\n.*?^\1```", "", text, flags=re.M | re.S)


def inline(text):
    """Every inline code span outside the fences (a span never crosses a line: test_no_code_span_crosses_a_line)."""
    return [m.group(1) for line in prose(text).splitlines() for m in re.finditer(r"`([^`]+)`", line)]


def commands(text):
    """Every shell text the page gives: whole fenced blocks, and inline spans."""
    return fences(text) + inline(text)


# ---- every command names a file that exists, with flags it accepts ------------------------------------------------

INVOKE = re.compile(r"(?<![\w/.-])(?:sudo\s+)?(?:bash|python3)\s+(?:~/minecraft-ai-host/)?"
                    r"((?:kit|host|deploy|discord|pack|mod)(?:/[\w.-]+)+\.(?:py|sh))")
ARG = re.compile(r"[ \t]+(\"[^\"\n]*\"|'[^']*'|[^\s;&|'\"()<>#`]+)")


def invocations(text):
    """-> [(path, [args])] for every repo file a command runs. Args stop at a shell operator, a redirect, a comment
    or a closing quote of an enclosing string; quoted args are unquoted."""
    out = []
    for cmd in commands(text):
        for m in INVOKE.finditer(cmd):
            args, pos = [], m.end()
            while (a := ARG.match(cmd, pos)):
                tok = a.group(1)
                args.append(tok[1:-1] if tok[:1] in "\"'" else tok)
                pos = a.end()
            out.append((m.group(1), args))
    return out


TRACKED = set(subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
                             check=True).stdout.splitlines())

PARSE_ONLY = r'''
import argparse, os, runpy, sys
real = argparse.ArgumentParser.parse_args
def parse_only(self, args=None, namespace=None):
    real(self, args, namespace)
    raise SystemExit(0)  # it parsed: stop before the tool does anything
argparse.ArgumentParser.parse_args = parse_only
path = sys.argv[1]
sys.path.insert(0, os.path.dirname(path))
sys.argv = [path] + sys.argv[2:]
runpy.run_path(path, run_name="__main__")
'''


def python_accepts(path, args):
    """The tool's own argparse parses args (exit 0) or refuses them (exit 2); nothing past the parse runs."""
    r = subprocess.run([sys.executable, "-c", PARSE_ONLY, str(REPO / path), *args], cwd=REPO, capture_output=True,
                       text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    return r.returncode == 0, r.stderr


def shell_accepts(path, args):
    """Each flag, and the first positional (a subcommand), is an arm of one of the script's `case` statements; a
    script that reads no arguments takes none. A flag whose arm shifts 2 takes the next token as its value."""
    src = (REPO / path).read_text()
    if not args:
        return True, ""
    if not any(s in src for s in ('"${1', '"$1"', "$#", '"$@"')):
        return False, f"{path} reads no arguments, but is given {args}"

    def arm(tok):
        return re.search(rf"(?m)^\s*(?:[^()\n]*\|\s*)?{re.escape(tok)}\s*(?:\|[^()\n]*)?\)(.*)$", src)
    positional, skip = [], False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            m = arm(tok)
            if not m:
                return False, f"{path} has no case arm for {tok}"
            skip = "shift 2" in m.group(1) or "${2" in m.group(1)
        else:
            positional.append(tok)
    if positional and not arm(positional[0]):
        return False, f"{path} has no case arm for {positional[0]}"
    return True, ""


def accepts(path, args):
    return (python_accepts if path.endswith(".py") else shell_accepts)(path, args)


def bad_invocations(text):
    bad = []
    for path, args in invocations(text):
        if path not in TRACKED:
            bad.append(f"{path}: not a file on this branch")
            continue
        ok, why = accepts(path, args)
        if not ok:
            bad.append(f"{path} {' '.join(args)}: {why.strip().splitlines()[-1] if why.strip() else 'refused'}")
    return bad


def test_the_pages_run_the_built_tools():
    """The reconciliation's floor: each phase's tool is the one the lanes built, not a by-hand stand-in."""
    found = {p for p, _ in invocations(TEXT)}
    for tool in ("kit/probe.sh", "kit/packages.sh", "kit/sudoers.sh", "kit/tailscale.py", "discord/bootstrap.py",
                 "discord/sync.py", "kit/render.py", "host/setup.sh", "host/check.sh", "mod/build.sh",
                 "pack/build.py", "deploy/backup.sh", "deploy/world.sh", "kit/record.py"):
        assert tool in found, tool
    steps = {(p, tuple(a)) for p, a in invocations(TEXT)}
    for sub in ("discover", "intents", "clean", "webhook", "invite"):
        assert any(p == "discord/bootstrap.py" and a[:1] == (sub,) for p, a in steps), sub
    assert ("host/setup.sh", ("--stand-up",)) in steps
    assert ("kit/tailscale.py", ("status",)) in steps and ("kit/tailscale.py", ("setup", "--apply")) in steps


@pytest.mark.parametrize("doc", sorted(DOCS))
def test_every_command_names_a_file_on_this_branch_with_flags_it_accepts(doc):
    assert invocations(DOCS[doc]) or doc == "kit/README.md"
    assert bad_invocations(DOCS[doc]) == []


@pytest.mark.parametrize("old,new,why", [
    ("sudo bash host/setup.sh --stand-up", "sudo bash host/setup.sh --standup", "no case arm for --standup"),
    ("python3 discord/bootstrap.py discover\n", "python3 discord/bootstrap.py discovr\n", "invalid choice"),
    ("python3 kit/tailscale.py setup --apply", "python3 kit/tailscale.py setup --aply", "unrecognized arguments"),
    ("bash kit/probe.sh", "bash kit/probe2.sh", "not a file on this branch"),
    ("sudo bash kit/packages.sh", "sudo bash kit/packages.sh --yes", "kit/packages.sh --yes"),
    ("python3 kit/render.py --check --env-only", "python3 kit/render.py --check --env-only --host-only",
     "not allowed with argument"),
    ("bash kit/sudoers.sh check", "bash kit/sudoers.sh verify", "no case arm for verify"),
])
def test_the_command_check_fails_on_a_wrong_file_or_flag(old, new, why):
    assert TEXT.count(old) >= 1, f"stale mutation pattern {old!r}"
    bad = bad_invocations(TEXT.replace(old, new, 1))
    assert any(why in b for b in bad), bad


def _sync_module():
    spec = importlib.util.spec_from_file_location("kit_md_sync", REPO / "discord" / "sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_sync_name_kit_md_uses_exists():
    """A python -c gate that calls sync.<name> breaks silently when the name is gone (load_kit() was)."""
    sync = _sync_module()
    names = set(re.findall(r"\bsync\.([A-Za-z_]\w*)", TEXT)) - {"py"}
    assert names and [n for n in sorted(names) if not hasattr(sync, n)] == []
    assert not hasattr(sync, "load_kit")  # the name the old P6 and P12 blocks called


@pytest.mark.parametrize("doc", sorted(DOCS))
def test_no_code_span_crosses_a_line(doc):
    """inline() reads spans line by line, so a span broken across two lines would hide its command."""
    odd = [ln for ln in prose(DOCS[doc]).splitlines() if ln.count("`") % 2]
    assert odd == []


def test_the_api_calls_go_through_the_built_modules_only():
    """The ad-hoc dapi/tsapi helpers are gone: tsapi printed response bodies (the share link with them)."""
    for gone in ("dapi", "tsapi", "api.tailscale.com", "discord.com/api", "urllib.request", "DEV="):
        assert gone not in TEXT, gone


# ---- docker before the P10 reboot goes through sg docker ---------------------------------------------------------

DOCKER_CMD = re.compile(r"(?<![\w/-])docker (?:compose|ps|exec|run|info|build|logs|stop|start|inspect)\b"
                        r"|deploy/(?:backup|world)\.sh|mod/build\.sh")
COMMAND_SPAN = re.compile(r"^(?:sudo |bash |sh |docker |python3 )")


def bare_docker(text):
    """Lines of ```sh blocks, and inline spans that are commands, that need the docker group but do not get it
    through `sg docker -c`."""
    lines = [line for b in fences(text) for line in b.splitlines()]
    lines += [s for s in inline(text) if COMMAND_SPAN.match(s)]
    return [line for line in lines if DOCKER_CMD.search(line) and "sg docker -c" not in line]


def until_the_reboot():
    """From P2 (docker installed, the group added) to P10's reboot: the kit user's shell has no docker group."""
    p10 = TEXT.index("\n## P10.")
    return TEXT[TEXT.index("\n## P2."):TEXT.index("sudo systemctl reboot", p10)]


def test_the_lint_flags_the_old_p10_backup_and_the_old_p9_inline_logs():
    old = "## P10. Arm\n\n```sh\npython3 kit/record.py begin P10\nbash deploy/backup.sh\n```\n"
    assert bare_docker(old) == ["bash deploy/backup.sh"]
    old_p9 = 'wait for `Done (` in `docker compose logs --since "$t" minecraft` (the log)\n'
    assert bare_docker(old_p9) == ['docker compose logs --since "$t" minecraft']
    assert bare_docker("```sh\nsg docker -c 'bash deploy/backup.sh'\n```\n") == []
    assert bare_docker("`mod/build.sh` never deletes a jar\n") == []  # a file named in prose is not a command


def test_every_docker_command_before_the_reboot_goes_through_sg_docker():
    assert bare_docker(until_the_reboot()) == []


# ---- the compose rule: never a bare up -d -------------------------------------------------------------------------

BARE_UP = re.compile(r"docker compose up -d(?![ \t]+(?:minecraft\b|--no-build[ \t]+bridge\b))")


def compose_rule_problems(text):
    problems = []
    if "**Never a bare `docker compose up -d`**, and never `docker compose restart`." not in text:
        problems.append("the rule line is gone")
    problems += [f"bare up -d: {c!r}" for c in commands(text) if BARE_UP.search(c)
                 and c != "docker compose up -d"]  # the rule itself names the bare form
    if re.search(r"docker compose restart(?!`)", "\n".join(fences(text))):
        problems.append("a restart in a command")
    return problems


def test_kit_md_never_runs_a_bare_up_d():
    assert compose_rule_problems(TEXT) == []


@pytest.mark.parametrize("old,new", [
    ("**Never a bare `docker compose up -d`**, and never `docker compose restart`.", "Prefer named services."),
    ("sg docker -c 'docker compose up -d minecraft'\n", "sg docker -c 'docker compose up -d'\n"),
    ("docker compose up -d --no-build bridge\n", "docker compose up -d\n"),
])
def test_the_compose_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert compose_rule_problems(TEXT.replace(old, new))


# ---- backups before each server-side change (P9) and before arming (P10) ------------------------------------------

BACKUP_GATE = "sg docker -c 'bash deploy/backup.sh'"


def p9_backup_problems(text):
    """Each P9 gate that changes the server (world.sh init; a new container) must come right after a backup gate."""
    gates = [re.sub(r"\s{2,}#.*$", "", ln) for ln in "\n".join(fences(section("P9.", text))).splitlines()
             if "kit/record.py gate P9" in ln]  # a trailing comment is not the command
    problems = []
    for i, ln in enumerate(gates):
        if "world.sh init" in ln or "up -d minecraft" in ln:
            prev = [g for g in gates[:i] if "tar -tzf" not in g]  # the tar listing reads, it changes nothing
            if not prev or BACKUP_GATE not in prev[-1]:
                problems.append(ln)
    if not any("world.sh init" in g for g in gates) or not any("up -d minecraft" in g for g in gates):
        problems.append("the init or the restart gate is gone")
    return problems


def test_p9_backs_up_before_world_init_and_before_each_restart():
    assert p9_backup_problems(TEXT) == []
    p9 = section("P9.")
    assert "the same two gates again (a fresh backup, then `up -d minecraft`" in p9  # the restart back to 60


@pytest.mark.parametrize("old,new", [
    # the abandoned run's order: init first, then the backup
    ("python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/backup.sh'        # the first backup: before "
     "world.sh init\npython3 kit/record.py gate P9 -- sh -c 'tar -tzf \"$(ls -t backups/world-*.tar.gz | head -1)\" "
     ">/dev/null'\npython3 kit/record.py gate P9 -- sg docker -c 'bash deploy/world.sh init'",
     "python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/world.sh init'\n"
     "python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/backup.sh'"),
    # the backup before the pause restart dropped
    ("python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/backup.sh'\n   python3 kit/record.py gate P9 -- sh",
     "python3 kit/record.py gate P9 -- sh"),
])
def test_the_p9_backup_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert p9_backup_problems(TEXT.replace(old, new))


def test_p10_backs_up_then_stands_up_before_the_reboot():
    p10 = section("P10.")
    line = f"python3 kit/record.py gate P10 -- {BACKUP_GATE} && python3 kit/record.py gate P10 -- sudo bash " \
           "host/setup.sh --stand-up"
    assert p10.count(line) == 1
    assert p10.index("python3 kit/record.py begin P10") < p10.index(line) < p10.index("sudo systemctl reboot")
    assert "systemctl enable --now" not in p10  # arming is setup.sh step 14's, with its refusals


# ---- P3: the gate is tailscale.py's, and it can fail on the friends' access --------------------------------------

P3_GATE = "python3 kit/record.py gate P3 -- python3 kit/tailscale.py setup --apply"


def test_the_p3_gate_is_the_tailscale_module_and_the_token_goes_after_it():
    p3 = section("P3.")
    assert p3.count(P3_GATE) == 1
    token_gone = 'python3 kit/record.py gate P3 -- test ! -e "$HOME/.config/mc-kit/tailscale-api-token"'
    assert p3.index(P3_GATE) < p3.index(token_gone) < p3.index("python3 kit/record.py end P3")


def _tailscale():
    spec = importlib.util.spec_from_file_location("kit_md_tailscale", KIT / "tailscale.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("policy", [
    {"grants": [{"src": ["autogroup:shared"], "dst": ["tag:mc-server"], "ip": ["*"]}]},
    {"grants": [{"src": ["autogroup:shared"], "dst": ["tag:mc-server"], "ip": ["tcp:25565", "udp:24454"]}]},
    {"acls": [{"action": "accept", "src": ["autogroup:shared"], "dst": ["tag:mc-server:*"]}]},
    {"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]},
              {"action": "accept", "src": ["autogroup:danger-all"], "dst": ["tag:mc-server:22"]}]},
])
def test_the_p3_gate_fails_when_shared_users_get_more_than_tcp_25565(policy):
    """What `setup --apply` runs on the policy before the write and again on the read-back (gates()): a rule that
    gives shared users (or everyone) more than tcp:25565 on the tag is refused, so the gate exits non-zero."""
    ts = _tailscale()
    with pytest.raises(ts.TsError):
        ts.merge_policy(policy, "tag:mc-server")


def test_the_kit_rule_is_the_agents_md_rule():
    ts = _tailscale()
    merged, _ = ts.merge_policy({"grants": []}, "tag:mc-server")
    rule = '{"src":["autogroup:shared"],"dst":["tag:mc-server"],"ip":["tcp:25565"]}'
    assert json.loads(rule) in merged["grants"]
    assert f"`{rule}`" in (REPO / "AGENTS.md").read_text()


# ---- the human steps: logged as they happen, 1-6 retro at the start ----------------------------------------------

def retro_problems(text):
    """Right after `record.py start`, in the same command, every required step 1-6 is logged --retro, the last one
    with the time steps 1-6 took as its note (never as an --unplanned step, which would add 1 to N)."""
    before = section("Before P0", text)
    m = re.search(r"python3 kit/record\.py start && for s in ([\d ]+); do python3 kit/record\.py human \"\$s\" "
                  r"--retro \|\| exit 1; done && python3 kit/record\.py human (\d+) --retro --note \"steps 1-6 took "
                  r"about <their answer>\"", before)
    if not m:
        return ["the start + retro command, with step 6's duration note, is gone"]
    logged = m.group(1).split() + [m.group(2)]
    want = [s for s in record.required_steps(record.load_steps(STEPS_TEXT)) if int(s) <= 6]
    problems = [] if logged == want else [f"logs {logged}, the required steps 1-6 are {want}"]
    if re.search(r"human --unplanned \"<note>\"", before):
        problems.append("the duration goes in as an unplanned step")
    return problems


def test_steps_1_to_6_are_logged_retro_at_the_start():
    assert retro_problems(TEXT) == []
    assert "python3 kit/record.py human 3 --retro" in section("Before P0")  # optional, on a yes
    assert "python3 kit/record.py human 7" in section("Before P0")  # step 7 happens live, after the clone


@pytest.mark.parametrize("old,new", [
    ("for s in 1 2 4 5;", "for s in 4 5;"),
    ("python3 kit/record.py start && for s", "python3 kit/record.py start; echo later; for s"),
    # the finding: the duration had no home but an unplanned step
    (' && python3 kit/record.py human 6 --retro --note "steps 1-6 took about <their answer>"', ""),
    ("python3 kit/record.py human 6 --retro --note", "python3 kit/record.py human 6 --retro --unplanned"),
])
def test_the_retro_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1
    assert retro_problems(TEXT.replace(old, new))


def test_every_optional_or_conditional_step_is_logged_somewhere_in_kit_md():
    steps = record.load_steps(STEPS_TEXT)
    maybe = [s for s, title in steps.items() if s.isdigit() and title.startswith(("(Optional)", "(Conditional)"))]
    assert sorted(maybe, key=int) == ["3", "19", "20"]  # the list this test guards; a new one joins it
    missing = [s for s in maybe if not re.search(rf"python3 kit/record\.py human {s}\b", TEXT)
               and not re.search(rf"Log step {s}\b", TEXT)]
    assert missing == []


HUMAN_LOG = re.compile(r"python3 kit/record\.py human (\w+)\b")


def logged_ids(text):
    """-> {step id: how many explicit `record.py human <id>` commands KIT.md gives}, plus the retro loop's ids."""
    out = {}
    for s in HUMAN_LOG.findall(text):
        out[s] = out.get(s, 0) + 1
    loop = re.search(r"for s in ([\d ]+); do python3 kit/record\.py human \"\$s\"", text)
    for s in (loop.group(1).split() if loop else []):
        out[s] = out.get(s, 0) + 1
    return out


def must_log():
    """Every required one-time step, and step 9 (skipped only by people who already have Discord)."""
    return record.required_steps(record.load_steps(STEPS_TEXT)) + ["9"]


def unlogged(text):
    have = logged_ids(text)
    return [s for s in must_log() if not have.get(s)]


def test_kit_md_logs_every_required_step_with_its_own_command():
    """A step KIT.md never says to log reaches finish unlogged (the integrated kit's 14, 15, 17 and 18 did)."""
    assert unlogged(TEXT) == []
    have = logged_ids(TEXT)
    assert {s: have[s] for s in must_log() if have[s] != 1} == {}  # one place each: the moment it happens


@pytest.mark.parametrize("step", must_log())
def test_the_logging_pin_catches_each_removed_line(step):
    if int(step) in (1, 2, 4, 5):  # the retro loop's ids: test_the_retro_pin_catches_its_mutant removes them
        pattern = re.compile(rf"(for s in [\d ]*?)\b{step}\b ?")
        assert len(pattern.findall(TEXT)) == 1, f"stale or ambiguous mutation pattern for step {step}"
        mutant = pattern.sub(r"\1", TEXT)
    else:
        pattern = re.compile(rf"python3 kit/record\.py human {step}\b")
        assert len(pattern.findall(TEXT)) == 1, f"stale or ambiguous mutation pattern for step {step}"
        mutant = pattern.sub("python3 kit/record.py status", TEXT)
    assert unlogged(mutant) == [step]


def test_step_19_prints_the_human_steps_command_for_the_second_terminal():
    p4 = section("P4.")
    assert any("nmcli device wifi connect" in b for b in fences(p4))
    assert "second terminal" in p4 and "python3 kit/record.py human 19" in p4
    assert "sudo nmcli device wifi connect <name> --ask" in STEPS_TEXT  # the command step 19 tells the person about
    assert "human step 19 (P4)" in section("P0.")  # P0 is where the case is found


def wifi_note_problems(text):
    """P7 routes a per-user Wi-Fi password (setup.sh step 2's abort, the desktop's default) to step 19 and logs it."""
    p7 = section("P7.", text)
    problems = []
    if "its password is not stored system-wide" not in p7:
        problems.append("P7 does not name setup.sh's abort")
    if "python3 kit/record.py human 19" not in p7:
        problems.append("P7 does not log step 19")
    if "second terminal" not in p7 or "until it finishes with no abort" not in p7:
        problems.append("P7 does not say where the command goes, or to re-run setup before the gates")
    return problems


def test_p7_routes_a_per_user_wifi_password_to_step_19():
    assert wifi_note_problems(TEXT) == []
    # the words P7 quotes are setup.sh's own abort, and the command it hands over is setup.sh's
    setup = (REPO / "host" / "setup.sh").read_text()
    assert "its password is not stored system-wide" in setup and "${wifi_store_how}" in setup
    assert "connection.permissions ''" in setup  # wifi_store_how stores it for the whole machine


def test_the_wifi_note_pin_catches_its_mutant():
    old = section("P7.")[section("P7.").index("- **A Wi-Fi password stored only"):]
    old = old[:old.index("- The charge cap")]
    assert TEXT.count(old) == 1
    assert wifi_note_problems(TEXT.replace(old, ""))


# ---- check.sh's ssh lines: a precise gate, never a blanket --accept ----------------------------------------------

def ssh_gate_problems(text):
    problems = []
    for p in ("P7", "P10"):
        gates = [ln for ln in "\n".join(fences(section(f"{p}.", text))).splitlines()
                 if f"kit/record.py gate {p} -- sudo bash host/check.sh" in ln]
        if len(gates) != 1 or not re.search(rf"gate {p} -- sudo bash host/check\.sh --no-ssh(\s|$)", gates[0]):
            problems.append(f"{p}: the check.sh gate is not check.sh --no-ssh: {gates}")
    if re.search(r"end P(7|10) --accept", text) or re.search(r"accepted check\.sh FAIL", text):
        problems.append("a check.sh FAIL is still accepted wholesale")
    return problems


def test_p7_and_p10_gate_check_sh_with_no_ssh_and_accept_nothing():
    assert ssh_gate_problems(TEXT) == []
    assert "--no-ssh) NO_SSH=1 ;;" in (REPO / "host" / "check.sh").read_text()


@pytest.mark.parametrize("old,new", [
    ("python3 kit/record.py gate P7 -- sudo bash host/check.sh --no-ssh", "python3 kit/record.py gate P7 -- sudo bash "
     "host/check.sh"),
    ("python3 kit/record.py gate P10 -- sudo bash host/check.sh --no-ssh", "python3 kit/record.py gate P10 -- sudo "
     "bash host/check.sh"),
    ("python3 kit/record.py end P10\n", 'python3 kit/record.py end P10 --accept "no ssh access wanted"\n'),
])
def test_the_ssh_gate_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert ssh_gate_problems(TEXT.replace(old, new))


# ---- docker never prints the secrets compose resolves -------------------------------------------------------------

SECRET_DOCKER = re.compile(r"docker inspect|docker compose (?:exec|run)\b[^\n]*\b(?:env|printenv)\b"
                           r"|docker compose config(?! -q 2>/dev/null)")


def docker_secret_problems(text):
    problems = [c for c in commands(text) if SECRET_DOCKER.search(c)
                and c not in ("docker inspect", "docker compose exec ... env", "docker compose config",
                              "docker compose config -q 2>/dev/null")]  # the rule's own spans name the forms
    rules = section("The rules that bind you here", text)
    for want in ("`docker inspect`", "`docker compose exec ... env`", "`docker compose config -q 2>/dev/null`",
                 "`python3 kit/render.py --check`"):
        if want not in rules:
            problems.append(f"the rule does not name {want}")
    return problems


def test_no_command_prints_what_compose_resolves_and_the_rule_says_so():
    assert docker_secret_problems(TEXT) == []


@pytest.mark.parametrize("old,new", [
    ("sg docker -c 'docker compose config -q 2>/dev/null'", "sg docker -c 'docker compose config'"),
    ("`docker inspect`, ", ""),
    ("python3 kit/record.py gate P8 -- sg docker -c 'docker compose exec minecraft rcon-cli list'",
     "python3 kit/record.py gate P8 -- sg docker -c 'docker compose exec minecraft env'"),
])
def test_the_docker_secret_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert docker_secret_problems(TEXT.replace(old, new))


def test_step_7_runs_the_kit_sudoers_tool_not_a_hand_written_line():
    step7 = STEPS_TEXT[STEPS_TEXT.index("### 7."):STEPS_TEXT.index("### 8.")]
    assert "sudo bash ~/minecraft-ai-host/kit/sudoers.sh install" in step7
    assert "NOPASSWD" not in step7 and "visudo -cf ~/" not in step7  # sudoers.sh refuses a file it did not write
    assert "sudo bash ~/minecraft-ai-host/kit/sudoers.sh install" in section("Before P0")


def ubuntu_floor_problems(steps_text, readme, kit_text):
    """Which Ubuntu to install (operator 2026-10-18): 24.04 LTS or newer, 24.04 the tested one, newer untested,
    Desktop not Server, 22.04 and older refused (kit/probe.py and kit/record.py refuse python3 under 3.11)."""
    flat = lambda t: " ".join(t.split())  # noqa: E731
    step1 = flat(steps_text[steps_text.index("### 1."):steps_text.index("### 2.")])
    want = {"HUMAN-STEPS step 1": (step1, ["**Ubuntu Desktop 24.04 LTS or newer**", "24.04 is the version the kit was "
                                          "tested on", "should work, but nobody has tested it yet",
                                          "22.04 or older won't work", "Use Desktop, not Server"]),
            "README": (flat(readme), ["**Ubuntu Desktop 24.04 LTS or newer**", "newer should work but is untested",
                                      "not 22.04 or older"]),
            "KIT.md P0": (flat(section("P0. Probe", kit_text)), ["python3 older than 3.11 (Ubuntu 22.04 or older)"]),
            "KIT.md Before P0": (flat(section("Before P0", kit_text)), ["If `start` refuses because python3 is older "
                                                                        "than 3.11"])}
    problems = [f"{where} lacks: {w}" for where, (text, ws) in want.items() for w in ws if w not in text]
    for name, text in (("HUMAN-STEPS", steps_text), ("README", readme), ("KIT.md", kit_text)):
        if "24.04 LTS**" in text or "install Ubuntu Desktop 24.04 LTS)" in text:  # the old "24.04 only" wording
            problems.append(f"{name} still says 24.04 only")
    return problems


def test_the_docs_say_ubuntu_24_04_or_newer():
    assert ubuntu_floor_problems(STEPS_TEXT, DOCS["kit/README.md"], TEXT) == []


@pytest.mark.parametrize("doc,old,new", [
    ("steps", "**Ubuntu Desktop 24.04 LTS or newer**", "**Ubuntu Desktop 24.04 LTS**"),
    ("steps", "22.04 or older\nwon't work", "22.04 should\nwork"),
    ("readme", "and not 22.04 or older", "and 22.04 is fine"),
    ("kit", "python3 older than 3.11 (Ubuntu 22.04 or older), no", "no"),
])
def test_the_ubuntu_floor_pin_catches_its_mutant(doc, old, new):
    texts = {"steps": STEPS_TEXT, "readme": DOCS["kit/README.md"], "kit": TEXT}
    assert texts[doc].count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    texts[doc] = texts[doc].replace(old, new)
    assert ubuntu_floor_problems(texts["steps"], texts["readme"], texts["kit"])


def test_human_steps_md_count_matches_its_mandatory_steps():
    """The count table's 20 = the one-time ids minus the optional and conditional ones (3, 19, 20); 19 without
    step 9 (most people have Discord). A step added or removed without updating the count fails here."""
    steps = record.load_steps(STEPS_TEXT)
    one_time = {s for s in steps if s.isdigit()}
    assert len(one_time - {"3", "19", "20"}) == 20 and len(record.required_steps(steps)) == 19
    assert "| **20** (19 if you already have Discord: step 9)" in STEPS_TEXT
    assert '"you do about 20 things once (19 if you already have Discord), each friend does 10; Claude does the rest."' in \
        " ".join(STEPS_TEXT.split())
    assert promise_problems(STEPS_TEXT) == []


def promise_problems(steps_text):
    """The promise paragraph derives its number once, the way the table counts (review 2026-10-16: it derived 19
    twice, from two different sets of steps). The first design's 17 = 1, 2 and 4-18, so it holds step 9."""
    para = " ".join(steps_text[steps_text.index("**The promise:**"):].split("\n\n")[0].split())
    want = ["the kit's first design had 17 one-time steps (1, 2 and 4–18, with step 9)",
            "make 19 with Discord sign-up and 18 without; the proof run measured the 18",
            "Choosing the pack (23) came after the run: 20 with step 9, 19 without."]
    problems = [f"missing: {w}" for w in want if w not in para]
    for stale in ("the 19th is", "grew by two", "left the count"):  # the second derivation, and the router's minus one
        if stale in para:
            problems.append(f"still says: {stale}")
    return problems


@pytest.mark.parametrize("old,new", [
    ("Choosing the pack (23) came after the run: 20 with step 9, 19 without.",
     "The 19th is choosing the pack (23), added after it."),
    ("Steps 19 and 20", "The router step (20) left the count. Steps 19 and 20"),
])
def test_the_promise_pin_catches_its_mutant(old, new):
    assert STEPS_TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert promise_problems(STEPS_TEXT.replace(old, new))


# ---- the pack card (proof run 2026-10-16: the session went straight to Vanilla Extended) --------------------------

def pack_card_problems(text, steps_text):
    """Right after P0's kit.toml (the RAM is known), before P1: an AskUserQuestion card, Vanilla Extended first and
    recommended, the other way to CUSTOM-PACK.md; logged as step 23, which HUMAN-STEPS makes required."""
    problems = []
    p0 = section("P0.", text)
    card = p0[p0.find("### The pack (human step 23)"):] if "### The pack (human step 23)" in p0 else ""
    if not card or p0.index("### The pack") < p0.index("python3 kit/record.py end P0"):
        problems.append("P0 has no pack card after the probe")
    if "**AskUserQuestion**" not in card:
        problems.append("the card is not asked with AskUserQuestion")
    ve, other = card.find("1. **Vanilla Extended (Recommended).**"), card.find("2. **Something different.**")
    if not (-1 < ve < other):
        problems.append("Vanilla Extended is not the first, recommended option")
    if "python3 kit/record.py human 23" not in card or "docs/kit/CUSTOM-PACK.md" not in card:
        problems.append("the card does not log step 23 or route to CUSTOM-PACK.md")
    if "23" not in record.required_steps(record.load_steps(steps_text)):
        problems.append("step 23 is not a required step, so finish would not refuse without it")
    return problems


def test_p0_asks_for_the_pack_with_a_card_and_logs_step_23():
    assert pack_card_problems(TEXT, STEPS_TEXT) == []


@pytest.mark.parametrize("old,new,doc", [
    ("### The pack (human step 23)", "### A pack", "kit"),
    ("Ask with the **AskUserQuestion** tool", "Ask in the chat", "kit"),
    ("1. **Vanilla Extended (Recommended).**", "1. **Vanilla Extended.**", "kit"),
    ('python3 kit/record.py human 23 --note', 'python3 kit/record.py status --note', "kit"),
    ("### 23. Choose the pack", "### 23. (Optional) Choose the pack", "steps"),
])
def test_the_pack_card_pin_catches_its_mutant(old, new, doc):
    text, steps_text = (TEXT.replace(old, new), STEPS_TEXT) if doc == "kit" else (TEXT, STEPS_TEXT.replace(old, new))
    assert (TEXT if doc == "kit" else STEPS_TEXT).count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert pack_card_problems(text, steps_text)


# ---- P6 cleans the template's channels before the first sync; P9 gates on Chunky; P11 reads since its begin ------

def order_problems(text):
    problems = []
    p6 = section("P6.", text)
    clean, sync = p6.find("python3 discord/bootstrap.py clean --apply"), p6.find("python3 discord/sync.py --apply")
    if not (-1 < clean < sync):
        problems.append("P6 does not clean before the first sync --apply")
    if "python3 kit/record.py gate P6 -- python3 discord/bootstrap.py clean" not in p6:
        problems.append("P6 does not gate on a dry-run clean")
    p9 = section("P9.", text)
    if not re.search(r"gate P9 -- sg docker -c 'bash deploy/world\.sh done overworld=3000 the_nether=1500 "
                     r"the_end=2000'", p9):
        problems.append("P9 does not gate on Chunky finishing the three rings")
    p11 = section("P11.", text)
    if 's=$(python3 kit/record.py began P11) && docker compose logs --since "$s" minecraft' not in p11 \
            or "--since 10m" in p11:
        problems.append("P11's chat gate does not read since the phase began")
    return problems


def test_p6_cleans_first_p9_gates_on_chunky_and_p11_reads_since_its_begin():
    assert order_problems(TEXT) == []


@pytest.mark.parametrize("old,new", [
    ("python3 discord/bootstrap.py clean --apply    # deletes them, then reads the channel list back\n", ""),
    ("python3 kit/record.py gate P6 -- python3 discord/bootstrap.py clean\n", ""),
    ("python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/world.sh done overworld=3000 the_nether=1500 "
     "the_end=2000'\n", ""),
    ('s=$(python3 kit/record.py began P11) && docker compose logs --since "$s" minecraft',
     "docker compose logs --since 10m minecraft"),
])
def test_the_order_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert order_problems(TEXT.replace(old, new))


# ---- what the record gets (review 2026-10-16): captchas are notes, step 23 once, P5's spike gate on every kit -------

def record_problems(text):
    problems = []
    if re.search(r'--unplanned "[^"]*captcha', text, re.I):
        problems.append("a captcha is logged as an unplanned step (it is part of steps 13 and 16)")
    if 'is part of that step,\n   never an unplanned one: add `--note "captcha"` to that step\'s line' not in section("P1.", text):
        problems.append("P1 does not say where the Authorize captcha goes")
    if re.findall(r"python3 kit/record\.py human 23\b[^\n`]*", text) != [
            'python3 kit/record.py human 23 --note "<vanilla-extended or custom>"']:
        problems.append("step 23 is not logged once, at the card, with the option picked")
    if "Log step 23 once the pack is written" in text:
        problems.append("the custom path logs step 23 a second time")
    p5 = section("P5.", text)
    gate = "python3 kit/record.py gate P5 -- sg docker -c 'bash pack/spike.sh --if-custom'"
    if gate not in p5 or "skips this block" in p5 or p5.find(gate) > p5.find("python3 kit/record.py end P5"):
        problems.append("P5's spike gate is not unconditional, before end P5")
    return problems


def test_the_record_gets_captchas_as_notes_step_23_once_and_every_spike_gate():
    assert record_problems(TEXT) == []


@pytest.mark.parametrize("old,new", [
    ('--unplanned "Discord terms-of-service tick"', '--unplanned "captcha on Authorize"'),
    ('never an unplanned one: add `--note "captcha"` to that step\'s line', "log it as an unplanned step"),
    ('human 23 --note "<vanilla-extended or custom>"', 'human 23 --note "<vanilla-extended, or custom: its name>"'),
    ("Step 23 is already logged: do not log it again.", "Log step 23 once the pack is written, with its name."),
    ("sg docker -c 'bash pack/spike.sh --if-custom'", "sg docker -c 'bash pack/spike.sh'"),
])
def test_the_record_pin_catches_its_mutant(old, new):
    assert TEXT.count(old) == 1, f"stale or ambiguous mutation pattern {old!r}"
    assert record_problems(TEXT.replace(old, new))


# ---- the rules a fresh session needs ------------------------------------------------------------------------------

def test_kit_md_says_a_bash_call_keeps_no_shell_state():
    rules = section("The rules that bind you here")
    assert "**Each Bash tool call is a fresh shell.** No variable, function or `cd` survives into the next call." \
        in rules
    for cmd in fences(TEXT):  # and no block relies on a helper: a function definition would have to be re-made
        assert not re.search(r"^\s*\w+\(\)\s*\{", cmd, re.M), cmd


def test_p6_runs_on_the_kit_profile_and_names_the_stop():
    p6 = section("P6.")
    assert '`discord.profile = "kit"`' in p6 and "**sync refuses, before any API call,**" in p6
    import tomllib
    example = tomllib.loads((KIT / "kit.example.toml").read_text())
    assert example["discord"]["profile"] == "kit"  # P0 copies the example, so a kit starts on the kit profile
