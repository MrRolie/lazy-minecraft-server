"""Tests for kit/record.py, the kit run recorder (plan gap #17). Run: uv run --with pytest pytest kit/

Every guard has a scenario that the real module refuses, and a mutation test: the guard's text is replaced in a
copy of record.py, and the same scenario must then go wrong (a guard test only counts once it catches its mutant;
a guard text that moved fails as a stale pattern, never a silent skip).
"""
import datetime
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import record

KIT = Path(__file__).resolve().parent
SOURCE = (KIT / "record.py").read_text()
PHASES = {f"P{n}": f"Phase {n}" for n in range(4)}
STEPS = {"1": "USB", "2": "Install", "11": "Admin app", "F1": "Java", "R1": "Share link"}
T0 = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)
# Secret shapes, assembled at run time so this file never holds one (discord/test_sync.py scans every tracked file).
SHARE = "https://login.tailscale.com/admin/" + "invite/" + "abc123"
TSKEY = "tskey" + "-api-k123-ABCDEF"
WEBHOOK = "https://discord.com/api/" + "webhooks/" + "123/abc"
DTOKEN = "M" + "x" * 25 + "." + "Gabcde" + "." + "y" * 30
# The shape of the RCON password KIT.md P4 mints (secrets.token_urlsafe(24): 32 urlsafe characters), with and
# without a digit (about 1 in 230 real ones has none).
RCON = "Xk3_Pq9-" + "abcdefghijklmnopqrstuv" + "12"
RCON_NO_DIGIT = "Xk_Pq-" + "abcdefghijklmnopqrstuvwxyz"


def at(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "f").write_text("one\n")
    git(r, "add", "f")
    git(r, "commit", "-q", "-m", "c")
    return r


@pytest.fixture
def rd(tmp_path):
    return tmp_path / "runs"


def started(mod, rd, repo):
    mod.cmd_start(rd, repo=repo, now=at(0))
    return mod.current(rd)


def events(mod, rd):
    return mod.read_events(mod.current(rd))


def ok(rc):
    return lambda argv: rc


# ---- the documents are the ids' one home ------------------------------------------------------------------------

def test_kit_md_has_phases_p0_to_p12_in_order():
    phases = record.load_phases(record.KIT_MD.read_text())
    assert list(phases) == [f"P{n}" for n in range(13)]


def test_human_steps_md_has_the_planned_ids():
    steps = record.load_steps(record.STEPS_MD.read_text())
    assert set(steps) == {str(n) for n in range(1, 24)} | {f"F{n}" for n in range(1, 11)} | {"R1", "R2", "R3"}


def test_finish_refuses_a_real_run_that_never_logged_the_pack_choice(rd, repo):
    """HUMAN-STEPS step 23 (the pack card, proof run 2026-10-16) is a required step: with every other required step
    of the real page logged, finish still refuses until 23 is logged, then passes."""
    steps = record.load_steps(record.STEPS_MD.read_text())
    required = record.required_steps(steps)
    assert "23" in required
    started(record, rd, repo)
    for s in required:
        if s != "23":
            record.cmd_human(rd, s, steps, retro=True)
    with pytest.raises(record.RecordError, match=r"never logged: 23\. "):
        record.cmd_finish(rd, PHASES, steps, repo=repo)
    record.cmd_human(rd, "23", steps, note="vanilla-extended")
    assert "INCOMPLETE" not in record.cmd_finish(rd, PHASES, steps, repo=repo)


# ---- the happy path and the record -------------------------------------------------------------------------------

def test_a_run_records_sha_phases_gates_and_human_steps(rd, repo):
    run = started(record, rd, repo)
    record.cmd_human(rd, "1", STEPS, retro=True, note="about 40 min", now=at(1))
    for n, p in enumerate(PHASES):
        record.cmd_begin(rd, p, PHASES, now=at(10 * n + 2))
        record.cmd_gate(rd, p, ["true"], now=at(10 * n + 3), runner=ok(0))
        if p == "P1":
            record.cmd_human(rd, "11", STEPS, now=at(10 * n + 4))
            record.cmd_human(rd, "11", STEPS, note="again, after a typo", now=at(10 * n + 5))
            record.cmd_human(rd, "", STEPS, unplanned="captcha on Authorize", now=at(10 * n + 6))
        if p == "P3":
            record.cmd_observe(rd, p, "chat showed up in game", now=at(10 * n + 7))
        status, _ = record.cmd_end(rd, p, now=at(10 * n + 9))
        assert status == ("pass-observed" if p == "P3" else "pass")
    record.cmd_finish(rd, PHASES, STEPS, repo=repo, now=at(60), incomplete=True)
    s = json.loads((run / "record.json").read_text())
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert s["sha_start"] == s["sha_finish"] == sha and s["dirty_start"] is False
    assert s["outcome"] == "complete" and s["took"] == "1h00m"
    assert [r["phase"] for r in s["phases"]] == list(PHASES) and s["phases"][1]["took"] == "7m00s"
    assert s["human"]["planned_seen"] == ["1", "11"] and s["human"]["unplanned"] == ["captcha on Authorize"]
    assert s["human"]["count"] == 3 and s["human"]["actions"] == 4
    assert s["human"]["planned_not_seen"] == ["2"]  # one-time steps only; F1 and R1 are not a setup run's
    md = (run / "record.md").read_text()
    assert sha in md and "NOT ON THE LIST: captcha on Authorize" in md and "(done before the run; reported)" in md
    assert "observed: chat showed up in game" in md and "| P1 |" in md
    assert record.current(rd) == run  # finished, still the latest


def test_the_gate_command_really_runs_and_its_exit_code_is_kept(rd, repo, tmp_path):
    started(record, rd, repo)
    record.cmd_begin(rd, "P0", PHASES)
    flag = tmp_path / "ran"
    rc, _ = record.cmd_gate(rd, "P0", ["sh", "-c", f"touch {flag}; exit 3"])
    assert rc == 3 and flag.exists()
    rc, _ = record.cmd_gate(rd, "P0", ["no-such-command-xyz"])
    assert rc == 127
    assert [e.get("rc") for e in events(record, rd) if e["kind"] == "gate"] == [3, 127]
    assert record.cmd_end(rd, "P0")[0] == "fail"


def test_a_dirty_tree_and_a_changed_sha_show(rd, repo):
    (repo / "f").write_text("changed\n")
    run = started(record, rd, repo)
    git(repo, "commit", "-qam", "d")
    record.cmd_finish(rd, PHASES, STEPS, repo=repo, incomplete=True)
    s = json.loads((run / "record.json").read_text())
    assert s["dirty_start"] is True and s["dirty_finish"] is False and s["sha_start"] != s["sha_finish"]
    md = (run / "record.md").read_text()
    assert "(dirty tree)" in md and "The code changed during the run" in md
    assert s["outcome"].startswith("incomplete")


