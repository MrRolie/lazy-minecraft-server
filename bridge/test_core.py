"""core.py tests: tailing, formatting, inbox writing, the watchdog. Pure, no Discord needed.
Run: uv run --with pytest pytest bridge/"""
import asyncio
import errno
import json
import os
from types import SimpleNamespace

import pytest

import core


def ev(type_, **kw):
    return {"v": 1, "ts": "2026-10-01T12:00:00Z", "type": type_, **kw}


def line(e):
    return (json.dumps(e) + "\n").encode()


@pytest.fixture
def mcve(tmp_path):
    return tmp_path


def tailer(d):
    return core.Tailer(str(d / "events.jsonl"), str(d / "bridge.offset"))


def append(d, data, name="events.jsonl"):
    with open(d / name, "ab") as f:
        f.write(data)


# ── tailing ──────────────────────────────────────────────────────────────────

def test_first_start_skips_existing_history_and_reads_what_comes_after(mcve):
    append(mcve, line(ev("chat", player="a", text="old")))
    t = tailer(mcve)
    assert t.read_new() == []
    append(mcve, line(ev("chat", player="a", text="new")))
    assert [e["text"] for e in t.read_new()] == ["new"]


def test_a_log_that_appears_after_start_is_read_from_the_beginning(mcve):
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="first")))
    assert [e["text"] for e in t.read_new()] == ["first"]


def test_partial_line_waits_for_its_newline(mcve):
    t = tailer(mcve)
    whole = line(ev("chat", player="a", text="hello"))
    append(mcve, whole[:10])
    assert t.read_new() == []
    assert t.off == 0
    append(mcve, whole[10:])
    assert [e["text"] for e in t.read_new()] == ["hello"]


def test_malformed_lines_are_skipped_and_consumed(mcve):
    t = tailer(mcve)
    append(mcve, b"not json\n" + b'{"v":2,"type":"chat"}\n' + b"[1,2]\n" + b"\xff\xfe\n"
           + line(ev("chat", player="a", text="ok")))
    assert [e["text"] for e in t.read_new()] == ["ok"]
    assert t.read_new() == []
    assert t.off == os.path.getsize(mcve / "events.jsonl")


def test_offset_persists_across_a_restart_without_duplicates(mcve):
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="one")))
    assert len(t.read_new()) == 1
    t.save()
    append(mcve, line(ev("chat", player="a", text="two")))
    t2 = tailer(mcve)  # the restart
    assert [e["text"] for e in t2.read_new()] == ["two"]


def test_save_writes_only_when_the_position_moved(mcve):
    t = tailer(mcve)
    state = mcve / "bridge.offset"
    os.utime(state, (1, 1))
    t.read_new()
    t.save()
    assert state.stat().st_mtime == 1  # idle poll: no write
    append(mcve, line(ev("chat", player="a", text="x")))
    t.read_new()
    t.save()
    assert state.stat().st_mtime != 1


def test_unsaved_progress_is_re_read_after_restart(mcve):
    """The pump saves before it posts; this pins that save() is what makes progress durable."""
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="one")))
    t.read_new()
    assert [e["text"] for e in tailer(mcve).read_new()] == ["one"]


def test_rotation_drains_the_old_file_then_reads_the_new_one_from_zero(mcve):
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="before")))
    assert len(t.read_new()) == 1
    append(mcve, line(ev("chat", player="a", text="tail of old")))  # written, not yet read
    os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")
    append(mcve, line(ev("chat", player="a", text="new file")))
    assert [e["text"] for e in t.read_new()] == ["tail of old", "new file"]
    t.save()
    append(mcve, line(ev("chat", player="a", text="after")))
    assert [e["text"] for e in tailer(mcve).read_new()] == ["after"]


