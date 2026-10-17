"""The bridge against the shared contract examples (mod/contract-examples/, mod/CONTRACT.md "Shared examples").

The mod's tests rebuild every events.jsonl line byte for byte and replay inbox.jsonl into inbox.expected.jsonl;
these tests hold the bridge to the same files, so a contract change on either side fails here. A missing
example file FAILS (never skips): without it nothing pins the contract. Pure: no discord.py needed.
Run: uv run --with pytest pytest bridge/
"""
import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

import core

EXAMPLES = Path(__file__).resolve().parent.parent / "mod" / "contract-examples"
TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
CONTRACT_TYPES = {"server", "chat", "game", "discovery", "coords", "status", "whitelist_result"}


def example(name):
    p = EXAMPLES / name
    assert p.is_file(), f"{p} is missing: the shared contract examples are required, not optional"
    return p


@pytest.fixture
def evs(tmp_path):
    """The example log read through the real tailer: the bridge starts with no log, then the log appears
    (read from 0). Nothing is written next to the examples."""
    src = example("events.jsonl")
    t = core.Tailer(str(tmp_path / "events.jsonl"), str(tmp_path / "bridge.offset"))
    shutil.copyfile(src, tmp_path / "events.jsonl")
    return t.read_new()


def test_every_example_event_is_read_parsed_and_routed(evs):
    path = example("events.jsonl")
    lines = [x for x in path.read_text("utf-8").splitlines() if x.strip()]
    assert len(evs) == len(lines), "the tailer dropped an example line"
    assert {e["type"] for e in evs} == CONTRACT_TYPES, "the examples no longer cover exactly the v1 event types"
    for e in evs:
        assert core.route(e) is not None, f"the bridge drops a contract event: {e}"
        # CONTRACT.md: ISO-8601 UTC with exactly three fractional digits and Z (a naive ts would be local time)
        assert TS.fullmatch(e["ts"]), f"ts not in the contract format: {e['ts']}"
        assert core.event_age(e, 0) is not None, f"unreadable ts: {e['ts']}"


# What the bridge does with each example line, in order. Checked against bridge/README.md.
EXPECTED = [
    ("server", "started"),
    ("presence", ("custom", "Server empty")),
    ("post", "game-chat", "*Steve joined the game*"),
    ("post", "game-chat", r'**Steve**: hello "world" \\ \<@123\> @everyone \*\*bold\*\* \`code\` 😀 é'),
    ("post", "game-chat", "*Steve was slain by Zombie*"),
    ("post", "game-chat", r"*Steve has made the advancement \[Stone Age\]*"),
    ("post", "discoveries", "**Steve** found: a cherry grove with a ruined portal\n"
                            "📍 Overworld 120, 72, -340 (Cherry Grove)"),
    ("post", "discoveries", "**Alex** found: fortress here, watch the blazes\n📍 Nether -45, 33, 210 (Crimson Forest)"),
    ("post", "coords", "📌 **test base** — Overworld -1024, 64, -272 (Windswept Savanna) · saved by Server"),
    ("post", "coords", "📌 **end city** — End 1000, 60, 0 (End Highlands) · saved by Steve"),
    ("presence", ("playing", "2/12 online")),
    ("whitelist_result", "w-1"),
    ("whitelist_result", 7),  # a numeric id is legal (a JSON scalar, echoed unchanged)
    ("whitelist_result", "w-3"),
    ("whitelist_result", "w-4"),
    ("whitelist_result", "w-7"),  # lookup failed: <error>
    ("whitelist_result", "w-8"),  # rate limited (the mod's own whitelist_add limit, 1.0.2)
    ("post", "game-chat", "*Steve left the game*"),
    ("presence", ("playing", "1/12 online")),
    ("server", "stopping"),
]


def test_each_example_event_formats_exactly(evs):
    got = [(r[0], r[1]["id"]) if r[0] == "whitelist_result" else r for r in map(core.route, evs)]
    assert got == EXPECTED


REPLY = {"added": "✅ `Notch` is on the whitelist", "already": "✅ `Notch` is already on the list",
         "invalid name": "❌ Couldn't add", "unknown account": "❌ No Minecraft account called",
         "lookup failed": "⏳ Couldn't reach Mojang", "rate limited": "⏳ The server is taking a lot of whitelist"}


def reply_key(detail):
    return "lookup failed" if detail.startswith("lookup failed: ") else detail