def test_accepted_failure_unblocks_the_next_phase_and_shows(rd, repo):
    run = started(record, rd, repo)
    record.cmd_begin(rd, "P0", PHASES)
    record.cmd_gate(rd, "P0", ["false"], runner=ok(1))
    status, _ = record.cmd_end(rd, "P0", accept="the person chose 12 GB (unproven)")
    assert status == "accepted-fail"
    record.cmd_begin(rd, "P1", PHASES)
    record.cmd_end(rd, "P1")  # no gate: fail
    record.cmd_begin(rd, "P1", PHASES)  # a failed phase can be begun again
    record.cmd_gate(rd, "P1", ["true"], runner=ok(0))
    record.cmd_end(rd, "P1")
    record.cmd_finish(rd, {"P0": "a", "P1": "b"}, STEPS, repo=repo, incomplete=True)
    s = json.loads((run / "record.json").read_text())
    assert s["outcome"] == "complete, with accepted failures"
    assert "accepted: the person chose 12 GB (unproven)" in (run / "record.md").read_text()


def test_a_failure_is_accepted_after_its_end_as_kit_md_runs_it(rd, repo):
    """KIT.md's order: `end Pn` shows the failure, Claude asks the person, then `end Pn --accept "<why>"`. The
    gates are kept as they ran (nothing is re-run), and the next phase can begin."""
    run = started(record, rd, repo)
    record.cmd_begin(rd, "P0", PHASES, now=at(1))
    record.cmd_gate(rd, "P0", ["true"], runner=ok(0), now=at(2))
    record.cmd_gate(rd, "P0", ["free-ram-check"], runner=ok(1), now=at(3))
    assert record.cmd_end(rd, "P0", now=at(4))[0] == "fail"
    status, msg = record.cmd_end(rd, "P0", accept="the proof VM has 12 GB (unproven)", now=at(6))
    assert status == "accepted-fail" and "1 gate(s) exited non-zero" in msg
    assert record.phase_state(events(record, rd))[1]["P0"] == "accepted-fail"
    with pytest.raises(record.RecordError, match="not the phase that just ended"):
        record.cmd_end(rd, "P0", accept="twice")  # accepted once only
    record.cmd_begin(rd, "P1", PHASES, now=at(7))
    record.cmd_gate(rd, "P1", ["true"], runner=ok(0), now=at(8))
    record.cmd_end(rd, "P1", now=at(9))
    record.cmd_finish(rd, {"P0": "a", "P1": "b"}, STEPS, repo=repo, now=at(10), incomplete=True)
    s = json.loads((run / "record.json").read_text())
    assert s["outcome"] == "complete, with accepted failures"
    p0 = s["phases"][0]
    assert p0["status"] == "accepted-fail" and p0["accepted"] == "the proof VM has 12 GB (unproven)"
    assert [g["rc"] for g in p0["gates"]] == [0, 1] and p0["took"] == "3m00s"  # the gates as they ran
    row = next(l for l in (run / "record.md").read_text().splitlines() if l.startswith("| P0."))
    assert "accepted-fail (accepted: the proof VM has 12 GB (unproven))" in row and "free-ram-check` → 1" in row


def test_markdown_cells_cannot_break_the_table(rd, repo):
    run = started(record, rd, repo)
    record.cmd_begin(rd, "P0", PHASES)
    record.cmd_gate(rd, "P0", ["sh", "-c", "a | b"], runner=ok(0))
    record.cmd_end(rd, "P0")
    record.cmd_finish(rd, PHASES, STEPS, repo=repo, incomplete=True)
    row = next(l for l in (run / "record.md").read_text().splitlines() if l.startswith("| P0."))
    assert row.count(" | ") == 4 and "a \\| b" in row


def test_cli_end_to_end(tmp_path, repo):
    env = {**os.environ, "MC_KIT_RUNS": str(tmp_path / "cli-runs")}

    def cli(*args):
        return subprocess.run(["python3", str(KIT / "record.py"), *args], env=env, capture_output=True, text=True)

    assert cli("start").returncode == 0
    assert cli("begin", "P0").returncode == 0
    r = cli("gate", "P0", "--", "sh", "-c", "printf '%s-%s' gate output; exit 0")
    assert r.returncode == 0 and "gate-output" in r.stdout  # the output passes through
    assert cli("gate", "P0", "--", "false").returncode == 1
    assert cli("end", "P0").returncode == 1  # a failed phase exits 1
    r = cli("end", "P0", "--accept", "proof vm")  # then the person accepts it (KIT.md: end, ask, accept)
    assert r.returncode == 0 and "accepted" in r.stdout, r.stderr
    assert cli("end", "P0", "--accept", "again").returncode == 1
    r = cli("human", "--unplanned", "a " + SHARE)
    assert r.returncode == 1 and "refusing" in r.stderr and "share link" in r.stderr
    r = cli("status")
    assert r.returncode == 0 and "required one-time steps not logged yet: 1, 2, 4, 5, 6, 7" in r.stdout
    r = cli("finish")  # nothing was logged: refused, and the run stays open
    assert r.returncode == 1 and "never logged" in r.stderr and "finish --incomplete" in r.stderr
    assert cli("finish", "--incomplete").returncode == 0
    runs = [p for p in (tmp_path / "cli-runs").iterdir() if p.is_dir()]
    data = (runs[0] / "events.jsonl").read_text()
    assert "gate-output" not in data  # a gate's output is shown, never stored
    assert "invite/" not in data


# ---- guards -----------------------------------------------------------------------------------------------------
# name -> (scenario(mod, rd, repo, tmp_path): runs the refused action, guard text, its mutant,
#          went_wrong(mod, rd): the mutant let the action through)