def test_rotation_drains_the_old_file_at_once_while_the_new_one_does_not_exist_yet(mcve):
    """Rotation is a rename: no live file until the mod's next event (up to 60 s). The tail of .1 (e.g. a
    whitelist_result) must not wait for it."""
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="before")))
    t.read_new()
    append(mcve, line(ev("whitelist_result", id="w", name="Steve", ok=True, detail="added")))
    os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")
    assert [e["type"] for e in t.read_new()] == ["whitelist_result"]  # at once, no live file yet
    assert t.read_new() == []
    append(mcve, line(ev("chat", player="a", text="new file")))
    assert [e["text"] for e in t.read_new()] == ["new file"]  # and nothing from .1 twice


def test_rotation_with_the_old_file_gone_starts_the_new_one_at_zero(mcve):
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="x" * 50)))
    t.read_new()
    os.remove(mcve / "events.jsonl")
    append(mcve, line(ev("chat", player="a", text="y")))  # new inode, smaller than the old offset
    assert [e["text"] for e in t.read_new()] == ["y"]


def test_truncation_in_place_restarts_at_zero(mcve):
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="x" * 50)))
    t.read_new()
    with open(mcve / "events.jsonl", "r+b") as f:
        f.truncate(0)
        f.write(line(ev("chat", player="a", text="y")))
    assert [e["text"] for e in t.read_new()] == ["y"]


def test_a_missing_log_mid_rotation_is_not_an_error(mcve):
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="x")))
    t.read_new()
    os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")
    assert t.read_new() == []


@pytest.mark.parametrize("content", ["garbage", "", '{"ino": 1}', '{"ino": "x", "off": 0}'])
def test_corrupt_or_empty_state_file_refuses_rather_than_guessing(mcve, content):
    (mcve / "bridge.offset").write_text(content)
    with pytest.raises(core.StateError, match="unreadable"):
        tailer(mcve)


