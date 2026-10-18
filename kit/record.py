#!/usr/bin/env python3
"""The kit run recorder: what a run did, when, with which code, and every human step it took.

Claude runs it as the kit user at each point docs/kit/KIT.md names:

  python3 kit/record.py start                    a new run: the git SHA (and whether the tree is dirty), the time
  python3 kit/record.py begin P3                 a phase starts (phases run in order, one at a time)
  python3 kit/record.py gate P3 -- <command...>  run a gate command; its output passes through, its exit code is kept
  python3 kit/record.py observe P11 "<what was seen>"   a gate only a person can see (labelled as observed, never
                                                 as a command result)
  python3 kit/record.py human 11 [--note "..."]  a human step from docs/kit/HUMAN-STEPS.md, logged as it happens
  python3 kit/record.py human 1 --retro          a step done before the run started (the OS install), as reported
  python3 kit/record.py human --unplanned "<what>"   a step the list does not have (a captcha, a 2FA prompt)
  python3 kit/record.py end P3 [--accept "<why>"]    the phase ends; its status is DERIVED from its gates
  python3 kit/record.py end P3 --accept "<why>"  right after a failed `end P3`, before anything else begins: the
                                                 person accepts that failure (the gates are not re-run)
  python3 kit/record.py status                   where the run is
  python3 kit/record.py began P11                print when P11 last began (UTC, 2026-10-16T03:07:48Z), for a gate
                                                 that reads logs since then: docker compose logs --since takes it
  python3 kit/record.py finish                   write record.json + record.md for the hand-back; refused while a
                                                 required one-time step is unlogged
  python3 kit/record.py finish --incomplete      a run that stops early, or misses a step: N is then a floor

A phase passes only on recorded gates: at least one, and every command gate exited 0. `end` takes no "pass" from
anyone. A failed phase blocks the next `begin` until it is re-run, or until `end --accept "<why>"` records the
failure as accepted, which the record shows in full. `--accept` works on the open phase, or on the phase that has
just ended in a failure, as long as nothing has begun since and it is not accepted already (the usual order: end,
read the failure, ask the person, then accept).

The phases (P0..P12) come from docs/kit/KIT.md's "## Pn. Title" headings and the human steps from
docs/kit/HUMAN-STEPS.md's "### <id>. Title" headings, their one home each.

The record goes to the hand-back, so a note, an observation or a gate command that looks like a secret (a Discord
token, a Tailscale key or share link, a webhook URL, a long random string such as the RCON password the kit mints)
is refused. A gate's output is shown, never stored.
The measured N counts the host owner's one-time steps (digit ids) and the unplanned ones; a friend's (F) or a
recurring (R) step can be logged, but it is listed on its own line and never counted in N. N is the measured N only
when every required one-time step was logged (KIT.md logs steps 1-6 with --retro at the start): `finish` refuses
while any is missing, so the run stays open and the step can still be logged; `finish --incomplete` seals it anyway,
and record.md then headlines N as INCOMPLETE, a floor. `status` names the required steps not logged yet.
Files: kit/runs/<run id>/ (gitignored; $MC_KIT_RUNS overrides, for tests). Stdlib only; it needs no kit.toml.
It is the first kit command a run executes (KIT.md "Before P0"), so it refuses a python3 under the kit's floor
(kit/probe.py PYTHON_FLOOR, 3.11) with a plain reason, and must itself run on an older one: stdlib only, no
3.11-only syntax or imports.
"""
import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from probe import python_floor_problem  # the floor's one home; probe.py is stdlib only and import-safe

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
KIT_MD = REPO / "docs" / "kit" / "KIT.md"
STEPS_MD = REPO / "docs" / "kit" / "HUMAN-STEPS.md"

