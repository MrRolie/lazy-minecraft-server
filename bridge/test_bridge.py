"""bridge.py glue tests with a mocked Discord client. Need discord.py; without it EACH test shows as skipped
(they alone cover the glue: backlog posting, watchdog recovery, server_down, the /whitelist command, pump order).
Run: uv run --with pytest --with discord.py==2.7.1 pytest bridge/"""
import asyncio
import importlib
import inspect
import os
from types import SimpleNamespace

import pytest

from test_core import Clock, answer_when_written, append, ev, line, read_inbox  # noqa: F401

try:
    import discord
except ImportError:  # only a missing discord.py skips (one skip per test); a broken bridge.py must FAIL
    discord = bridge = None
else:
    import bridge
pytestmark = pytest.mark.skipif(discord is None, reason="glue test: needs --with discord.py==2.7.1")


@pytest.fixture
def mcve(tmp_path):
    return tmp_path

class FakeChannel:
    def __init__(self, name, cid, view=True, send=True):
        self.name, self.id, self.sent = name, cid, []
        self._perms = SimpleNamespace(view_channel=view, send_messages=send)

    def permissions_for(self, member):
        return self._perms

    async def send(self, text, allowed_mentions=None):
        self.sent.append((text, allowed_mentions))


def fake_guild(**overrides):
    chans = [FakeChannel(n, i, *overrides.get(n, (True, True))) for i, n in enumerate(bridge.CHANNELS, 1)
             if overrides.get(n) != "missing"]
    return SimpleNamespace(text_channels=chans, me=object())


def test_resolve_channels_ok_and_every_gap_named():
    assert set(bridge.resolve_channels(fake_guild())) == set(bridge.CHANNELS)
    with pytest.raises(bridge.Fatal) as e:
        bridge.resolve_channels(fake_guild(coords="missing", **{"ops-log": (False, False)}))
    assert "#coords is missing" in str(e.value) and "extra_bots" in str(e.value)
    with pytest.raises(bridge.Fatal, match="not in guild"):
        bridge.resolve_channels(None)


def test_intents_are_exactly_guilds_messages_and_content():
    i = bridge.intents()
    assert {n for n, v in i if v} == {"guilds", "guild_messages", "message_content"}


@pytest.fixture
def client(mcve):
    c = bridge.Bridge(guild_id=42, out_dir=str(mcve), in_dir=str(mcve), clock=Clock(),
                      ready_path=str(mcve / "ready"))
    c.channels = {n: FakeChannel(n, i) for i, n in enumerate(bridge.CHANNELS, 1)}
    c.presences = []

    async def change_presence(activity=None):
        c.presences.append(activity)
    c.change_presence = change_presence
    return c


def sent(c, name):
    return [t for t, _ in c.channels[name].sent]


def test_events_post_with_mentions_disabled_and_presence_deduped(client):
    async def go():
        await client.handle_event(ev("chat", player="a", text="@everyone hi"))
        await client.handle_event(ev("server", state="started"))
        await client.handle_event(ev("status", online=["a"], max=12))
        await client.handle_event(ev("status", online=["a"], max=12))
        await client.handle_event(ev("status", online=[], max=12))
    asyncio.run(go())
    assert sent(client, "game-chat") == ["**a**: @everyone hi", "🟢 Server is up"]
    for _, am in client.channels["game-chat"].sent:
        assert am.everyone is False and am.users is False and am.roles is False
    assert [(type(a).__name__, a.name) for a in client.presences] == [
        ("Game", "1/12 online"), ("CustomActivity", "Server empty")]