def test_save_is_fsynced_before_the_rename(mcve, monkeypatch):
    t = tailer(mcve)
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(core.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(core.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    append(mcve, line(ev("chat", player="a", text="x")))
    t.read_new()
    t.save()
    assert calls[:2] == ["fsync", "replace"]  # the file's data, then the rename (then the directory)
    assert json.loads((mcve / "bridge.offset").read_text())["off"] == t.off


def test_newline_free_megabyte_is_skipped(mcve, monkeypatch):
    monkeypatch.setattr(core, "READ_CAP", 128)
    t = tailer(mcve)
    append(mcve, b"x" * 128 + line(ev("chat", player="a", text="ok")))
    assert [e["text"] for e in t.read_new()] == ["ok"]


def test_reads_past_one_chunk(mcve, monkeypatch):
    monkeypatch.setattr(core, "READ_CAP", 100)
    t = tailer(mcve)
    for i in range(20):
        append(mcve, line(ev("chat", player="a", text=str(i))))
    assert [e["text"] for e in t.read_new()] == [str(i) for i in range(20)]


# ── formatting ───────────────────────────────────────────────────────────────

def test_chat_is_bold_name_and_escaped_text():
    assert core.route(ev("chat", player="Steve_01", text="*hi* @everyone <@123> `x`")) == (
        "post", "game-chat", r"**Steve\_01**: \*hi\* @everyone \<@123\> \`x\`")


def test_text_never_starts_a_markdown_line():
    _, _, text = core.route(ev("chat", player="a", text="one\n# big\n> quote\r\n-# small"))
    assert "\n" not in text
    assert text == r"**a**: one \# big \> quote \-\# small"  # a whitespace run is one space, as in the mod


def test_urls_stay_clickable_and_masked_links_are_defused():
    _, _, text = core.route(ev("chat", player="a", text="see https://x.com/a_b and [free](http://evil)"))
    assert "https://x.com/a_b" in text
    assert r"\[free\]\(http://evil)" in text


def test_bidi_controls_are_stripped():
    rlo = "\u202e"
    _, _, text = core.route(ev("chat", player="a\u2066b\u2069", text=f"hi {rlo}gnp.exe\u202a\u202d\u2067\u2068"))
    assert text == "**ab**: hi gnp.exe"
    assert core.route(ev("game", text="x\u202by"))[2] == "*xy*"


def test_section_signs_and_control_chars_are_stripped():
    _, _, text = core.route(ev("chat", player="a", text="§cred\x07 text"))
    assert text == "**a**: red text"


def test_game_messages_are_italic_and_our_own_discord_echo_is_dropped():
    assert core.route(ev("game", text="Steve was slain by a Zombie")) == (
        "post", "game-chat", "*Steve was slain by a Zombie*")
    assert core.route(ev("game", text="[Discord] Bob: hi")) is None
    assert core.route(ev("game", text="   ")) is None


def test_server_state_events():
    assert core.route(ev("server", state="started")) == ("server", "started")
    assert core.route(ev("server", state="stopping")) == ("server", "stopping")
    assert core.SERVER_TEXT == {"started": "🟢 Server is up", "stopping": "🔴 Server is going down"}
    assert core.route(ev("server", state="exploded")) is None


def test_discovery_post():
    e = ev("discovery", player="Alex", dim="minecraft:the_nether", x=-120, y=64, z=33,
           biome="minecraft:soul_sand_valley", text="a *giant* skull")
    assert core.route(e) == ("post", "discoveries",
                             "**Alex** found: a \\*giant\\* skull\n📍 Nether -120, 64, 33 (Soul Sand Valley)")


def test_coords_post():
    e = ev("coords", player="Alex", dim="minecraft:overworld", x=1, y=70, z=-2,
           biome="terralith:cave/frostfire_caves", name="Main_base")
    assert core.route(e) == ("post", "coords",
                             "📌 **Main\\_base** — Overworld 1, 70, -2 (Frostfire Caves) · saved by Alex")


def test_placed_events_with_bad_fields_are_skipped():
    base = dict(player="A", dim="minecraft:overworld", x=1, y=2, z=3, biome="minecraft:plains", name="n")
    assert core.route(ev("coords", **base)) is not None
    for bad in ({"x": "1"}, {"y": True}, {"z": None}, {"dim": 7}, {"name": None}):
        assert core.route(ev("coords", **{**base, **bad})) is None


@pytest.mark.parametrize("dim,want", [
    ("minecraft:overworld", "Overworld"), ("minecraft:the_nether", "Nether"), ("minecraft:the_end", "End"),
    ("deeperdarker:otherside", "Otherside"), ("foo:the_aether", "Aether"), ("", "?")])
def test_pretty_dim(dim, want):
    assert core.pretty_dim(dim) == want


@pytest.mark.parametrize("biome,want", [
    ("minecraft:dark_forest", "Dark Forest"), ("terralith:cave/deep_caves", "Deep Caves"),
    ("plains", "Plains"), ("minecraft:", "?")])
def test_pretty_biome(biome, want):
    assert core.pretty_biome(biome) == want


def test_status_presence():
    assert core.route(ev("status", online=["a", "b"], max=12)) == ("presence", ("playing", "2/12 online"))
    assert core.route(ev("status", online=[], max=12)) == ("presence", ("custom", "Server empty"))
    assert core.route(ev("status", online="a", max=12)) is None


def test_whitelist_result_routing_and_unknown_types():
    r = ev("whitelist_result", id="u", name="Steve", ok=True, detail="added")
    assert core.route(r) == ("whitelist_result", r)
    assert core.route(ev("whitelist_result", id="u", name="Steve", ok="yes", detail="")) is None
    assert core.route(ev("explode")) is None


def test_clip():
    assert core.clip("x" * 2000) == "x" * 2000
    assert len(core.clip("x" * 3000)) == 2000


# ── Discord -> game ──────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_rate_limiter_sliding_window():
    c = Clock()
    rl = core.RateLimiter(1, 2, clock=c)
    assert rl.allow("a") and not rl.allow("a") and rl.allow("b")
    c.t += 1.9
    assert not rl.allow("a")
    c.t += 0.1
    assert rl.allow("a")


def test_say_record_trims_and_notes_attachments():
    rec = core.say_record("  Bob the  " + "x" * 40, "  hi\nthere  ", ["image/png", "application/zip"],
                          new_id=lambda: "id1")
    assert rec == {"type": "say", "id": "id1", "author": ("Bob the " + "x" * 40)[:32],  # the run collapsed
                   "text": "hi there [image] [file]"}
    assert len(core.say_record("a", "y" * 999)["text"]) == 256
    assert core.say_record("a", "   ") is None
    assert core.say_record("a", "", ["image/gif"])["text"] == "[image]"
    assert core.say_record("", "hi")["author"] == "?"


def test_inbox_appends_whole_lines(mcve):
    inbox = core.Inbox(str(mcve / "inbox.jsonl"))
    inbox.append({"type": "say", "id": "1", "author": "é", "text": "a"})
    inbox.append({"type": "say", "id": "2", "author": "b", "text": "b"})
    lines = (mcve / "inbox.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["1", "2"]
    assert json.loads(lines[0])["author"] == "é"


@pytest.mark.parametrize("name,ok", [("Notch\n", False), ("\nNotch", False), ("Steve", True), ("a_b", True), ("x" * 16, True), ("ab", False),
                                     ("x" * 17, False), ("bad-name", False), ("sp ace", False),
                                     ("ünï", False), ("", False)])
def test_valid_name(name, ok):
    assert core.valid_name(name) is ok


def read_inbox(d):
    p = d / "inbox.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def whitelister(d, timeout=1.0, clock=None):
    ids = iter(f"req{i}" for i in range(100))
    return core.Whitelister(core.Inbox(str(d / "inbox.jsonl")), timeout=timeout,
                            limiter=core.RateLimiter(3, 3600, clock=clock or Clock()), new_id=lambda: next(ids))


async def answer_when_written(d, w, **result):
    while not read_inbox(d):
        await asyncio.sleep(0.005)
    req = read_inbox(d)[-1]
    assert w.resolve({"type": "whitelist_result", "id": req["id"], "name": req["name"], **result})


@pytest.mark.parametrize("result,reply,outcome", [
    ({"ok": True, "detail": "added"}, "✅ `Steve` is on the whitelist. See you in game!", "added as Steve (added)"),
    ({"ok": True, "detail": "added", "name": "STEVE"}, "✅ `STEVE` is on the whitelist. See you in game!",
     "added as STEVE (added)"),  # the mod returns the canonical spelling
    ({"ok": True, "detail": "already"}, "✅ `Steve` is already on the list.", "already on the list"),
    ({"ok": False, "detail": "unknown account"}, "❌ No Minecraft account called `Steve`. Check the spelling; "
     "if it's right, Mojang may be down, so try again later.", "refused: unknown account"),
    ({"ok": False, "detail": "lookup failed: timeout"}, "⏳ Couldn't reach Mojang to check that name. "
     "Try again later.", "refused: lookup failed: timeout"),
    ({"ok": False, "detail": "invalid name"}, "❌ Couldn't add `Steve`.", "refused: invalid name"),
])
def test_whitelist_correlates_its_result_by_id(mcve, result, reply, outcome):
    async def go():
        w = whitelister(mcve)
        got, _ = await asyncio.gather(w.request(1, " Steve ", "bob (1)"), answer_when_written(mcve, w, **result))
        return w, got
    w, got = asyncio.run(go())
    assert got == (reply, outcome)
    assert read_inbox(mcve) == [{"type": "whitelist_add", "id": "req0", "name": "Steve", "requested_by": "bob (1)"}]
    assert w.pending == {}


def test_a_result_for_another_request_does_not_answer_this_one(mcve):
    async def go():
        w = whitelister(mcve, timeout=0.2)
        task = asyncio.create_task(w.request(1, "Steve", "bob"))
        while not read_inbox(mcve):
            await asyncio.sleep(0.005)
        assert not w.resolve({"id": "someone-else", "name": "Steve", "ok": True, "detail": "added"})
        return await task
    reply, outcome = asyncio.run(go())
    assert reply.startswith("⏳") and "no result in 0.2s" in outcome


def test_whitelist_timeout_says_server_not_running_and_late_result_is_unmatched(mcve):
    async def go():
        w = whitelister(mcve, timeout=0.05)
        got = await w.request(1, "Steve", "bob")
        return w, got
    w, (reply, outcome) = asyncio.run(go())
    assert reply == "⏳ The server isn't running right now. Try again later."
    assert "req0" in outcome
    assert w.resolve({"id": "req0", "name": "Steve", "ok": True, "detail": "added"}) is False


def test_whitelist_rejects_bad_names_and_known_down_server_without_writing(mcve):
    async def go():
        w = whitelister(mcve)
        return (await w.request(1, "bad-name!", "bob"), await w.request(1, "Steve", "bob", server_down=True))
    bad, down = asyncio.run(go())
    assert bad[0].startswith("❌") and bad[1] == "invalid name"
    assert down == ("⏳ The server isn't running right now. Try again later.", "server down, not sent")
    assert read_inbox(mcve) == []


def test_whitelist_rate_limit_is_three_per_user_per_hour(mcve):
    clock = Clock()

    async def go():
        w = whitelister(mcve, timeout=0.01, clock=clock)
        out = [await w.request(1, "Steve", "bob") for _ in range(4)]
        out.append(await w.request(2, "Alex", "amy"))  # another user is unaffected
        clock.t += 3600
        out.append(await w.request(1, "Steve", "bob"))
        return out
    out = asyncio.run(go())
    assert [o[1] for o in out][3] == "rate-limited"
    assert all(o[1] != "rate-limited" for i, o in enumerate(out) if i != 3)
    assert len(read_inbox(mcve)) == 5


# ── the offline watchdog ─────────────────────────────────────────────────────

def test_watchdog_fires_once_and_recovers():
    w = core.Watchdog(started=1000, stale_after=180)
    assert w.check(1100, mtime=1000) is None
    assert w.check(1181, mtime=1000) == "offline"
    assert w.check(1300, mtime=1000) is None  # once, not every tick
    assert w.check(1301, mtime=1300) == "recovered"
    assert w.check(1302, mtime=1300) is None


def test_watchdog_keeps_the_last_seen_mtime_when_the_log_is_briefly_missing():
    w = core.Watchdog(started=1000, stale_after=180)
    assert w.check(2000, mtime=1990) is None
    assert w.check(2001, mtime=None) is None  # mid-rotation: was 'offline' (fell back to the start time)
    assert w.check(2171, mtime=None) == "offline"  # but a log gone for good still counts as silence


def test_is_backlog_only_for_timely_types():
    now = 1_790_000_000
    old = "2026-09-21T00:00:00Z"
    for t in ("chat", "game", "server"):
        assert core.is_backlog({"type": t, "ts": old}, now)
    for t in ("discovery", "coords", "status", "whitelist_result"):
        assert not core.is_backlog({"type": t, "ts": old}, now)
    assert not core.is_backlog({"type": "chat", "ts": "nope"}, now)
    assert not core.is_backlog({"type": "chat"}, now)


def test_a_linked_state_file_is_refused_by_nofollow_not_by_its_content(mcve, tmp_path_factory):
    """The target is a VALID offset, so only O_NOFOLLOW (ELOOP) can refuse it."""
    target = tmp_path_factory.mktemp("world") / "level.dat"
    target.write_text('{"ino": null, "off": 0}')
    os.symlink(target, mcve / "bridge.offset")
    with pytest.raises(core.StateError) as e:
        tailer(mcve)
    assert isinstance(e.value.__cause__, OSError) and e.value.__cause__.errno == errno.ELOOP


def test_save_through_a_planted_tmp_link_never_writes_the_target(mcve, tmp_path_factory):
    target = tmp_path_factory.mktemp("world") / "level.dat"
    target.write_bytes(b"WORLD")
    t = tailer(mcve)
    os.symlink(target, mcve / "bridge.offset.tmp")
    append(mcve, line(ev("chat", player="a", text="x")))
    t.read_new()
    t.save()
    assert target.read_bytes() == b"WORLD"
    assert not (mcve / "bridge.offset").is_symlink()
    assert json.loads((mcve / "bridge.offset").read_text())["off"] == t.off


def two_rotations_while_down(mcve, keep):
    """State saved on file A; then A -> .1, B written, B -> .1 (A gone), as the mod does while the bridge is
    down. A is kept alive through a hard link so C can't reuse its inode (that case is the README's limit)."""
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="A1")))
    t.read_new()
    t.save()
    os.link(mcve / "events.jsonl", keep / "A")
    append(mcve, line(ev("chat", player="a", text="A2 never read")))
    os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")
    append(mcve, line(ev("chat", player="b", text="B1 " + "b" * 200)) + line(ev("chat", player="b", text="B2")))
    os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")