def test_example_whitelist_results_get_the_reply_their_detail_means(evs, tmp_path):
    results = [e for e in evs if e["type"] == "whitelist_result"]
    assert {reply_key(r["detail"]) for r in results} == set(REPLY)  # a new detail value fails here

    async def reply_to(res):
        w = core.Whitelister(core.Inbox(str(tmp_path / "inbox.jsonl")), timeout=1, new_id=lambda: "rid")
        task = asyncio.create_task(w.request(1, "Notch", "Mike"))
        while "rid" not in w.pending:
            await asyncio.sleep(0.001)
        assert w.resolve({**res, "id": "rid"})
        return (await task)[0]
    for r in results:
        assert asyncio.run(reply_to(r)).startswith(REPLY[reply_key(r["detail"])]), r


def _no_constants(name):
    raise ValueError(f"not strict JSON: {name}")


def strict_loads(raw):
    """RFC 8259 only, as the mod parses the inbox (1.0.4): Python's json already refuses comments, single quotes,
    unquoted keys or values, trailing commas and trailing data; NaN/Infinity are refused here too."""
    return json.loads(raw, parse_constant=_no_constants)


def inbox_lines():
    out = []
    for raw in example("inbox.jsonl").read_text("utf-8").split("\n"):
        try:
            rec = strict_loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def valid_id(i):
    """CONTRACT.md: a JSON string, or a JSON integer matching -?[0-9]{1,18} (not a bool, fraction or exponent)."""
    return isinstance(i, str) or (type(i) is int and len(str(abs(i))) <= 18)


def accepted(rec):
    """Which inbox lines the mod acts on, as the contract states it: (verb, id) or None."""
    if not valid_id(rec.get("id")):
        return None
    if rec.get("type") == "say" and isinstance(rec.get("author"), str) and isinstance(rec.get("text"), str):
        return ("say", rec["id"]) if core.one_line(rec["text"]) else None
    if rec.get("type") == "whitelist_add" and isinstance(rec.get("name"), str):
        return ("whitelist_add", rec["id"])
    return None


def expected_calls():
    return [json.loads(x) for x in example("inbox.expected.jsonl").read_text("utf-8").splitlines() if x.strip()]


def test_bridge_records_have_exactly_the_fields_the_mod_reads():
    calls = expected_calls()
    mod_fields = {v: set().union(*(set(c) - {"verb", "name_valid"} for c in calls if c["verb"] == v))
                  for v in ("say", "whitelist_add")}
    say = core.say_record("Mike", "hello")
    assert set(say) - {"type"} == mod_fields["say"]
    add = {"type": "whitelist_add", "id": "x", "name": "Notch", "requested_by": "Mike (1)"}  # Whitelister's shape
    assert set(add) - {"type"} == mod_fields["whitelist_add"]
    assert {c["verb"] for c in calls} == {"say", "whitelist_add"}


def test_whitelister_writes_the_shape_the_examples_pin(tmp_path):
    w = core.Whitelister(core.Inbox(str(tmp_path / "inbox.jsonl")), timeout=0.01, new_id=lambda: "w-1")
    asyncio.run(w.request(1, "Notch", "Mike (123)"))
    written = json.loads((tmp_path / "inbox.jsonl").read_text())
    canonical = next(r for r in inbox_lines() if r.get("id") == "w-1")
    assert written == canonical


def test_bridge_cleaning_equals_the_mods_on_every_example_say():
    """The bridge pre-cleans `say` exactly as the mod does, so what a friend typed is what players see,
    and a cleaning change on either side fails here."""
    raw = {r["id"]: r for r in inbox_lines() if r.get("type") == "say" and isinstance(r.get("id"), (str, int))}
    says = [c for c in expected_calls() if c["verb"] == "say"]
    assert len(says) >= 5
    for c in says:
        r = raw[c["id"]]
        rec = core.say_record(r["author"], r["text"])
        assert rec is not None and (rec["author"], rec["text"]) == (c["author"], c["text"]), c["id"]
    # a line the mod skips as empty after cleaning ("§§") is one the bridge never writes
    skipped = [r for i, r in raw.items() if i not in {c["id"] for c in says} and r.get("text") == "§§"]
    assert skipped and all(core.say_record(r["author"], r["text"]) is None for r in skipped)


def test_name_validation_agrees_with_the_mod():
    adds = [c for c in expected_calls() if c["verb"] == "whitelist_add"]
    assert adds
    for c in adds:
        assert core.valid_name(c["name"]) is c["name_valid"], c["name"]