def test_a_start_without_a_stop_is_one_ops_log_line_even_from_a_backlog(client):
    now = client.clock.t = 1_790_000_000.0
    asyncio.run(client.handle_batch([
        {**ev("server", state="started"), "ts": ts(now - 7200)},   # the bridge's first: proves nothing
        {**ev("server", state="stopping"), "ts": ts(now - 7100)},
        {**ev("server", state="started"), "ts": ts(now - 7000)},   # clean restart
        {**ev("server", state="started"), "ts": ts(now - 3600)},   # no stopping since: crash, OOM exit or heal
    ]))
    ops = sent(client, "ops-log")
    assert len(ops) == 1 and ops[0].startswith("⚠️ minecraft restarted without a clean stop (crash or heal)")
    assert "at 2026-09-21 13:13:20 UTC" in ops[0]  # ts(now - 3600)
    assert sent(client, "game-chat") == []  # the backlog itself stays unposted
    asyncio.run(client.handle_event(ev("server", state="started")))  # fresh, again with no stop
    assert len(sent(client, "ops-log")) == 2 and sent(client, "game-chat") == ["🟢 Server is up"]


def test_watchdog_glue_posts_one_ops_line_and_recovers_silently(client, mcve):
    async def go():
        append(mcve, line(ev("status", online=["a"], max=12)))
        os.utime(mcve / "events.jsonl", (client.clock.t, client.clock.t))
        for e in client.tailer.read_new():
            await client.handle_event(e)
        client.clock.t += 181
        await client.tick_watchdog()
        await client.tick_watchdog()
        assert client.server_down()
        os.utime(mcve / "events.jsonl", (client.clock.t, client.clock.t))
        await client.tick_watchdog()
    asyncio.run(go())
    ops = sent(client, "ops-log")
    assert len(ops) == 1 and "server offline?" in ops[0]
    assert [a.name for a in client.presences] == ["1/12 online", "Server offline?", "1/12 online"]
    assert not client.server_down()


def msg(client, text="hi", author_id=7, bot=False, channel="game-chat", attachments=()):
    return SimpleNamespace(
        author=SimpleNamespace(bot=bot, id=author_id, display_name="Bob"), webhook_id=None, guild=object(),
        channel=client.channels[channel], type=discord.MessageType.default, clean_content=text,
        attachments=[SimpleNamespace(content_type=ct) for ct in attachments])


def test_game_chat_messages_become_rate_limited_says(client, mcve):
    async def go():
        await client.on_message(msg(client, "one"))
        await client.on_message(msg(client, "too fast"))             # same author inside 2 s
        await client.on_message(msg(client, "other", author_id=8))
        await client.on_message(msg(client, "bot", bot=True))
        await client.on_message(msg(client, "elsewhere", channel="coords"))
        client.say_limit.clock = lambda: 10 ** 9
        await client.on_message(msg(client, "", attachments=["image/png"]))
    asyncio.run(go())
    assert [(r["author"], r["text"]) for r in read_inbox(mcve)] == [("Bob", "one"), ("Bob", "other"),
                                                                    ("Bob", "[image]")]


def test_no_says_while_the_server_is_known_down(client, mcve):
    async def go():
        await client.handle_event(ev("server", state="stopping"))
        await client.on_message(msg(client, "anyone?"))
    asyncio.run(go())
    assert read_inbox(mcve) == []


def test_late_whitelist_results_are_one_summary_line_per_batch(client):
    asyncio.run(client.handle_batch([
        ev("whitelist_result", id=f"gone{i}", name=f"Steve{i}", ok=i != 2, detail="added" if i != 2 else
           "unknown account") for i in range(3)]))
    assert sent(client, "ops-log") == [
        "3 late whitelist result(s), no request waiting: `Steve0` ok (added), `Steve1` ok (added), "
        "`Steve2` refused (unknown account)"]