def test_after_two_rotations_the_new_1_is_not_read_at_the_old_offset(mcve, tmp_path_factory):
    two_rotations_while_down(mcve, tmp_path_factory.mktemp("keep"))
    append(mcve, line(ev("chat", player="c", text="C1")))
    assert [e["text"] for e in tailer(mcve).read_new()] == ["C1"]  # nothing from B at A's offset


def test_after_two_rotations_with_no_live_file_nothing_is_drained(mcve, tmp_path_factory):
    two_rotations_while_down(mcve, tmp_path_factory.mktemp("keep"))
    assert tailer(mcve).read_new() == []  # .1 is B, not the file being tailed


def seqs(evs):
    return [int(e["text"]) for e in evs]


@pytest.mark.parametrize("first_start", [False, True])
def test_a_rotation_landing_mid_poll_loses_and_duplicates_nothing(mcve, monkeypatch, first_start):
    """rv3's repro: the mod rotates after the bridge picked the live file, and its next append creates B.
    The bridge reads A through the fd it opened, so B's bytes are never paired with A's inode or offset."""
    t = tailer(mcve)
    if first_start:  # rv3's case: ino=None (the log appeared after the bridge started)
        assert t.ino is None
    for i in range(3):
        append(mcve, line(ev("chat", player="a", text=str(i))))
    real = core._complete

    def rotate_now(f, off, label):
        monkeypatch.setattr(core, "_complete", real)
        os.replace(mcve / "events.jsonl", mcve / "events.jsonl.1")  # EventLog's Files.move
        append(mcve, line(ev("chat", player="a", text="100")) + line(ev("chat", player="a", text="101")))
        return real(f, off, label)
    monkeypatch.setattr(core, "_complete", rotate_now)
    got = seqs(t.read_new())
    append(mcve, line(ev("chat", player="a", text="102")))
    got += seqs(t.read_new())
    assert got == [0, 1, 2, 100, 101, 102]