def _second_begin(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_begin(rd, "P0", PHASES)


def _skip_ahead(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P1", PHASES)


def _after_a_failure(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_end(rd, "P0")  # no gate: fail
    mod.cmd_begin(rd, "P1", PHASES)


def _unknown_phase(mod, rd, repo, tmp):
    started(mod, rd, repo)
    for p in PHASES:  # every phase passed, so the order check cannot stand in for this one
        mod.cmd_begin(rd, p, PHASES)
        mod.cmd_gate(rd, p, ["true"], runner=ok(0))
        mod.cmd_end(rd, p)
    mod.cmd_begin(rd, "P9", PHASES)


def _gate_not_open(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_gate(rd, "P0", ["true"], runner=ok(0))


def _end_not_open(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_end(rd, "P0")


def _gateless_pass(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    if mod.cmd_end(rd, "P0")[0] == "fail":
        raise mod.RecordError("refused as fail")


def _failed_gate_pass(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_gate(rd, "P0", ["true"], runner=ok(0))
    mod.cmd_gate(rd, "P0", ["false"], runner=ok(1))
    if mod.cmd_end(rd, "P0")[0] == "fail":
        raise mod.RecordError("refused as fail")


def _observed_as_pass(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_observe(rd, "P0", "the person saw it")
    if mod.cmd_end(rd, "P0")[0] != "pass":
        raise mod.RecordError("labelled as observed")


def _accept_a_pass(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_gate(rd, "P0", ["true"], runner=ok(0))
    mod.cmd_end(rd, "P0", accept="why not")


def _failed_p0(mod, rd, repo):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_gate(rd, "P0", ["false"], runner=ok(1))
    mod.cmd_end(rd, "P0")


def _late_accept_after_a_later_phase(mod, rd, repo, tmp):
    _failed_p0(mod, rd, repo)
    mod.cmd_end(rd, "P0", accept="first")
    mod.cmd_begin(rd, "P1", PHASES)
    mod.cmd_end(rd, "P1")  # no gate: fail
    mod.cmd_end(rd, "P0", accept="again, after P1")


def _late_accept_while_another_is_open(mod, rd, repo, tmp):
    _failed_p0(mod, rd, repo)
    mod.cmd_end(rd, "P0", accept="first")
    mod.cmd_begin(rd, "P1", PHASES)
    mod.cmd_end(rd, "P0", accept="again, with P1 open")


def _late_accept_a_pass(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_gate(rd, "P0", ["true"], runner=ok(0))
    mod.cmd_end(rd, "P0")
    mod.cmd_end(rd, "P0", accept="why not")


def _late_secret(mod, rd, repo, tmp):
    _failed_p0(mod, rd, repo)
    mod.cmd_end(rd, "P0", accept=f"because {WEBHOOK}")


def _p0_accepted_twice(mod, rd):
    return sum(1 for e in mod.read_events(mod.current(rd))
               if e.get("phase") == "P0" and e.get("accepted")) == 2


def _start_twice(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_start(rd, repo=repo, now=at(5))


def _finish_open(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES)
    mod.cmd_finish(rd, PHASES, STEPS, repo=repo, incomplete=True)


def _finish_twice(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_finish(rd, PHASES, STEPS, repo=repo, incomplete=True)
    mod.cmd_finish(rd, PHASES, STEPS, repo=repo, incomplete=True)


def _after_finish(action):
    def scenario(mod, rd, repo, tmp):
        started(mod, rd, repo)
        mod.cmd_finish(rd, PHASES, STEPS, repo=repo, incomplete=True)
        if action == "human":
            mod.cmd_human(rd, "1", STEPS)
        else:
            mod.cmd_begin(rd, "P0", PHASES)
    return scenario


def _event_after_finish(mod, rd):
    kinds = [e["kind"] for e in mod.read_events(mod.current(rd))]
    return kinds.index("finish") < len(kinds) - 1


def _unknown_step(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_human(rd, "99", STEPS)


def _step_and_unplanned(mod, rd, repo, tmp):
    started(mod, rd, repo)
    mod.cmd_human(rd, "1", STEPS, unplanned="also this")


def _fake_git_sha(mod, rd, repo, tmp):
    fake = tmp / "fakebin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\necho not-a-sha\n")
    (fake / "git").chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{fake}:{old}"
    try:
        mod.cmd_start(rd, repo=repo)
    finally:
        os.environ["PATH"] = old


def _secret(where, value):
    def scenario(mod, rd, repo, tmp):
        started(mod, rd, repo)
        mod.cmd_begin(rd, "P0", PHASES)
        if where == "note":
            mod.cmd_human(rd, "1", STEPS, note=f"pasted {value}")
        elif where == "unplanned":
            mod.cmd_human(rd, "", STEPS, unplanned=f"pasted {value}")
        elif where == "observe":
            mod.cmd_observe(rd, "P0", f"saw {value}")
        elif where == "gate":
            mod.cmd_gate(rd, "P0", ["curl", value], runner=ok(0))
        elif where == "accept":
            mod.cmd_gate(rd, "P0", ["false"], runner=ok(1))
            mod.cmd_end(rd, "P0", accept=f"because {value}")
    return scenario


def _wrote_any(kinds):
    def went_wrong(mod, rd):
        run = mod.current(rd)
        return run is not None and any(e["kind"] in kinds for e in mod.read_events(run)[1:])
    return went_wrong


def _secret_landed(mod, rd):
    run = mod.current(rd)
    text = (run / "events.jsonl").read_text()
    return any(v in text for v in (SHARE, TSKEY, WEBHOOK, DTOKEN, RCON, RCON_NO_DIGIT))


def _two_begins(mod, rd):
    return sum(e["kind"] == "begin" for e in mod.read_events(mod.current(rd))) == 2


def _a_pass(mod, rd):
    return any(e["kind"] == "end" and e["status"] == "pass" for e in mod.read_events(mod.current(rd)))


def _accepted(mod, rd):
    return any(e.get("accepted") for e in mod.read_events(mod.current(rd)))


def _second_run(mod, rd):
    return len([p for p in rd.iterdir() if p.is_dir()]) == 2


def _finished(mod, rd):
    return sum(e["kind"] == "finish" for e in mod.read_events(mod.current(rd))) >= 1


def _two_finishes(mod, rd):
    return sum(e["kind"] == "finish" for e in mod.read_events(mod.current(rd))) == 2


def _no_sha_run(mod, rd):
    return rd.exists() and any(p.is_dir() for p in rd.iterdir())


GUARDS = {
    "second_begin": (_second_begin, 'if open_phase:\n        raise RecordError(f"{open_phase} is still open: end it first '
                     '(python3 kit/record.py end {open_phase})")\n    for p in phases:',
                     'if False:\n        raise RecordError(f"{open_phase} is still open: end it first '
                     '(python3 kit/record.py end {open_phase})")\n    for p in phases:', _two_begins),
    "skip_ahead": (_skip_ahead, "if status.get(p) not in PASSING + (SKIPPED,):", "if False:", _wrote_any({"begin"})),
    "after_a_failure": (_after_a_failure, "if status.get(p) not in PASSING + (SKIPPED,):",
                        "if status.get(p) is None:",
                        lambda m, rd: any(e["kind"] == "begin" and e["phase"] == "P1"
                                          for e in m.read_events(m.current(rd)))),
    "unknown_phase": (_unknown_phase, "if phase not in phases:", "if False:",
                      lambda m, rd: any(e.get("phase") == "P9" for e in m.read_events(m.current(rd)))),
    "gate_not_open": (_gate_not_open, "if open_phase != phase:", "if False:", _wrote_any({"gate"})),
    "end_not_open": (_end_not_open, "if open_phase != phase:", "if False:", _wrote_any({"end"})),
    "gateless_pass": (_gateless_pass, "if not gates:\n        return \"fail\"", "if False:\n        return \"fail\"",
                      _a_pass),
    "failed_gate_pass": (_failed_gate_pass, 'bad = [g for g in gates if g["kind"] == "gate" and g["rc"] != 0]',
                         "bad = []", _a_pass),
    "observed_as_pass": (_observed_as_pass, 'if any(g["kind"] == "observe" for g in gates):', "if False:", _a_pass),
    "accept_a_pass": (_accept_a_pass, 'if status != "fail":\n            raise', 'if False:\n            raise',
                      _accepted),
    "late_accept_after_a_later_phase": (
        _late_accept_after_a_later_phase, 'if not last or (last["kind"], last["phase"]) != ("end", phase):',
        "if False:", _p0_accepted_twice),
    "late_accept_while_another_is_open": (_late_accept_while_another_is_open, "if open_phase != phase:", "if False:",
                                          _p0_accepted_twice),
    "late_accept_a_pass": (_late_accept_a_pass, 'if last["status"] != "fail":', "if False:", _accepted),
    "late_secret_accept": (_late_secret, 'refuse_secret(accept, "reason")', "pass", _secret_landed),
    "start_twice": (_start_twice, 'if run and not any(e["kind"] == "finish" for e in read_events(run)):', "if False:",
                    _second_run),
    "finish_open": (_finish_open, 'if open_phase:\n        raise RecordError(f"{open_phase} is still open: end it first '
                    '(python3 kit/record.py end {open_phase})")\n    skipped = ',
                    'if False:\n        raise RecordError(f"{open_phase} is still open: end it first '
                    '(python3 kit/record.py end {open_phase})")\n    skipped = ', _finished),
    "finish_twice": (_finish_twice, 'if any(e["kind"] == "finish" for e in events):\n        raise',
                     'if False:\n        raise', _two_finishes),
    "human_after_finish": (_after_finish("human"), 'if any(e["kind"] == "finish" for e in read_events(run)):',
                           "if False:", _event_after_finish),
    "begin_after_finish": (_after_finish("begin"), 'if any(e["kind"] == "finish" for e in read_events(run)):',
                           "if False:", _event_after_finish),
    "unknown_step": (_unknown_step, "if step and step not in steps:", "if False:",
                     lambda m, rd: _wrote_any({"human"})(m, rd)),
    "step_and_unplanned": (_step_and_unplanned, "if bool(step) == bool(unplanned):", "if False:",
                           lambda m, rd: _wrote_any({"human"})(m, rd)),
    "sha_shape": (_fake_git_sha, 'if not re.fullmatch(r"[0-9a-f]{40}", sha):', "if False:", _no_sha_run),
    "secret_note": (_secret("note", SHARE), 'for text, where in ((note, "note"), (unplanned, "description")):',
                    'for text, where in ((unplanned, "description"),):', _secret_landed),
    "secret_unplanned": (_secret("unplanned", TSKEY), 'for text, where in ((note, "note"), (unplanned, "description")):',
                         'for text, where in ((note, "note"),):', _secret_landed),
    "secret_observe": (_secret("observe", WEBHOOK), 'refuse_secret(what, "observation")', "pass", _secret_landed),
    "secret_gate": (_secret("gate", DTOKEN), 'refuse_secret(" ".join(argv), "gate command")', "pass", _secret_landed),
    "secret_accept": (_secret("accept", SHARE), 'refuse_secret(accept, "reason")', "pass", _secret_landed),
    "pattern_discord_token": (_secret("note", DTOKEN), r'[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.', "NEVER", _secret_landed),
    "pattern_tskey": (_secret("note", TSKEY), 'r"tskey-[A-Za-z0-9-]+"', 'r"NEVER"', _secret_landed),
    "pattern_opaque_length": (_secret("note", RCON), "[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])",
                              "[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])", _secret_landed),
    "pattern_opaque_no_digit": (_secret("observe", RCON_NO_DIGIT), "(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])",
                                "(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])",
                                _secret_landed),
    "pattern_share": (_secret("note", SHARE), r'r"login\.tailscale\.com/admin/invite/[A-Za-z0-9]"', 'r"NEVER"',
                      _secret_landed),
    "pattern_webhook": (_secret("note", WEBHOOK), r'r"discord(?:app)?\.com/api/webhooks/\d"', 'r"NEVER"',
                        _secret_landed),
}


def load(text, tmp_path, name="record_mut"):
    path = tmp_path / f"{name}.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", sorted(GUARDS))
def test_guard_refuses(tmp_path, repo, name):
    scenario, *_, went_wrong = GUARDS[name]
    rd = tmp_path / "runs"
    with pytest.raises(record.RecordError):
        scenario(record, rd, repo, tmp_path)
    assert not went_wrong(record, rd) if rd.exists() and record.current(rd) else True


@pytest.mark.parametrize("name", sorted(GUARDS))
def test_each_guard_catches_its_mutant(tmp_path, repo, name):
    scenario, guard, mutant, went_wrong = GUARDS[name]
    assert SOURCE.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    mod = load(SOURCE.replace(guard, mutant), tmp_path)
    rd = tmp_path / "runs"
    try:
        scenario(mod, rd, repo, tmp_path)
    except (mod.RecordError, KeyError):
        pass  # a later line may still stop it; what matters is whether the bad state was written
    assert went_wrong(mod, rd), name


def test_root_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(record.os, "geteuid", lambda: 0)
    assert record.main(["start"]) == 1
    assert "not root" in capsys.readouterr().err and not (tmp_path / "runs").exists()


def test_old_python_is_refused_before_anything_is_written(monkeypatch, tmp_path, capsys):
    """record.py start is the first kit command a run executes (KIT.md "Before P0"): on Ubuntu 22.04's python3 it
    stops there with the floor named, never a traceback later."""
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))
    assert record.main(["start"], version=(3, 10, 12)) == 1
    err = capsys.readouterr().err
    assert err.startswith("refusing: python3 is 3.10.12, older than 3.11: the kit needs Python 3.11 or newer"), err
    assert not (tmp_path / "runs").exists()
    assert record.main(["start"], version=(3, 11, 0)) == 0  # the floor itself passes


def test_old_python_is_refused_before_the_parser_is_built(monkeypatch, tmp_path, capsys):
    """Ubuntu 18.04's python3 is 3.6, whose argparse has no add_subparsers(required=): the parser raises TypeError
    there. The floor must refuse before the parser is built, so that run ends in the plain reason too."""
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))

    def py36_argparse(*a, **k):
        raise TypeError("__init__() got an unexpected keyword argument 'required'")
    monkeypatch.setattr(record.argparse, "ArgumentParser", py36_argparse)
    assert record.main(["start"], version=(3, 6, 9)) == 1
    assert capsys.readouterr().err.startswith("refusing: python3 is 3.6.9, older than 3.11")
    assert not (tmp_path / "runs").exists()


def test_the_python_floor_guard_catches_its_mutant(monkeypatch, tmp_path):
    guard = '    if floor:\n        print(f"refusing: {floor}", file=sys.stderr)\n        return 1\n'
    assert SOURCE.count(guard) == 1, "stale or ambiguous mutation pattern"
    mod = load(SOURCE.replace(guard, ""), tmp_path, "record_floor_mut")
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))
    mod.REPO = record.REPO
    mod.KIT_MD, mod.STEPS_MD = record.KIT_MD, record.STEPS_MD
    assert mod.main(["start"], version=(3, 10, 12)) == 0  # without the guard, an old python3 starts a run


def test_record_refuses_under_a_real_python_3_10_without_a_traceback(tmp_path):
    from test_host_bootstrap import find_python310
    py310 = find_python310()
    if not py310:
        pytest.skip("no Python 3.10 here (uv python install 3.10 provides one)")
    env = {**os.environ, "MC_KIT_RUNS": str(tmp_path / "runs"), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([py310, str(KIT / "record.py"), "start"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "Traceback" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.stderr.startswith("refusing: python3 is 3.10.") and "older than 3.11" in r.stderr
    assert not (tmp_path / "runs").exists()


def test_root_guard_catches_its_mutant(monkeypatch, tmp_path):
    guard = "if os.geteuid() == 0:"
    assert SOURCE.count(guard) == 1
    mod = load(SOURCE.replace(guard, "if False:"), tmp_path)
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    mod.REPO = record.REPO  # the copy lives in tmp_path; point it back at this checkout and its docs
    mod.KIT_MD, mod.STEPS_MD = record.KIT_MD, record.STEPS_MD
    assert mod.main(["start"]) == 0 and (tmp_path / "runs").exists()


@pytest.mark.parametrize("text, match", [
    ("## P0. A\n## P2. B\n", "P0..P1 in order"),
    ("## P0. A\n## P0. B\n", "duplicate"),
    ("no headings\n", "no phase"),
])
def test_phase_headings_are_checked(text, match):
    with pytest.raises(record.RecordError, match=match):
        record.load_phases(text)


@pytest.mark.parametrize("guard, text", [
    ("if list(phases) != want:", "## P0. A\n## P2. B\n"),
    ("if len(set(ids)) != len(ids):", "## P0. A\n## P0. B\n"),
])
def test_heading_guards_catch_their_mutants(tmp_path, guard, text):
    assert SOURCE.count(guard) == 1
    mod = load(SOURCE.replace(guard, "if False:"), tmp_path)
    mod.load_phases(text)  # the mutant accepts what the real one refuses


# ---- the RCON password shape (verify:runbook finding: the filter let the kit's own credential through) -----------

def test_every_kit_minted_rcon_password_is_refused():
    """KIT.md P4 mints RCON_PASSWORD with secrets.token_urlsafe(24). A seeded sample of that exact alphabet and
    length: every value must be refused, so the record can never carry it."""
    import random
    rng = random.Random(20261015)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    sample = ["".join(rng.choice(alphabet) for _ in range(32)) for _ in range(2000)]
    assert [v for v in sample if not record.looks_secret(f"note {v} end")] == []


@pytest.mark.parametrize("text", [
    "0123456789abcdef" * 2 + "01234567",          # a commit id: lower-case hex, 40
    "1600000000000000061",                        # a snowflake id
    "PAUSE_WHEN_EMPTY_SECONDS_AND_A_LONG_TAIL",   # a constant name: upper case only
    "lab-laptop." + "tail1234ab.ts.net",          # a MagicDNS name (assembled: pack/test_build.py scans for one)
    "vanilla-extended-1.0.2-ch1.mrpack",
    "test_the_shell_kit_key_allowlist_is_the_keys_render_writes",  # long, but one case
])
def test_the_long_random_string_shape_lets_ordinary_text_through(text):
    assert record.looks_secret(text) is None


def test_the_upper_case_condition_is_what_lets_a_commit_id_through(tmp_path):
    guard = "(?=[A-Za-z0-9_-]*[A-Z])"
    assert SOURCE.count(guard) == 1
    mod = load(SOURCE.replace(guard, ""), tmp_path, "record_case_mut")
    assert mod.looks_secret("0123456789abcdef" * 2 + "01234567")  # the mutant refuses a commit id


def test_no_gate_kit_md_runs_looks_like_a_secret():
    """Every `record.py gate` line in KIT.md must get past the filter, or the run could not record it."""
    import re
    lines = [m.group(1) for m in re.finditer(r"python3 kit/record\.py gate P\d+ -- (.+)", record.KIT_MD.read_text())]
    assert lines and [ln for ln in lines if record.looks_secret(ln)] == []


# ---- the measured N: required one-time steps never logged (verify:runbook finding) -------------------------------

REQ_STEPS = {"1": "USB", "2": "Install", "3": "(Optional) BIOS", "9": "(Most people skip this) Discord account",
             "19": "(Conditional) Wi-Fi", "F1": "Java", "R1": "Share link"}


def _run_logging(mod, rd, repo, logged, incomplete=False, steps=REQ_STEPS):
    started(mod, rd, repo)
    for s in logged:
        mod.cmd_human(rd, s, steps, retro=True)
    mod.cmd_finish(rd, PHASES, steps, repo=repo, incomplete=incomplete)
    run = mod.current(rd)
    return json.loads((run / "record.json").read_text()), (run / "record.md").read_text()


def test_a_run_missing_a_required_step_headlines_n_as_incomplete(rd, repo):
    s, md = _run_logging(record, rd, repo, ["1"], incomplete=True)
    assert s["human"]["required_not_seen"] == ["2"] and s["human"]["count"] == 1
    assert "**Human steps (N): INCOMPLETE, at least 1.** Required one-time steps never logged: 2" in md
    assert "- **Human steps (N):** 1" not in md


def test_optional_and_conditional_steps_are_not_required(rd, repo):
    s, md = _run_logging(record, rd, repo, ["1", "2"])
    assert s["human"]["required_not_seen"] == [] and s["human"]["planned_not_seen"] == ["3", "9", "19"]
    assert "- **Human steps (N):** 2 (" in md and "INCOMPLETE" not in md


def test_finish_incomplete_says_n_is_incomplete(rd, repo):
    started(record, rd, repo)
    out = record.cmd_finish(rd, PHASES, REQ_STEPS, repo=repo, incomplete=True)
    assert "HUMAN STEPS INCOMPLETE: required one-time steps never logged: 1, 2" in out


def test_finish_refuses_while_a_required_step_is_unlogged_and_the_run_stays_open(rd, repo):
    """The guard fires before the run is sealed: the missing step can still be logged, then finish passes."""
    started(record, rd, repo)
    record.cmd_human(rd, "1", REQ_STEPS, retro=True)
    assert "required one-time steps not logged yet: 2" in record.cmd_status(rd, PHASES, REQ_STEPS)
    with pytest.raises(record.RecordError, match="never logged: 2.*finish --incomplete"):
        record.cmd_finish(rd, PHASES, REQ_STEPS, repo=repo)
    assert not any(e["kind"] == "finish" for e in events(record, rd))  # not sealed
    record.cmd_human(rd, "2", REQ_STEPS, note="logged late")
    assert "required one-time steps not logged yet: none" in record.cmd_status(rd, PHASES, REQ_STEPS)
    assert "INCOMPLETE" not in record.cmd_finish(rd, PHASES, REQ_STEPS, repo=repo)


def test_a_friend_or_recurring_step_is_logged_but_never_counted_in_n(rd, repo):
    """KIT.md P11 logs F10 (the host whitelisting themself, as the bridge check): N stays the one-time count."""
    steps = {**{k: v for k, v in REQ_STEPS.items() if k != "R1"}, "F10": "Whitelist yourself", "R1": "Share link"}
    s, md = _run_logging(record, rd, repo, ["1", "2", "F10", "R1"], steps=steps)
    assert s["human"]["count"] == 2 and s["human"]["planned_seen"] == ["1", "2"]
    assert s["human"]["other_seen"] == ["F10", "R1"] and s["human"]["actions"] == 4
    assert "- **Human steps (N):** 2 (2 from docs/kit/HUMAN-STEPS.md, 0 not on the list)" in md
    assert "- **Also logged, not in N (a friend's or a recurring step):** F10. Whitelist yourself, R1. Share link" in md


N_MUTANTS = {
    "headline": ('if s["human"]["required_not_seen"] else', 'if False else'),
    "required_filter": ('not title.startswith("(") and s not in without]', 's not in without]'),
    "finish_flag": ('if missing else ""', 'if False else ""'),
    "finish_refusal": ("if unlogged and not incomplete:", "if False:"),
    "count_one_time_only": ("planned_seen = [s for s in seen if s.isdigit()]", "planned_seen = list(seen)"),
}


@pytest.mark.parametrize("name", sorted(N_MUTANTS))
def test_each_incomplete_n_guard_catches_its_mutant(tmp_path, repo, name):
    guard, mutant = N_MUTANTS[name]
    assert SOURCE.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    mod = load(SOURCE.replace(guard, mutant), tmp_path, f"record_n_{name}")
    rd = tmp_path / "runs"
    if name == "finish_flag":
        started(mod, rd, repo)
        assert "INCOMPLETE" not in mod.cmd_finish(rd, PHASES, REQ_STEPS, repo=repo, incomplete=True)
        return
    if name == "finish_refusal":
        started(mod, rd, repo)
        mod.cmd_finish(rd, PHASES, REQ_STEPS, repo=repo)  # the mutant seals a run that misses steps 1 and 2
        assert any(e["kind"] == "finish" for e in events(mod, rd))
        return
    if name == "count_one_time_only":
        s, _ = _run_logging(mod, rd, repo, ["1", "2", "F1"])
        assert s["human"]["count"] == 3  # the mutant counts the friend's step in N
        return
    logged = ["1"] if name == "headline" else ["1", "2"]
    s, md = _run_logging(mod, rd, repo, logged, incomplete=True)
    if name == "headline":
        assert "INCOMPLETE" not in md  # the mutant headlines a floor as the measured N
    else:
        assert s["human"]["required_not_seen"] == ["3", "9", "19"]  # the mutant demands the optional steps


# ---- began: a gate reads the logs since its phase began (proof run 2026-10-16: P11's `--since 10m` missed a
# message sent 12 hours into the phase) -----------------------------------------------------------------------------

def _p0_twice(mod, rd, repo):
    """P0 begins at 0, fails, and is re-run from 5: a gate in the re-run wants the second begin."""
    started(mod, rd, repo)
    mod.cmd_begin(rd, "P0", PHASES, now=at(0))
    mod.cmd_end(rd, "P0", now=at(1))  # no gate: fail
    mod.cmd_begin(rd, "P0", PHASES, now=at(5))


def test_began_is_the_latest_begin_in_compose_s_since_format(rd, repo):
    _p0_twice(record, rd, repo)
    assert record.cmd_began(rd, "P0", PHASES) == "2026-10-15T12:05:00Z"
    with pytest.raises(record.RecordError, match="P1 never began"):
        record.cmd_began(rd, "P1", PHASES)
    with pytest.raises(record.RecordError, match=r"P9 never began in this run \(KIT.md has P0, P1, P2, P3\)"):
        record.cmd_began(rd, "P9", PHASES)


def test_began_reads_a_finished_run(rd, repo):
    _p0_twice(record, rd, repo)
    record.cmd_gate(rd, "P0", ["true"], runner=ok(0), now=at(6))
    record.cmd_end(rd, "P0", now=at(7))
    record.cmd_finish(rd, PHASES, STEPS, repo=repo, now=at(8), incomplete=True)
    assert record.cmd_began(rd, "P0", PHASES) == "2026-10-15T12:05:00Z"


def test_began_latest_catches_its_mutant(tmp_path, repo):
    guard = "    return begun[-1]\n"
    assert SOURCE.count(guard) == 1, "stale or ambiguous mutation pattern"
    mod = load(SOURCE.replace(guard, "    return begun[0]\n"), tmp_path, "record_began_first")
    _p0_twice(mod, tmp_path / "runs", repo)
    assert mod.cmd_began(tmp_path / "runs", "P0", PHASES) == "2026-10-15T12:00:00Z"  # the failed first attempt


def test_began_never_begun_catches_its_mutant(tmp_path, repo):
    guard = "    if not begun:\n"
    assert SOURCE.count(guard) == 1, "stale or ambiguous mutation pattern"
    mod = load(SOURCE.replace(guard, "    if False:\n"), tmp_path, "record_began_never")
    _p0_twice(mod, tmp_path / "runs", repo)
    with pytest.raises(IndexError):  # no refusal naming the phase: a bare traceback instead
        mod.cmd_began(tmp_path / "runs", "P1", PHASES)


def test_began_cli_prints_only_the_time(tmp_path, repo):
    env = {**os.environ, "MC_KIT_RUNS": str(tmp_path / "cli-runs")}

    def cli(*args):
        return subprocess.run(["python3", str(KIT / "record.py"), *args], env=env, capture_output=True, text=True)

    assert cli("start").returncode == 0
    r = cli("began", "P0")
    assert r.returncode == 1 and r.stdout == "" and "P0 never began" in r.stderr
    assert cli("begin", "P0").returncode == 0
    r = cli("began", "P0")
    assert r.returncode == 0 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n", r.stdout), r.stdout


# ---- no Discord (kit.toml discord.enabled = false): the Discord phases are skipped, its steps not required -----------

REAL_PHASES = record.load_phases(record.KIT_MD.read_text())
REAL_STEPS_TEXT = record.STEPS_MD.read_text()
REAL_STEPS = record.load_steps(REAL_STEPS_TEXT)
WITHOUT = record.discord_steps(REAL_STEPS_TEXT)


def _through(mod, rd, upto, discord=False, minute=1):
    """Every phase before `upto` in order: a passing gate each, and a skip for each Discord phase (no Discord)."""
    for p in REAL_PHASES:
        if p == upto:
            return
        if p in mod.DISCORD_PHASES and not discord:
            mod.cmd_skip(rd, p, REAL_PHASES, discord, now=at(minute))
        else:
            mod.cmd_begin(rd, p, REAL_PHASES, now=at(minute))
            mod.cmd_gate(rd, p, ["true"], now=at(minute), runner=ok(0))
            mod.cmd_end(rd, p, now=at(minute))
        minute += 1


def test_the_discord_steps_are_the_discord_section_of_human_steps():
    assert WITHOUT == tuple(str(n) for n in range(9, 17))
    assert len(record.required_steps(REAL_STEPS)) == 19 and len(record.required_steps(REAL_STEPS, WITHOUT)) == 12


def test_the_discord_section_is_required_to_exist():
    with pytest.raises(record.RecordError, match="You, once: Discord"):
        record.discord_steps(REAL_STEPS_TEXT.replace("## You, once: Discord", "## You, once: chat"))


def test_the_discord_phases_are_p1_p6_p11_and_kit_md_skips_exactly_those():
    """DISCORD_PHASES is the second statement of which KIT.md phases are Discord's alone: KIT.md gives
    `record.py skip Pn` in exactly those phases' sections, so a phase added to one and not the other fails here."""
    assert record.DISCORD_PHASES == ("P1", "P6", "P11")
    text = record.KIT_MD.read_text()
    heads = [m.start() for m in record.PHASE_HEAD.finditer(text)] + [len(text)]
    skipped_in = {}
    for (p, _), a, b in zip(record.PHASE_HEAD.findall(text), heads, heads[1:]):
        skipped_in[p] = re.findall(r"python3 kit/record\.py skip (P\d+)\b", text[a:b])
    assert {p: s for p, s in skipped_in.items() if s} == {p: [p] for p in record.DISCORD_PHASES}


def test_a_run_with_no_discord_skips_its_phases_and_says_so(rd, repo):
    run = started(record, rd, repo)
    for s in record.required_steps(REAL_STEPS, WITHOUT):
        record.cmd_human(rd, s, REAL_STEPS, retro=True)
    _through(record, rd, None)
    record.cmd_finish(rd, REAL_PHASES, REAL_STEPS, repo=repo, now=at(90), discord=False, without=WITHOUT)
    s = json.loads((run / "record.json").read_text())
    assert s["outcome"] == "complete; skipped, no Discord (kit.toml discord.enabled = false): P1, P6, P11"
    assert s["discord"] is False and s["human"]["required_not_seen"] == []
    assert not set(WITHOUT) & set(s["human"]["planned_not_seen"])  # not part of the run, so not "unseen"
    rows = {r["phase"]: r for r in s["phases"]}
    assert [p for p, r in rows.items() if r["status"] == "skipped"] == ["P1", "P6", "P11"]
    md = (run / "record.md").read_text()
    assert "- **Discord:** none" in md and "| skipped (no Discord" in md
    assert "**Outcome:** complete\n" not in md


def test_status_names_the_choice_and_its_required_steps(rd, repo):
    started(record, rd, repo)
    with_discord = record.cmd_status(rd, REAL_PHASES, REAL_STEPS)
    assert "discord:" not in with_discord and "10, 11" in with_discord  # a Discord run's status reads as before
    without = record.cmd_status(rd, REAL_PHASES, REAL_STEPS, False, WITHOUT)
    assert "discord: none" in without and "10" not in without.split("not logged yet:")[1]


def _skip_with_discord(mod, rd, repo, tmp):
    started(mod, rd, repo)
    _through(mod, rd, "P1", discord=True)
    mod.cmd_skip(rd, "P1", REAL_PHASES, True, now=at(30))


def _skip_a_phase_every_server_runs(mod, rd, repo, tmp):
    started(mod, rd, repo)
    _through(mod, rd, "P2")
    mod.cmd_skip(rd, "P2", REAL_PHASES, False, now=at(30))


def _skip_out_of_order(mod, rd, repo, tmp):
    started(mod, rd, repo)
    _through(mod, rd, "P1")  # P1 and later not run: P6 cannot be skipped yet
    mod.cmd_skip(rd, "P6", REAL_PHASES, False, now=at(30))


def _skip_twice(mod, rd, repo, tmp):
    started(mod, rd, repo)
    _through(mod, rd, "P2")  # P1 already skipped
    mod.cmd_skip(rd, "P1", REAL_PHASES, False, now=at(30))


def _finish_discord_after_skips(mod, rd, repo, tmp):
    started(mod, rd, repo)
    for s in record.required_steps(REAL_STEPS):
        mod.cmd_human(rd, s, REAL_STEPS, retro=True)
    _through(mod, rd, None)  # skipped as no Discord ...
    mod.cmd_finish(rd, REAL_PHASES, REAL_STEPS, repo=repo, now=at(90), discord=True)  # ... and finished as Discord


def _begin_a_discord_phase_with_no_discord(mod, rd, repo, tmp):
    started(mod, rd, repo)
    _through(mod, rd, "P1")
    mod.cmd_begin(rd, "P1", REAL_PHASES, now=at(30), discord=False)


def _skips(mod, rd):
    return sum(e["kind"] == "skip" for e in mod.read_events(mod.current(rd)))


SKIP_GUARDS = {
    "skip_with_discord": (_skip_with_discord, "    if discord:\n        raise RecordError(f\"kit.toml says Discord",
                          "    if False:\n        raise RecordError(f\"kit.toml says Discord",
                          lambda m, rd: _skips(m, rd) == 1),
    "skip_any_phase": (_skip_a_phase_every_server_runs, "if phase not in DISCORD_PHASES:", "if False:",
                       lambda m, rd: any(e.get("phase") == "P2" and e["kind"] == "skip"
                                         for e in m.read_events(m.current(rd)))),
    "skip_out_of_order": (_skip_out_of_order, "    check_begin(events, phase, phases)\n    _, status",
                          "    _, status", lambda m, rd: any(e.get("phase") == "P6" for e in m.read_events(m.current(rd)))),
    "skip_twice": (_skip_twice, "if status.get(phase) in PASSING + (SKIPPED,):", "if False:",
                   lambda m, rd: sum(e.get("phase") == "P1" for e in m.read_events(m.current(rd))) == 2),
    "finish_discord_after_skips": (_finish_discord_after_skips, "if discord and skipped:", "if False:", _finished),
    "begin_discord_phase_with_no_discord": (
        _begin_a_discord_phase_with_no_discord, "    if phase in DISCORD_PHASES and not discord:", "    if False:",
        lambda m, rd: any(e["kind"] == "begin" and e["phase"] == "P1" for e in m.read_events(m.current(rd)))),
}


@pytest.mark.parametrize("name", sorted(SKIP_GUARDS))
def test_skip_guard_refuses(tmp_path, repo, name):
    scenario, *_, went_wrong = SKIP_GUARDS[name]
    rd = tmp_path / "runs"
    with pytest.raises(record.RecordError):
        scenario(record, rd, repo, tmp_path)
    assert not went_wrong(record, rd)


@pytest.mark.parametrize("name", sorted(SKIP_GUARDS))
def test_each_skip_guard_catches_its_mutant(tmp_path, repo, name):
    scenario, guard, mutant, went_wrong = SKIP_GUARDS[name]
    assert SOURCE.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    mod = load(SOURCE.replace(guard, mutant), tmp_path, f"record_skip_{name}")
    rd = tmp_path / "runs"
    try:
        scenario(mod, rd, repo, tmp_path)
    except mod.RecordError:
        pass
    assert went_wrong(mod, rd), name


def test_the_required_set_follows_the_choice_and_its_filter_catches_its_mutant(rd, repo, tmp_path):
    """With Discord, finish refuses without steps 10-16; with no Discord it does not ask for them. The mutant that
    drops the `without` filter asks for them with no Discord too."""
    no_discord = record.required_steps(REAL_STEPS, WITHOUT)
    started(record, rd, repo)
    for s in no_discord:
        record.cmd_human(rd, s, REAL_STEPS, retro=True)
    with pytest.raises(record.RecordError, match=r"never logged: 10, 11, 12, 13, 14, 15, 16\. "):
        record.cmd_finish(rd, PHASES, REAL_STEPS, repo=repo)
    guard = 'not title.startswith("(") and s not in without]'
    mod = load(SOURCE.replace(guard, 'not title.startswith("(")]'), tmp_path, "record_without")
    assert SOURCE.count(guard) == 1
    assert len(mod.required_steps(REAL_STEPS, WITHOUT)) == 19  # the mutant requires the Discord steps anyway
    record.cmd_finish(rd, PHASES, REAL_STEPS, repo=repo, discord=False, without=WITHOUT)  # no Discord: complete


@pytest.mark.parametrize("text,want", [(None, True), ("[discord]\nguild_id = \"1\"\n", True),
                                       ("[discord]\nenabled = true\n", True), ("[discord]\nenabled = false\n", False)])
def test_the_record_reads_the_choice_from_kit_toml_as_written(tmp_path, text, want):
    f = tmp_path / "kit.toml"
    if text is not None:
        f.write_text(text)
    assert record.discord_on(f) is want


def test_a_choice_that_is_not_true_or_false_is_refused(tmp_path):
    (tmp_path / "kit.toml").write_text('[discord]\nenabled = "no"\n')
    with pytest.raises(record.RecordError, match="true or false"):
        record.discord_on(tmp_path / "kit.toml")


def test_the_skip_command_reads_kit_toml(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))
    (tmp_path / "kit.toml").write_text("[discord]\nenabled = false\n")
    monkeypatch.setenv("MC_KIT_TOML", str(tmp_path / "kit.toml"))
    rd = tmp_path / "runs"
    started(record, rd, repo)
    record.cmd_begin(rd, "P0", REAL_PHASES, now=at(1))
    record.cmd_gate(rd, "P0", ["true"], now=at(1), runner=ok(0))
    record.cmd_end(rd, "P0", now=at(1))
    assert record.main(["skip", "P1"]) == 0 and "skipped, no Discord" in capsys.readouterr().out
    (tmp_path / "kit.toml").write_text("[discord]\nenabled = true\n")
    assert record.main(["skip", "P6"]) == 1 and "kit.toml says Discord" in capsys.readouterr().err


def test_a_phase_skipped_then_run_as_discord_lets_the_run_finish(rd, repo, tmp_path):
    """Skipped for no Discord, then the person chose Discord and the phase was begun and passed: finish reads the
    phase's status now, so it seals. The mutant that reads the skip events ever recorded refuses it for good."""
    def scenario(mod, rd):
        started(mod, rd, repo)
        for s in mod.required_steps(REAL_STEPS):
            mod.cmd_human(rd, s, REAL_STEPS, retro=True)
        _through(mod, rd, "P2")  # P1 skipped (no Discord) ...
        mod.cmd_begin(rd, "P1", REAL_PHASES, now=at(20))  # ... then Discord after all: P1 runs and passes
        mod.cmd_gate(rd, "P1", ["true"], now=at(20), runner=ok(0))
        mod.cmd_end(rd, "P1", now=at(20))
        _through(mod, rd, None, discord=True, minute=21)
        return mod.cmd_finish(rd, REAL_PHASES, REAL_STEPS, repo=repo, now=at(90), discord=True)
    scenario(record, rd)
    s = json.loads((record.current(rd) / "record.json").read_text())
    assert s["outcome"] == "complete" and "discord" not in s
    guard = "skipped = [p for p, st in status.items() if st == SKIPPED]"
    assert SOURCE.count(guard) == 1
    mod = load(SOURCE.replace(guard, 'skipped = [e["phase"] for e in events if e["kind"] == "skip"]'), tmp_path,
               "record_skip_history")
    with pytest.raises(mod.RecordError, match="were skipped for no Discord"):
        scenario(mod, tmp_path / "runs2")


def test_a_discord_run_record_has_no_discord_line(rd, repo):
    """Discord mode is unchanged: no "Discord:" line in record.md, no "discord" key in record.json or the finish
    event (they appear only with no Discord)."""
    run = started(record, rd, repo)
    for s in record.required_steps(REAL_STEPS):
        record.cmd_human(rd, s, REAL_STEPS, retro=True)
    _through(record, rd, None, discord=True)
    record.cmd_finish(rd, REAL_PHASES, REAL_STEPS, repo=repo, now=at(90))
    assert "Discord:" not in (run / "record.md").read_text()
    assert "discord" not in json.loads((run / "record.json").read_text())
    assert all("discord" not in e for e in record.read_events(run))


def test_begin_reads_kit_toml_only_for_a_discord_phase(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("MC_KIT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("MC_KIT_TOML", str(tmp_path / "kit.toml"))
    rd = tmp_path / "runs"
    started(record, rd, repo)
    (tmp_path / "kit.toml").write_text("not [valid toml")  # mid-edit: begin P0 never reads it
    assert record.main(["begin", "P0"]) == 0
    record.cmd_gate(rd, "P0", ["true"], now=at(1), runner=ok(0))
    record.cmd_end(rd, "P0", now=at(1))
    (tmp_path / "kit.toml").write_text("[discord]\nenabled = false\n")
    assert record.main(["begin", "P1"]) == 1 and "record.py skip P1" in capsys.readouterr().err
    (tmp_path / "kit.toml").write_text("[discord]\nenabled = true\n")
    assert record.main(["begin", "P1"]) == 0