PHASE_HEAD = re.compile(r"^## (P\d+)\. (.+?)\s*$", re.M)
STEP_HEAD = re.compile(r"^### ((?:[FR])?\d+)\. (.+?)\s*$", re.M)
PASSING = ("pass", "pass-observed", "accepted-fail")
# Shapes that must never reach a record that leaves the machine.
SECRETS = (
    ("a Discord token", re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")),
    ("a Tailscale key", re.compile(r"tskey-[A-Za-z0-9-]+")),
    ("a Tailscale share link", re.compile(r"login\.tailscale\.com/admin/invite/[A-Za-z0-9]")),
    ("a Discord webhook URL", re.compile(r"discord(?:app)?\.com/api/webhooks/\d")),
    # A long random string with both cases: the RCON password the kit mints (KIT.md P4: secrets.token_urlsafe(24),
    # 32 urlsafe characters) or any token like it. Whole runs of the urlsafe alphabet only, so a commit SHA (lower-case
    # hex), a snowflake id (digits) and a constant name (upper case) pass; a digit is not required, because about 1
    # in 230 token_urlsafe(24) values has none.
    ("a long random string (a password or token)",
     re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")),
)


class RecordError(Exception):
    pass


# ---- pure -----------------------------------------------------------------------------------------------------

def now_iso(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def headings(text, pattern, what):
    found = pattern.findall(text)
    ids = [i for i, _ in found]
    if not found:
        raise RecordError(f"no {what} headings found")
    if len(set(ids)) != len(ids):
        raise RecordError(f"duplicate {what} ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    return dict(found)


def load_phases(text):
    phases = headings(text, PHASE_HEAD, "phase (## Pn. Title)")
    want = [f"P{n}" for n in range(len(phases))]
    if list(phases) != want:
        raise RecordError(f"KIT.md phases must be P0..P{len(phases) - 1} in order, found {list(phases)}")
    return phases


def load_steps(text):
    return headings(text, STEP_HEAD, "human step (### <id>. Title)")


def required_steps(steps):
    """The host owner's one-time steps every run must log: a digit id whose title is not marked "(Optional)",
    "(Conditional)" or "(Most people skip this)" (HUMAN-STEPS.md puts the mark in parentheses at the start)."""
    return [s for s, title in steps.items() if s.isdigit() and not title.startswith("(")]


def looks_secret(text):
    for kind, pattern in SECRETS:
        if pattern.search(text):
            return kind
    return None


def refuse_secret(text, where):
    kind = looks_secret(text)
    if kind:
        raise RecordError(f"that {where} looks like {kind}, and the run record is handed back: describe it instead")


def phase_state(events):
    """-> (open phase or None, {phase: last end status})."""
    open_phase, status = None, {}
    for e in events:
        if e["kind"] == "begin":
            open_phase = e["phase"]
        elif e["kind"] == "end":
            status[e["phase"]] = e["status"]
            open_phase = None
        elif e["kind"] == "accept":  # a failure accepted after its phase ended (cmd_end)
            status[e["phase"]] = "accepted-fail"
    return open_phase, status


def last_phase_event(events):
    return next((e for e in reversed(events) if e["kind"] in ("begin", "end", "accept")), None)


def gates_since_begin(events, phase):
    out = []
    for e in events:
        if e["kind"] == "begin" and e["phase"] == phase:
            out = []
        elif e["kind"] in ("gate", "observe") and e["phase"] == phase:
            out.append(e)
    return out


def derive_status(gates):
    if not gates:
        return "fail", "no gate was recorded"
    bad = [g for g in gates if g["kind"] == "gate" and g["rc"] != 0]
    if bad:
        return "fail", f"{len(bad)} gate(s) exited non-zero: " + "; ".join(f"{' '.join(g['argv'])} -> {g['rc']}" for g in bad)
    if any(g["kind"] == "observe" for g in gates):
        return "pass-observed", ""
    return "pass", ""


def check_begin(events, phase, phases):
    if phase not in phases:
        raise RecordError(f"unknown phase {phase}; KIT.md has {', '.join(phases)}")
    open_phase, status = phase_state(events)
    if open_phase:
        raise RecordError(f"{open_phase} is still open: end it first (python3 kit/record.py end {open_phase})")
    for p in phases:
        if p == phase:
            break
        if status.get(p) not in PASSING:
            raise RecordError(f"{p} has not passed (status: {status.get(p, 'not run')}); phases run in order")


def check_open(events, phase):
    open_phase, _ = phase_state(events)
    if open_phase != phase:
        raise RecordError(f"{phase} is not the open phase (open: {open_phase or 'none'}); "
                          f"begin it first: python3 kit/record.py begin {phase}")


def duration(a, b):
    secs = int((parse_iso(b) - parse_iso(a)).total_seconds())
    h, rem = divmod(secs, 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


def summarize(events, phases, steps):
    start = next(e for e in events if e["kind"] == "start")
    fin = next((e for e in events if e["kind"] == "finish"), None)
    rows, cur = [], None
    for e in events:
        if e["kind"] == "begin":
            cur = {"phase": e["phase"], "title": phases.get(e["phase"], "?"), "begin": e["t"], "gates": []}
            rows.append(cur)
        elif e["kind"] in ("gate", "observe") and cur is not None:
            cur["gates"].append(e)
        elif e["kind"] == "end" and cur is not None:
            cur.update(end=e["t"], status=e["status"], why=e.get("why", ""), accepted=e.get("accepted", ""),
                       took=duration(cur["begin"], e["t"]))
            cur = None
        elif e["kind"] == "accept":
            row = next(r for r in reversed(rows) if r["phase"] == e["phase"])
            row.update(status="accepted-fail", accepted=e["accepted"])
    humans = [e for e in events if e["kind"] == "human"]
    seen = sorted({e["step"] for e in humans if e.get("step")}, key=lambda s: list(steps).index(s))
    planned_seen = [s for s in seen if s.isdigit()]  # the host owner's one-time steps: what N counts
    other_seen = [s for s in seen if not s.isdigit()]  # friends' (F) and recurring (R) steps: logged, never in N
    unplanned = [e for e in humans if not e.get("step")]
    _, status = phase_state(events)
    last = phases and list(phases)[-1]
    complete = all(status.get(p) in ("pass", "pass-observed") for p in phases)
    return {
        "run": start["run"],
        "host": start["host"],
        "sha_start": start["sha"], "dirty_start": start["dirty"],
        "sha_finish": fin["sha"] if fin else None, "dirty_finish": fin["dirty"] if fin else None,
        "started": start["t"], "finished": fin["t"] if fin else None,
        "took": duration(start["t"], fin["t"]) if fin else None,
        "outcome": ("complete" if complete else
                    "complete, with accepted failures" if all(status.get(p) in PASSING for p in phases) else
                    f"incomplete (last ended: {max(status, key=lambda p: int(p[1:])) if status else 'none'}; "
                    f"{last} needed)"),
        "phases": rows,
        "human": {
            "planned_seen": planned_seen,
            "other_seen": other_seen,
            # the host owner's one-time steps only: friends' (F) and recurring (R) steps are not part of a setup run
            "planned_not_seen": [s for s in steps if s.isdigit() and s not in planned_seen],
            "unplanned": [e["what"] for e in unplanned],
            # the required one-time steps never logged (KIT.md logs 1-6 with --retro at the start): while any is
            # missing, `count` is a floor, never the measured N, and record.md says so in its headline
            "required_not_seen": [s for s in required_steps(steps) if s not in planned_seen],
            "count": len(planned_seen) + len(unplanned),  # N: each one-time step once, plus every unplanned one
            "actions": len(humans),                         # a step done twice counts twice here
            "log": humans,
        },
    }


def cell(text):
    """Text safe inside one markdown table cell."""
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_md(s, steps):
    out = [f"# Kit run {s['run']}", "",
           f"- **Host:** {s['host']}",
           f"- **Code:** `{s['sha_start']}`" + (" (dirty tree)" if s["dirty_start"] else ""),
           ]
    if s["sha_finish"] and s["sha_finish"] != s["sha_start"]:
        out.append(f"- **The code changed during the run:** finished at `{s['sha_finish']}`"
                   + (" (dirty tree)" if s["dirty_finish"] else ""))
    elif s["sha_finish"] and s["dirty_finish"] and not s["dirty_start"]:
        out.append("- **The tree became dirty during the run.**")
    out += [f"- **Started:** {s['started']}" + (f" · **finished:** {s['finished']} · **took:** {s['took']}"
                                                if s["finished"] else " · not finished"),
            f"- **Outcome:** {s['outcome']}",
            (f"- **Human steps (N): INCOMPLETE, at least {s['human']['count']}.** Required one-time steps never "
             f"logged: {', '.join(s['human']['required_not_seen'])}, so this count is a floor, not the measured N"
             if s["human"]["required_not_seen"] else
             f"- **Human steps (N):** {s['human']['count']}") +
            f" ({len(s['human']['planned_seen'])} from "
            f"docs/kit/HUMAN-STEPS.md, {len(s['human']['unplanned'])} not on the list); "
            f"{s['human']['actions']} logged in all (a repeated step counts again there)",
            ]
    if s["human"]["other_seen"]:
        out.append("- **Also logged, not in N (a friend's or a recurring step):** "
                   + ", ".join(f"{i}. {steps.get(i, '?')}" for i in s["human"]["other_seen"]))
    out += ["", "## Phases", "", "| Phase | Began (UTC) | Took | Status | Gates |", "|---|---|---|---|---|"]
    for r in s["phases"]:
        gates = "<br>".join(
            (f"`{cell(' '.join(g['argv']))}` → {g['rc']}" if g["kind"] == "gate" else f"observed: {cell(g['what'])}")
            for g in r["gates"]) or "none"
        status = r.get("status", "OPEN")
        if r.get("accepted"):
            status += f" (accepted: {cell(r['accepted'])})"
        elif r.get("why"):
            status += f" ({cell(r['why'])})"
        out.append(f"| {r['phase']}. {cell(r['title'])} | {r['begin']} | {r.get('took', '-')} | {status} | {gates} |")
    out += ["", "## Human steps, in the order they happened", "",
            "| When (UTC) | Phase | Step | Note |", "|---|---|---|---|"]
    for e in s["human"]["log"]:
        step = (f"{e['step']}. {steps.get(e['step'], '?')}" if e.get("step") else f"NOT ON THE LIST: {e['what']}")
        when = e["t"] + (" (done before the run; reported)" if e.get("retro") else "")
        out.append(f"| {when} | {e.get('phase') or '-'} | {cell(step)} | {cell(e.get('note', ''))} |")
    if s["human"]["planned_not_seen"]:
        out += ["", "**One-time steps on the list but not seen in this run:** "
                + ", ".join(s["human"]["planned_not_seen"])]
    return "\n".join(out) + "\n"


# ---- files ----------------------------------------------------------------------------------------------------

def runs_dir():
    return Path(os.environ.get("MC_KIT_RUNS") or REPO / "kit" / "runs")


def current(rd):
    p = rd / "current"
    if not p.is_file():
        return None
    run = rd / p.read_text().strip()
    return run if (run / "events.jsonl").is_file() else None


def need_run(rd):
    run = current(rd)
    if not run:
        raise RecordError("no run in progress: python3 kit/record.py start")
    return run


def need_open_run(rd):
    """The current run, refused once finished: its record.md is written, so a later event would never reach it."""
    run = need_run(rd)
    if any(e["kind"] == "finish" for e in read_events(run)):
        raise RecordError(f"run {run.name} is finished; python3 kit/record.py start begins a new one")
    return run


def read_events(run):
    return [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]


def append(run, event):
    with open(run / "events.jsonl", "a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def write_atomic(path, text):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def git_sha(repo):
    try:
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True).stdout.strip() != ""
    except (OSError, subprocess.CalledProcessError) as e:
        raise RecordError(f"cannot read the git SHA of {repo} ({e}); a run record without its code is no record")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RecordError(f"git gave '{sha}', not a commit SHA; a run record without its code is no record")
    return sha, dirty


# ---- commands -------------------------------------------------------------------------------------------------

def cmd_start(rd, repo=REPO, now=None):
    run = current(rd)
    if run and not any(e["kind"] == "finish" for e in read_events(run)):
        raise RecordError(f"run {run.name} is not finished: python3 kit/record.py finish (a run that stopped early "
                          "is sealed with finish --incomplete, and recorded as incomplete)")
    sha, dirty = git_sha(repo)
    t = now_iso(now)
    run_id = t.replace("-", "").replace(":", "")
    run = rd / run_id
    run.mkdir(parents=True, exist_ok=False)
    append(run, {"kind": "start", "t": t, "run": run_id, "sha": sha, "dirty": dirty, "host": socket.gethostname()})
    write_atomic(rd / "current", run_id + "\n")
    return f"run {run_id} started at {sha[:12]}" + (" (dirty tree)" if dirty else "")


def cmd_begin(rd, phase, phases, now=None):
    run = need_open_run(rd)
    check_begin(read_events(run), phase, phases)
    append(run, {"kind": "begin", "t": now_iso(now), "phase": phase})
    return f"{phase}. {phases[phase]}: begun"


def cmd_gate(rd, phase, argv, now=None, runner=subprocess.call):
    run = need_open_run(rd)
    check_open(read_events(run), phase)
    if not argv:
        raise RecordError("no gate command given: python3 kit/record.py gate P4 -- <command ...>")
    refuse_secret(" ".join(argv), "gate command")
    t = now_iso(now)
    try:
        rc = runner(argv)
    except FileNotFoundError:
        rc = 127
    append(run, {"kind": "gate", "t": t, "phase": phase, "argv": argv, "rc": rc})
    return rc, f"{phase} gate `{' '.join(argv)}` -> exit {rc}" + ("" if rc == 0 else " (FAILED)")


def cmd_observe(rd, phase, what, now=None):
    run = need_open_run(rd)
    check_open(read_events(run), phase)
    if not what.strip():
        raise RecordError("say what was observed")
    refuse_secret(what, "observation")
    append(run, {"kind": "observe", "t": now_iso(now), "phase": phase, "what": what})
    return f"{phase} observed: {what}"


def cmd_human(rd, step, steps, note="", unplanned="", retro=False, now=None):
    run = need_open_run(rd)
    if bool(step) == bool(unplanned):
        raise RecordError("give a step id from docs/kit/HUMAN-STEPS.md, or --unplanned \"<what>\", not both")
    if step and step not in steps:
        raise RecordError(f"no human step {step} in docs/kit/HUMAN-STEPS.md (ids: {', '.join(steps)}); "
                          "a step the list lacks is --unplanned \"<what>\"")
    for text, where in ((note, "note"), (unplanned, "description")):
        if text:
            refuse_secret(text, where)
    open_phase, _ = phase_state(read_events(run))
    e = {"kind": "human", "t": now_iso(now), "phase": open_phase}
    if step:
        e["step"] = step
    else:
        e["what"] = unplanned
    if note:
        e["note"] = note
    if retro:
        e["retro"] = True
    append(run, e)
    return f"human step logged: {step + '. ' + steps[step] if step else 'NOT ON THE LIST: ' + unplanned}"


def accept_ended(run, events, phase, accept, now=None):
    """Accept the failure of the phase that has just ended: nothing may have begun since, and once only."""
    last = last_phase_event(events)
    if not last or (last["kind"], last["phase"]) != ("end", phase):
        was = f"{last['kind']} {last['phase']}" if last else "nothing"
        raise RecordError(f"{phase} is not the phase that just ended (the last phase event: {was}); a failure is "
                          "accepted right after its end, before anything else begins")
    if last["status"] != "fail":
        raise RecordError(f"{phase} did not fail ({last['status']}); there is nothing to accept")
    append(run, {"kind": "accept", "t": now_iso(now), "phase": phase, "accepted": accept})
    return "accepted-fail", f"{phase}: its failure is accepted ({last.get('why', '')})"


def cmd_end(rd, phase, accept="", now=None):
    run = need_open_run(rd)
    events = read_events(run)
    if accept:
        refuse_secret(accept, "reason")
    open_phase, _ = phase_state(events)
    if accept and open_phase is None:
        return accept_ended(run, events, phase, accept, now)
    check_open(events, phase)
    status, why = derive_status(gates_since_begin(events, phase))
    e = {"kind": "end", "t": now_iso(now), "phase": phase, "status": status}
    if why:
        e["why"] = why
    if accept:
        if status != "fail":
            raise RecordError(f"{phase} did not fail ({status}); there is nothing to accept")
        e["status"], e["accepted"] = "accepted-fail", accept
    append(run, e)
    return e["status"], f"{phase} ended: {e['status']}" + (f" ({why})" if why else "")


def unlogged_required(events, steps):
    logged = {e.get("step") for e in events if e["kind"] == "human"}
    return [s for s in required_steps(steps) if s not in logged]


def cmd_finish(rd, phases, steps, repo=REPO, now=None, incomplete=False):
    run = need_run(rd)
    events = read_events(run)
    if any(e["kind"] == "finish" for e in events):
        raise RecordError(f"run {run.name} is already finished")
    open_phase, _ = phase_state(events)
    if open_phase:
        raise RecordError(f"{open_phase} is still open: end it first (python3 kit/record.py end {open_phase})")
    unlogged = unlogged_required(events, steps)
    if unlogged and not incomplete:
        raise RecordError(f"required one-time steps never logged: {', '.join(unlogged)}. Log each one the person did "
                          "(python3 kit/record.py human <id> --note \"logged late\"), then finish; a run that "
                          "really misses one is sealed with: python3 kit/record.py finish --incomplete")
    sha, dirty = git_sha(repo)
    fin = {"kind": "finish", "t": now_iso(now), "sha": sha, "dirty": dirty}
    append(run, fin)
    s = summarize(events + [fin], phases, steps)
    write_atomic(run / "record.json", json.dumps(s, indent=2, sort_keys=True) + "\n")
    write_atomic(run / "record.md", render_md(s, steps))
    missing = s["human"]["required_not_seen"]
    flag = (f"\n  HUMAN STEPS INCOMPLETE: required one-time steps never logged: {', '.join(missing)}; N = "
            f"{s['human']['count']} is only a floor" if missing else "")
    return f"run {run.name} finished: {s['outcome']}{flag}\n  {run / 'record.md'}\n  {run / 'record.json'}"


def cmd_began(rd, phase, phases):
    """-> the time `phase` last began in the current run (finished or not), as written (now_iso: the RFC 3339 form
    `docker compose logs --since` names in its help, e.g. 2013-01-02T13:23:37Z). Refuses a phase that never began:
    a gate that read since an empty time would read the whole log."""
    begun = [e["t"] for e in read_events(need_run(rd)) if e["kind"] == "begin" and e["phase"] == phase]
    if not begun:
        raise RecordError(f"{phase} never began in this run" + ("" if phase in phases else
                                                                 f" (KIT.md has {', '.join(phases)})"))
    return begun[-1]


def cmd_status(rd, phases, steps):
    run = need_run(rd)
    events = read_events(run)
    open_phase, status = phase_state(events)
    lines = [f"run {run.name} at {events[0]['sha'][:12]}"]
    for p, title in phases.items():
        mark = "OPEN" if p == open_phase else status.get(p, "-")
        lines.append(f"  {p}. {title}: {mark}")
    lines.append(f"  human steps so far: {sum(1 for e in events if e['kind'] == 'human')}")
    unlogged = unlogged_required(events, steps)
    lines.append("  required one-time steps not logged yet: " + (", ".join(unlogged) if unlogged else "none"))
    return "\n".join(lines)


def main(argv=None, version=None):
    """version: the python3 version to judge against the floor (tests fake it); default the running one."""
    # The floor comes first, before the parser: add_subparsers(required=) is 3.7+, so on Ubuntu 18.04's 3.6 the
    # parser itself would die with a traceback before any later check could refuse.
    floor = python_floor_problem(sys.version_info if version is None else version)
    if floor:
        print(f"refusing: {floor}", file=sys.stderr)
        return 1
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("began").add_argument("phase")
    p = sub.add_parser("finish")
    p.add_argument("--incomplete", action="store_true",
                   help="seal the run although a required one-time step was never logged (N is then a floor)")
    for name in ("begin", "end"):
        p = sub.add_parser(name)
        p.add_argument("phase")
        if name == "end":
            p.add_argument("--accept", default="", help="record a failed phase as accepted, with the reason")
    p = sub.add_parser("gate")
    p.add_argument("phase")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("observe")
    p.add_argument("phase")
    p.add_argument("what")
    p = sub.add_parser("human")
    p.add_argument("step", nargs="?", default="")
    p.add_argument("--note", default="")
    p.add_argument("--unplanned", default="")
    p.add_argument("--retro", action="store_true", help="done before the run started; as the person reports it")
    a = ap.parse_args(argv)
    rd = runs_dir()
    try:
        if os.geteuid() == 0:
            raise RecordError("run as the kit user, not root (the record lives in the kit user's checkout)")
        phases = load_phases(KIT_MD.read_text())
        steps = load_steps(STEPS_MD.read_text())
        if a.cmd == "start":
            print(cmd_start(rd, repo=REPO))
        elif a.cmd == "begin":
            print(cmd_begin(rd, a.phase, phases))
        elif a.cmd == "gate":
            cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
            rc, msg = cmd_gate(rd, a.phase, cmd)
            print(msg)
            return 0 if rc == 0 else 1
        elif a.cmd == "observe":
            print(cmd_observe(rd, a.phase, a.what))
        elif a.cmd == "human":
            print(cmd_human(rd, a.step, steps, note=a.note, unplanned=a.unplanned, retro=a.retro))
        elif a.cmd == "end":
            status, msg = cmd_end(rd, a.phase, accept=a.accept)
            print(msg)
            return 0 if status in PASSING else 1
        elif a.cmd == "finish":
            print(cmd_finish(rd, phases, steps, repo=REPO, incomplete=a.incomplete))
        elif a.cmd == "status":
            print(cmd_status(rd, phases, steps))
        elif a.cmd == "began":
            print(cmd_began(rd, a.phase, phases))
    except (RecordError, OSError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