def test_symlinks_are_never_followed(mcve, tmp_path_factory):
    secret = tmp_path_factory.mktemp("world") / "level.dat"
    secret.write_bytes(line(ev("chat", player="x", text="from the world")))
    # events.jsonl planted as a link: ignored, not read
    os.symlink(secret, mcve / "events.jsonl")
    t = tailer(mcve)
    assert t.read_new() == [] and t.ino is None
    # inbox.jsonl as a link: refused, the target untouched
    os.symlink(secret, mcve / "inbox.jsonl")
    before = secret.read_bytes()
    with pytest.raises(OSError):
        core.Inbox(str(mcve / "inbox.jsonl")).append({"type": "say", "id": "1", "author": "a", "text": "b"})
    assert secret.read_bytes() == before


def test_a_linked_rotated_file_is_not_drained(mcve):
    """.1 as a symlink to the very file we were reading: following it would pass the inode check."""
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="one")))
    t.read_new()
    append(mcve, line(ev("chat", player="x", text="via the link")))
    os.rename(mcve / "events.jsonl", mcve / "moved")
    os.symlink(mcve / "moved", mcve / "events.jsonl.1")
    append(mcve, line(ev("chat", player="a", text="new")))
    assert [e["text"] for e in t.read_new()] == ["new"]