def test_the_strict_json_and_id_rules_select_exactly_the_lines_the_mod_acts_on():
    """Every example line through strict parsing and the contract's id/shape rules gives exactly the mod's
    handler calls, in order: the lenient lines (l1-l6), bad ids (array, bool, null, 1.5, 1e3, 19 digits), the
    missing id, the unknown types and the blank say all drop out; -7 and the 18-digit id stay in."""
    raw = [ln for ln in example("inbox.jsonl").read_text("utf-8").split("\n")]
    lenient = [ln for ln in raw if any(f"{q}l{n}{q}" in ln or f"'l{n}'" in ln for n in range(1, 7) for q in '"')
               or ln.startswith("{type:") or "NaN" in ln]
    assert len(lenient) == 6
    for ln in lenient:
        with pytest.raises(ValueError):
            strict_loads(ln)
    got = [a for a in map(accepted, inbox_lines()) if a]
    want = [(c["verb"], c["id"]) for c in expected_calls()]
    assert got == want
    assert ("say", -7) in want and ("say", 123456789012345678) in want
    assert not any(isinstance(i, int) and len(str(abs(i))) > 18 for _, i in want)


def test_the_extended_cleaning_examples_are_present_and_pinned():
    """d-13 (all 25 White_Space), d-14 (C1 + DEL), d-15 (blank fillers, an all-filler author -> "?")."""
    calls = {c["id"]: c for c in expected_calls() if c["verb"] == "say"}
    assert {"d-13", "d-14", "d-15"} <= set(calls)
    raw = {r["id"]: r for r in inbox_lines() if r.get("type") == "say" and valid_id(r.get("id"))}
    assert {ord(c) for c in raw["d-13"]["text"]} >= {ord(c) for c in core.WHITESPACE}
    assert {ord(c) for c in raw["d-15"]["author"] + raw["d-15"]["text"]} >= {ord(c) for c in core.FILLERS}
    assert calls["d-15"]["author"] == "?"
    for i in ("d-13", "d-14", "d-15"):
        rec = core.say_record(raw[i]["author"], raw[i]["text"])
        assert (rec["author"], rec["text"]) == (calls[i]["author"], calls[i]["text"]), i


@pytest.mark.parametrize("author,text", [("Mike", "hello"), ("ünï", "a\u2028b"), ("x", "\ud800 lone surrogate"),
                                         ("\u3164", "nan inf NaN"), ("é" * 50, "😀" * 300)])
def test_every_line_the_bridge_writes_is_one_the_mod_accepts(tmp_path, author, text):
    inbox = core.Inbox(str(tmp_path / "inbox.jsonl"))
    inbox.append(core.say_record(author, text))
    inbox.append({"type": "whitelist_add", "id": "w", "name": "Notch", "requested_by": f"{author} (1)"})
    data = (tmp_path / "inbox.jsonl").read_bytes()
    data.decode("ascii")  # ASCII-escaped: no raw lone surrogate, nothing an encoder can choke on
    lines = data.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 3  # whole lines, each \n-terminated
    for ln in lines[:2]:
        assert accepted(strict_loads(ln.decode())) is not None


# ── unclean restarts (bridge logic only, no contract change): a `started` with no `stopping` since the last one ──

def example_server_events():
    """The example log's own `server` lines, by state: the shapes the mod writes."""
    evs = [json.loads(x) for x in example("events.jsonl").read_text("utf-8").splitlines() if x.strip()]
    by_state = {e["state"]: e for e in evs if e["type"] == "server"}
    assert set(by_state) == {"started", "stopping"}, "the examples no longer carry both server states"
    return by_state


@pytest.mark.parametrize("seq,flagged", [
    (["started"], []),                                  # the first start this bridge sees proves nothing
    (["started", "started"], [1]),                      # no `stopping` in between: an OOM exit, a kill, a heal
    (["started", "stopping", "started"], []),           # a clean restart
    (["stopping", "started", "started", "started"], [2, 3]),
    (["started", "stopping", "stopping", "started", "started"], [4]),
])
def test_an_unclean_restart_is_a_started_with_no_stopping_since_the_last_start(seq, flagged):
    shapes = example_server_events()
    prev, got = None, []
    for i, state in enumerate(seq):
        r = core.route(shapes[state])
        assert r == ("server", state)
        if core.unclean_restart(prev, r[1]):
            got.append(i)
        prev = r[1]
    assert got == flagged


def test_the_unclean_restart_line_names_the_start_and_escapes_it():
    text = core.unclean_restart_text(example_server_events()["started"])
    assert text.startswith("⚠️ minecraft restarted without a clean stop (crash or heal)")
    assert "at 2026-10-01 20:00:00 UTC with no" in text
    for bad in ({"ts": "*x* @everyone"}, {"ts": "2026-10-01T20:00:00"}, {}):  # garbage, naive, missing
        assert " at ? with no " in core.unclean_restart_text(bad)  # no file text reaches Discord