def ts(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def test_a_backlog_of_400_old_chat_lines_posts_nothing(client):
    now = client.clock.t = 1_790_000_000.0
    old = [{**ev("chat", player="a", text=f"line {i}"), "ts": ts(now - 400)} for i in range(400)]
    asyncio.run(client.handle_batch(old))
    assert sent(client, "game-chat") == []


def test_backlog_keeps_discoveries_and_coords_and_server_state_but_posts_fresh_chat(client):
    now = client.clock.t = 1_790_000_000.0
    place = dict(player="A", dim="minecraft:overworld", x=1, y=2, z=3, biome="minecraft:plains")
    asyncio.run(client.handle_batch([
        {**ev("game", text="Steve joined the game"), "ts": ts(now - 3600)},
        {**ev("server", state="stopping"), "ts": ts(now - 3600)},
        {**ev("discovery", text="a skull", **place), "ts": ts(now - 86400)},
        {**ev("coords", name="base", **place), "ts": ts(now - 86400)},
        {**ev("chat", player="a", text="fresh"), "ts": ts(now - 10)},
        {**ev("chat", player="a", text="no ts")} | {"ts": "garbage"},
    ]))
    assert sent(client, "game-chat") == ["**a**: fresh", "**a**: no ts"]
    assert len(sent(client, "discoveries")) == 1 and len(sent(client, "coords")) == 1
    assert client.server_state == "stopping"  # state still follows the stale event; it just isn't posted


def test_watchdog_is_not_fooled_by_a_missing_log_mid_rotation(client, mcve):
    async def go():
        append(mcve, line(ev("status", online=[], max=12)))
        os.utime(mcve / "events.jsonl", (client.clock.t, client.clock.t))
        await client.tick_watchdog()
        client.clock.t += 600  # the bridge has run 10 min; the log was written just now
        os.utime(mcve / "events.jsonl", (client.clock.t, client.clock.t))
        await client.tick_watchdog()
        os.rename(mcve / "events.jsonl", mcve / "events.jsonl.1")  # mid-rotation: no live file
        client.clock.t += 5
        await client.tick_watchdog()
        os.remove(mcve / "events.jsonl.1")  # neither file: the last mtime seen still stands
        await client.tick_watchdog()
    asyncio.run(go())
    assert sent(client, "ops-log") == [] and not client.watchdog.offline


def test_paths_keep_the_mods_files_apart_from_the_bridges():
    assert bridge.paths("/mcve") == ("/mcve/out", "/mcve/in")


class FakeInteraction:
    def __init__(self, guild_id=42):
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=7, name="bob", mention="<@7>")
        self.replies = []
        outer = self

        class Resp:
            async def defer(self, ephemeral=False, thinking=False):
                outer.replies.append(("defer", ephemeral))

            async def send_message(self, text, ephemeral=False):
                outer.replies.append((text, ephemeral))

        class Follow:
            async def send(self, text, ephemeral=False):
                outer.replies.append((text, ephemeral))
        self.response, self.followup = Resp(), Follow()


def test_whitelist_command_end_to_end_with_ops_log(client, mcve):
    async def go():
        it = FakeInteraction()
        await asyncio.gather(client.on_whitelist(it, "Steve"), answer(client, mcve))
        return it

    async def answer(c, d):
        while not read_inbox(d):
            await asyncio.sleep(0.005)
        req = read_inbox(d)[-1]
        await c.handle_event(ev("whitelist_result", id=req["id"], name=req["name"], ok=True, detail="already"))
    it = asyncio.run(go())
    assert it.replies == [("defer", True), ("✅ `Steve` is already on the list.", True)]
    assert sent(client, "ops-log") == ["/whitelist “Steve” by <@7>: already on the list"]
    assert read_inbox(mcve)[0]["requested_by"] == "bob (7)"


def test_whitelist_command_refuses_other_guilds_and_escapes_bad_input_in_ops_log(client, mcve):
    async def go():
        other = FakeInteraction(guild_id=99)
        await client.on_whitelist(other, "Steve")
        bad = FakeInteraction()
        await client.on_whitelist(bad, "*x*@everyone")
        return other, bad
    other, bad = asyncio.run(go())
    assert other.replies == [("This only works in the friend server.", True)]
    assert bad.replies[1][0].startswith("❌")
    assert sent(client, "ops-log") == [r"/whitelist “\*x\*@everyone” by <@7>: invalid name"]
    assert read_inbox(mcve) == []