def test_watchdog_counts_from_bridge_start_for_an_old_or_missing_log():
    w = core.Watchdog(started=5000, stale_after=180)
    assert w.check(5100, mtime=10) is None  # an old file is not offline the moment we start
    assert w.check(5181, mtime=None) == "offline"


def test_save_refuses_a_link_planted_between_unlink_and_open(mcve, tmp_path_factory, monkeypatch):
    """O_EXCL|O_NOFOLLOW, not the unlink alone, is what keeps save() from writing through a link."""
    target = tmp_path_factory.mktemp("world") / "level.dat"
    target.write_bytes(b"WORLD")
    t = tailer(mcve)
    real_unlink = os.unlink

    def unlink_then_plant(p):
        try:
            real_unlink(p)
        except FileNotFoundError:
            pass
        os.symlink(target, p)
    monkeypatch.setattr(core.os, "unlink", unlink_then_plant)
    append(mcve, line(ev("chat", player="a", text="x")))
    t.read_new()
    with pytest.raises(OSError):
        t.save()
    assert target.read_bytes() == b"WORLD"


def test_a_failed_read_of_the_new_file_after_rotation_keeps_the_old_position(mcve, monkeypatch):
    """The state must not move to the new file before its read succeeded, or .1's tail is lost."""
    t = tailer(mcve)
    append(mcve, line(ev("chat", player="a", text="A1")))
    t.read_new()
    append(mcve, line(ev("chat", player="a", text="A2 tail")))
    os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")
    append(mcve, line(ev("chat", player="c", text="C1")))
    real, live = core._complete, str(mcve / "events.jsonl")

    def vanish_once(f, off, label):
        if label == live:  # a read error on the new file, after .1 was drained
            monkeypatch.setattr(core, "_complete", real)
            raise OSError(5, "I/O error", label)
        return real(f, off, label)
    monkeypatch.setattr(core, "_complete", vanish_once)
    assert t.read_new() == []
    assert [e["text"] for e in t.read_new()] == ["A2 tail", "C1"]


def test_a_naive_ts_is_not_aged():
    assert core.event_age({"ts": "2026-10-01T20:00:00"}, 0) is None
    assert core.event_age({"ts": "2026-10-01T20:00:00.000Z"}, 0) is not None


@pytest.mark.parametrize("raw,want", [
    ("a \x07 b", "a b"),                       # a dropped control does not end a whitespace run
    ("　\xa0 x\t\n\x85y  ", "x y"),   # the Unicode White_Space set, not Python's isspace/strip
    ("§\U0001F600ok", "ok"),                  # § takes the next code point, astral too
    ("a‎؜b", "ab"),                  # bidi marks
    ("x" + " " * 200 + "y", "x y"),
    ("a\x1cb\x1f", "ab"),                     # U+001C-001F: Python calls them space, White_Space doesn't
    ("a\x80\x9fb", "ab"),                     # C1 (other than U+0085, which is whitespace)
])
def test_cleaning_follows_the_mods_text_clean(raw, want):
    assert core.one_line(raw) == want


@pytest.mark.parametrize("first_start", [False, True])
def test_a_rotation_landing_just_before_the_open_loses_and_duplicates_nothing(mcve, monkeypatch, first_start):
    """The other side of the window: the mod rotates and appends B right before the bridge opens the live path.
    The old lstat-then-reopen Tailer took A's inode with B's bytes here (rv3: lost [0, 1, 2], dups 2)."""
    t = tailer(mcve)
    if not first_start:
        append(mcve, line(ev("chat", player="a", text="0")))
        assert seqs(t.read_new()) == [0]
    for i in (1, 2):
        append(mcve, line(ev("chat", player="a", text=str(i))))
    real, live, fired = core.open_regular, str(mcve / "events.jsonl"), []

    def rotate_first(path, flags, mode=0o644):
        if path == live and not fired:
            fired.append(1)
            os.replace(live, live + ".1")
            append(mcve, line(ev("chat", player="a", text="100")) + line(ev("chat", player="a", text="101")))
        return real(path, flags, mode)
    monkeypatch.setattr(core, "open_regular", rotate_first)
    got = seqs(t.read_new())
    append(mcve, line(ev("chat", player="a", text="102")))
    got += seqs(t.read_new())
    want = [1, 2, 100, 101, 102] if not first_start else [100, 101, 102]  # first start: A was never seen
    assert got == want