@pytest.fixture
def fenced(tmp_path, monkeypatch):
    """main() with a fence stamp present and the hold recorded instead of slept."""
    stamp = tmp_path / "bridge-fence.ok"
    stamp.write_text("2026-10-01T00:00:00Z\n")
    monkeypatch.setattr(bridge, "FENCE_STAMP", str(stamp))
    held = []
    monkeypatch.setattr(bridge, "hold_then", lambda code, msg: held.append((code, msg)) or code)
    return stamp, held


def test_missing_token_is_a_held_fatal(fenced, monkeypatch):
    _, held = fenced
    monkeypatch.setenv("DISCORD_BRIDGE_TOKEN", "")
    assert bridge.main() == 2
    assert held and "DISCORD_BRIDGE_TOKEN is not set" in held[0][1]


def test_no_fence_stamp_is_a_held_fatal_before_anything_else(fenced, monkeypatch):
    """Every start path (mc-server, a restart policy after an unclean stop, a hand-run up) hits main()."""
    stamp, held = fenced
    stamp.unlink()
    monkeypatch.setenv("DISCORD_BRIDGE_TOKEN", "x")  # a token would otherwise log in
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    assert bridge.main() == 2
    assert held and "no egress fence" in held[0][1]


def test_the_fence_stamp_and_ready_paths_are_constants_no_env_can_move(tmp_path, monkeypatch):
    """An env var naming some other (existing) file must not satisfy the fence check or move the heartbeat."""
    assert not os.path.exists("/run/mc-host/bridge-fence.ok"), "this test needs a host without a real fence stamp"
    decoy = tmp_path / "decoy"
    decoy.write_text("x\n")
    for name in ("FENCE_STAMP", "READY", "MC_FENCE_STAMP", "BRIDGE_READY"):
        monkeypatch.setenv(name, str(decoy))
    try:
        importlib.reload(bridge)
        assert (bridge.FENCE_STAMP, bridge.READY) == ("/run/mc-host/bridge-fence.ok", "/tmp/ready")
        assert inspect.signature(bridge.Bridge).parameters["ready_path"].default == "/tmp/ready"
        held = []
        monkeypatch.setattr(bridge, "hold_then", lambda code, msg: held.append((code, msg)) or code)
        monkeypatch.setenv("DISCORD_BRIDGE_TOKEN", "x")
        monkeypatch.setenv("DISCORD_GUILD_ID", "1")
        assert bridge.main() == 2
        assert held and "no egress fence" in held[0][1]
    finally:
        monkeypatch.undo()
        importlib.reload(bridge)


def test_the_ready_heartbeat_is_touched_by_every_poll(client, mcve):
    ready = mcve / "ready"
    asyncio.run(client.pump_once())
    first = ready.stat().st_mtime_ns
    os.utime(ready, ns=(first - 10**9, first - 10**9))
    asyncio.run(client.pump_once())
    assert ready.stat().st_mtime_ns > first - 10**9


def test_pump_saves_the_offset_before_it_posts(client, mcve, monkeypatch):
    """A crash while posting must not re-post after a restart: the offset is durable first."""
    append(mcve, line(ev("chat", player="a", text="x")))

    async def crash(events):
        assert events and client.tailer._saved == (client.tailer.ino, client.tailer.off)  # saved already
        raise RuntimeError("crash mid-post")
    monkeypatch.setattr(client, "handle_batch", crash)
    with pytest.raises(RuntimeError):
        asyncio.run(client.pump_once())
    restarted = bridge.Bridge(guild_id=42, out_dir=str(mcve), in_dir=str(mcve), clock=Clock())
    assert restarted.tailer.read_new() == []  # nothing to re-post


def test_a_corrupt_offset_is_a_held_fatal_not_a_bare_exit(fenced, monkeypatch, tmp_path, caplog):
    _, held = fenced
    (tmp_path / "out").mkdir()
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "bridge.offset").write_text("")
    for k, v in {"DISCORD_BRIDGE_TOKEN": "x", "DISCORD_GUILD_ID": "1", "MCVE_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    assert bridge.main() == 2
    assert held and "bridge.offset is unreadable" in held[0][1]