def test_a_directory_at_bridge_offset_is_refused_by_the_regular_file_check(mcve):
    (mcve / "bridge.offset").mkdir()  # opens fine O_RDONLY: only the fstat S_ISREG check refuses it
    with pytest.raises(core.StateError) as e:
        tailer(mcve)
    assert e.value.__cause__.errno == errno.EINVAL


@pytest.mark.parametrize("bad", [True, False, None, 1.5, [1], {"a": 1}])
def test_a_whitelist_result_id_must_be_a_string_or_a_non_bool_int(bad):
    assert core.route(ev("whitelist_result", id=bad, name="Steve", ok=True, detail="added")) is None
    assert core.route(ev("whitelist_result", id=7, name="Steve", ok=True, detail="added")) is not None


@pytest.mark.parametrize("cp", sorted(core.WHITESPACE))
def test_every_white_space_code_point_collapses_like_the_mod(cp):
    assert core.one_line(f"{cp}a{cp}{cp}b{cp}") == "a b"


@pytest.mark.parametrize("cp", sorted(core.BIDI))
def test_every_bidi_control_is_removed_like_the_mod(cp):
    assert core.one_line(f"a{cp}b {cp} c") == "ab c"


def test_the_white_space_and_bidi_sets_are_exactly_the_mods():
    """Text.isWhitespace / isBidiControl at ve-commands 1.0.3, as code-point ranges."""
    ws = {*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F,
          0x3000}
    bidi = {*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C}
    assert {ord(c) for c in core.WHITESPACE} == ws and len(ws) == 25
    assert {ord(c) for c in core.BIDI} == bidi and len(bidi) == 12


@pytest.mark.parametrize("cp", sorted(core.FILLERS))
def test_every_blank_filler_is_removed_like_the_mod(cp):
    assert core.one_line(f"a{cp}b {cp} c") == "ab c"
    assert core.say_record(cp * 3, "hi")["author"] == "?"  # an all-filler author is no author


def test_the_blank_filler_set_is_exactly_the_mods():
    """Text.isBlankFiller at ve-commands 1.0.4."""
    assert {ord(c) for c in core.FILLERS} == {0x2800, 0x3164, 0x115F, 0x1160, 0xFFA0, 0x200B, 0xFEFF}


def test_the_inbox_writer_is_strict_json_and_survives_a_lone_surrogate(mcve):
    inbox = core.Inbox(str(mcve / "inbox.jsonl"))
    inbox.append({"type": "say", "id": "1", "author": "a", "text": "\ud800 x"})
    raw = (mcve / "inbox.jsonl").read_bytes()
    assert raw == b'{"type":"say","id":"1","author":"a","text":"\\ud800 x"}\n'
    with pytest.raises(ValueError):  # NaN would be lenient JSON, which the mod skips
        inbox.append({"type": "say", "id": "2", "author": "a", "text": "t", "x": float("nan")})
