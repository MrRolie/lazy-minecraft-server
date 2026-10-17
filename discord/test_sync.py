"""Pure-planner tests for discord/sync.py. Run: uv run --with pytest pytest discord/"""
import copy
import datetime
import json
import re
import subprocess
from pathlib import Path

import pytest

import sync

GID, BOT = "100", "200"
# The tests' kit.toml is kit/fixtures/example.toml, a made-up kit instance (profile "kit"). The friends' own server,
# asus-node, has its record in kit/fixtures/asus-node.toml and its guild's text in discord/content/ and discord/posts/:
# the tests about that server read them and skip where a copy dropped them (kit/INSTANCE-PATHS). Whether this tree
# holds the record is kit/instance.py's one predicate: part of a record raises here, so a damaged live tree fails the
# run instead of skipping its pins. The P6 stop also runs against a stand-in instance built in tmp_path, so a copy
# without the record still tests it.
EXAMPLE = sync.REPO / "kit" / "fixtures" / "example.toml"
FIXTURE = sync.ASUS_NODE_FIXTURE
sync.KIT_TOML = EXAMPLE
CFG = sync.load_kit_config()  # and kit/ is on sys.path
import instance as kit_instance  # noqa: E402  (the `instance` fixture below is an Instance)

NO_RECORD = kit_instance.NO_RECORD
RECORD = kit_instance.record()
needs_record = pytest.mark.skipif(not RECORD, reason=NO_RECORD)
KIT = CFG["discord"]
BOTS = sync.kit_bots(KIT)
GOLDEN = sync.REPO / "kit" / "fixtures" / "asus-node" / "discord"  # welcome.md and setup.md as posted before the kit
LAYOUT = sync.load_layout((Path(__file__).parent / "layout.toml").read_text(), BOTS)  # as written (asus-node's)
KIT_LAYOUT = sync.load_layout((Path(__file__).parent / "layout.toml").read_text(), BOTS, "kit")
# the #setup texts: the kit's, and asus-node's as posted (an instance path)
SETUPS = [pytest.param("kit/setup.md", id="kit"), pytest.param("setup.md", id="asus-node", marks=needs_record)]


def asus_cfg():
    """asus-node's own kit.toml (its record), or a skip where a copy dropped it."""
    if not RECORD:
        pytest.skip(NO_RECORD)
    import kitconfig  # on sys.path: sync.load_kit_config put kit/ there
    return kitconfig.load(FIXTURE)
# Built from two literals so this file never itself matches a share link (test_no_tracked_file_...).
INVITE = "https://login.tailscale.com/admin/invite/"
FAKE_LINK = INVITE + "FakeTestCode0123"
TODAY = datetime.date(2026, 10, 2)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No test reads the real discord/.env (live tokens), and no resolved secret leaks across tests."""
    monkeypatch.setattr(sync, "DOTENV", tmp_path / "absent.env")
    monkeypatch.setattr(sync, "REDACT", {})
    monkeypatch.setattr(sync, "KIT_TOML", EXAMPLE)  # never the real kit.toml
    monkeypatch.delenv("MC_KIT_TOML", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)


def converged_state(layout):
    """Synthesize the guild exactly as the layout wants it."""
    chans, nid = [], 1000
    for ci, cat in enumerate(layout["categories"]):
        nid += 1
        cid = str(nid)
        chans.append({"id": cid, "name": cat["name"], "type": sync.CATEGORY, "position": ci,
                      "permission_overwrites": sync.overwrites(GID, BOT, private=cat["private"],
                                                                 extra_bots=cat["extra_bots"])})
        for pi, ch in enumerate(cat["channels"]):
            nid += 1
            typ = sync._type(ch)
            chans.append({"id": str(nid), "name": ch["name"], "type": typ, "position": pi, "parent_id": cid,
                          "topic": ch["topic"] if typ == sync.TEXT else None,
                          "permission_overwrites": sync.overwrites(
                              GID, BOT, private=cat["private"], read_only=ch["read_only"],
                              extra_bots=cat["extra_bots"])})
    return chans


def test_empty_guild_creates_every_category_and_channel_in_order():
    actions, notes = sync.plan(LAYOUT, [], GID, BOT)
    assert all(a["op"] == "create" for a in actions)
    n_channels = sum(len(c["channels"]) for c in LAYOUT["categories"])
    assert len(actions) == len(LAYOUT["categories"]) + n_channels
    # each category is created before its children (executor resolves parent by name)
    seen = set()
    for a in actions:
        if a["type"] == sync.CATEGORY:
            seen.add(a["name"])
        else:
            assert a["parent"] in seen
    assert notes == []


def test_converged_guild_plans_zero_actions():
    state = converged_state(LAYOUT)
    sys_id = next(c["id"] for c in state if c["name"] == "general")
    actions, notes = sync.plan(LAYOUT, state, GID, BOT, system_channel_id=sys_id)
    assert actions == [] and notes == []


def test_unlisted_channel_is_reported_never_deleted():
    state = converged_state(LAYOUT) + [{"id": "9", "name": "old-stuff", "type": sync.TEXT, "position": 0}]
    sys_id = next(c["id"] for c in state if c["name"] == "general")
    actions, notes = sync.plan(LAYOUT, state, GID, BOT, system_channel_id=sys_id)
    assert actions == []
    assert any("old-stuff" in n for n in notes)


def test_drifted_topic_and_order_are_patched():
    state = converged_state(LAYOUT)
    sys_id = next(c["id"] for c in state if c["name"] == "general")
    gen = next(c for c in state if c["name"] == "general")
    gen["topic"] = "hand edited"
    gen["position"] = 99  # moved to the bottom of THE WORLD
    ops = [a["op"] for a in sync.plan(LAYOUT, state, GID, BOT, system_channel_id=sys_id)[0]]
    assert ops == ["patch", "reorder"]


def test_system_channel_set_when_missing():
    actions, _ = sync.plan(LAYOUT, converged_state(LAYOUT), GID, BOT, system_channel_id=None)
    assert [a["op"] for a in actions] == ["guild"]


def test_overwrites_read_only_and_private():
    assert sync.overwrites(GID, BOT) == []
    ro = {o["id"]: o for o in sync.overwrites(GID, BOT, read_only=True)}
    assert int(ro[GID]["deny"]) & sync.SEND and not int(ro[GID]["deny"]) & sync.VIEW
    assert int(ro[BOT]["allow"]) & sync.SEND
    priv = {o["id"]: o for o in sync.overwrites(GID, BOT, private=True)}
    assert int(priv[GID]["deny"]) & sync.VIEW


def test_content_noop_edit_and_repost():
    msgs = [{"id": "1", "content": "a", "attachments": []}, {"id": "2", "content": "b", "attachments": []}]
    assert sync.plan_content("x", "c", [("a", None), ("b", None)], msgs) == []
    edits = sync.plan_content("x", "c", [("a", None), ("B", None)], msgs)
    assert [(e["op"], e["id"]) for e in edits] == [("edit_msg", "2")]
    repost = [e["op"] for e in sync.plan_content("x", "c", [("a", None), ("b", None), ("c", None)], msgs)]
    assert repost == ["delete_msg", "delete_msg", "post_msg", "post_msg", "post_msg"]


PACK = {"path": Path("dist/p.mrpack"), "filename": "p.mrpack", "size": 11056}


def _msg(content, *atts):
    return {"id": "1", "content": content, "attachments": [{"id": "9", "filename": f, "size": n} for f, n in atts]}


def test_attachment_current_is_noop():
    assert sync.plan_content("setup", "c", [("t", PACK)], [_msg("t", ("p.mrpack", 11056))]) == []


@pytest.mark.parametrize("msg", [
    _msg("t", ("p.mrpack", 999)),                         # size differs (rebuilt pack)
    _msg("t", ("old.mrpack", 11056)),                     # filename differs (chapter bump)
    _msg("t"),                                            # no attachment yet
    _msg("t", ("p.mrpack", 11056), ("x.png", 1)),         # extra attachment
    _msg("changed", ("p.mrpack", 11056)),                 # text alone differs
])
def test_attachment_mismatch_edits_with_file(msg):
    (a,) = sync.plan_content("setup", "c", [("t", PACK)], [msg])
    assert a["op"] == "edit_msg" and a["file"] is PACK and a["content"] == "t"


def test_unwanted_attachment_is_dropped():
    (a,) = sync.plan_content("setup", "c", [("t", None)], [_msg("t", ("p.mrpack", 11056))])
    assert a["op"] == "edit_msg" and a["file"] is None
    assert sync.message_body(a, {})["attachments"] == []


def test_repost_uploads_the_attachment():
    acts = sync.plan_content("setup", "c", [("a", None), ("t", PACK)], [_msg("a")])
    posts = [a for a in acts if a["op"] == "post_msg"]
    assert [a["op"] for a in acts] == ["delete_msg", "post_msg", "post_msg"]
    assert [p["file"] for p in posts] == [None, PACK]
    body = sync.message_body(posts[1], {"flags": sync.SUPPRESS_NOTIFICATIONS})
    assert body["attachments"] == [{"id": 0, "filename": "p.mrpack"}] and body["flags"]
    assert "attachments" not in sync.message_body(posts[0], {})  # file-less POST body unchanged


def test_take_attach_removes_the_directive_line():
    assert sync.take_attach("**2. Pack**\nget it\n{attach:pack}\nafter") == ("**2. Pack**\nget it\nafter", "pack")
    assert sync.take_attach("plain {attach:pack} inline is not a directive") == (
        "plain {attach:pack} inline is not a directive", None)
    assert sync.take_attach("no file") == ("no file", None)
    with pytest.raises(ValueError):
        sync.take_attach("{attach:pack}\n{attach:pack}")


def test_the_attached_pack_is_the_one_compose_runs(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  minecraft:\n    environment:\n      MODRINTH_MODPACK: "/pack/ve-2.0.0-ch3.mrpack"\n')
    assert sync.ATTACHMENTS["pack"](tmp_path) == tmp_path / "dist" / "ve-2.0.0-ch3.mrpack"
    # a custom pack: kit/render.py's override names it, and compose (so sync) reads that first
    (tmp_path / "docker-compose.override.yml").write_text('      MODRINTH_MODPACK: "/pack/my-pack-1.0.0-ch1.mrpack"\n')
    assert sync.ATTACHMENTS["pack"](tmp_path) == tmp_path / "dist" / "my-pack-1.0.0-ch1.mrpack"
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-compose.override.yml").unlink()
    with pytest.raises(ValueError):
        sync.ATTACHMENTS["pack"](tmp_path)
    # the shipped compose names the file the server runs
    assert sync.ATTACHMENTS["pack"](Path(__file__).parent.parent).name.endswith(".mrpack")


def test_resolve_attachment_fail_closed(tmp_path):
    (tmp_path / "docker-compose.yml").write_text('      MODRINTH_MODPACK: "/pack/ve-1.mrpack"\n')
    with pytest.raises(ValueError, match="run python3 pack/build.py first"):
        sync.resolve_attachment("pack", tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "ve-1.mrpack").write_bytes(b"PK" * 10)
    assert sync.resolve_attachment("pack", tmp_path) == {
        "path": tmp_path / "dist" / "ve-1.mrpack", "filename": "ve-1.mrpack", "size": 20}
    with pytest.raises(ValueError, match="unknown"):
        sync.resolve_attachment("nope", tmp_path)


def test_multipart_body():
    payload = {"content": "hi", "attachments": [{"id": 0, "filename": "p.mrpack"}]}
    body, ctype = sync.multipart(payload, PACK, b"\x00PKDATA\xff", "BOUND")
    assert ctype == "multipart/form-data; boundary=BOUND"
    assert body.startswith(b"--BOUND\r\n") and body.endswith(b"\r\n--BOUND--\r\n")
    json_part, file_part = body[len(b"--BOUND\r\n"):-len(b"\r\n--BOUND--\r\n")].split(b"\r\n--BOUND\r\n")
    head, js = json_part.split(b"\r\n\r\n", 1)
    assert head == b'Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json'
    assert json.loads(js) == payload
    head, data = file_part.split(b"\r\n\r\n", 1)
    assert head == (b'Content-Disposition: form-data; name="files[0]"; filename="p.mrpack"\r\n'
                    b"Content-Type: application/octet-stream")
    assert data == b"\x00PKDATA\xff"


@pytest.mark.parametrize("name", SETUPS)
def test_shipped_setup_attaches_the_pack(name):
    text = (Path(__file__).parent / "content" / name).read_text()
    atts = [sync.take_attach(p)[1] for p in sync.split_parts(text)]
    assert atts.count("pack") == 1
    assert not any("{attach:" in sync.take_attach(p)[0] for p in sync.split_parts(text))


def test_split_parts_and_links():
    assert sync.split_parts("one\n---\ntwo\n") == ["one", "two"]
    with pytest.raises(ValueError):
        sync.split_parts("x" * (sync.MAX_LEN + 1))
    assert sync.resolve_links("see {#setup}", {"setup": "42"}) == "see <#42>"
    with pytest.raises(ValueError):
        sync.resolve_links("{#nope}", {})


@pytest.mark.parametrize("profile", [pytest.param("asus-node", marks=needs_record), "kit"])
@pytest.mark.parametrize("opened", [True, False])
def test_shipped_content_fits_and_links_resolve(profile, opened):
    layout = sync.load_layout((Path(__file__).parent / "layout.toml").read_text(), BOTS, profile)
    names = {ch["name"]: "1" for c in layout["categories"] for ch in c["channels"]}
    # the longest names the schema allows, so a kit instance's longer address still fits each part
    cfg = cfg_with(**{"tailscale.host_name": "h" * 63, "tailscale.server_address": "h" * 63 + "." + "t" * 63
                      + ".ts.net", "server.open_date": None if not opened else datetime.date(2026, 12, 31)})
    for cat in layout["categories"]:
        for ch in cat["channels"]:
            if ch["content"]:
                text = (Path(__file__).parent / "content" / ch["content"]).read_text()
                # filled with a longer-than-real link, as sync posts it
                filled = sync.fill_config(text, sync.resolve_config(sync.CONFIG_REF.findall(text), cfg))
                filled = sync.fill_secrets(filled, {"share_link": FAKE_LINK + "X" * 40})
                assert "{secret:" not in filled and "{config:" not in filled
                parts = sync.split_parts(sync.resolve_links(filled, names))
                assert parts


def _http_error(code, body=b"{}"):
    import io
    import urllib.error
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))


def test_get_retries_upstream_5xx_but_writes_do_not(monkeypatch):
    import urllib.request
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_open(req, timeout=0):
        calls.append(req.get_method())
        if len(calls) == 1:
            raise _http_error(503)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert sync.Api("t")("GET", "/x") == {"ok": True} and calls == ["GET", "GET"]

    calls.clear()

    def always_503(req, timeout=0):
        calls.append(req.get_method())
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", always_503)
    with pytest.raises(SystemExit):
        sync.Api("t")("POST", "/x", {"a": 1})
    assert calls == ["POST"]  # a write that 5xx'd is never blindly repeated


def test_renamed_from_renames_in_place_and_converges():
    layout = sync.load_layout("""
[[category]]
name = "VOICE"
  [[category.channel]]
  name = "Campfire"
  type = "voice"
  renamed_from = ["Overworld"]
""")
    current = [{"id": "1", "name": "VOICE", "type": sync.CATEGORY, "position": 0, "permission_overwrites": []},
               {"id": "2", "name": "Overworld", "type": sync.VOICE, "position": 0, "parent_id": "1",
                "permission_overwrites": []}]
    actions, notes = sync.plan(layout, current, GID, BOT)
    assert [(a["op"], a["id"], a["fields"]) for a in actions] == [("patch", "2", {"name": "Campfire"})]
    assert notes == []
    current[1]["name"] = "Campfire"  # after apply
    assert sync.plan(layout, current, GID, BOT) == ([], [])


def test_renamed_from_must_be_a_list():
    with pytest.raises(ValueError):
        sync.load_layout('[[category]]\nname = "V"\n  [[category.channel]]\n  name = "a"\n  renamed_from = "b"\n')


def test_extra_bots_get_the_bot_allow_only_in_locked_channels_of_their_category():
    layout = sync.load_layout('''
[[category]]
name = "OPEN"
extra_bots = ["300"]
  [[category.channel]]
  name = "chat"
[[category]]
name = "ADMIN"
private = true
extra_bots = ["300"]
  [[category.channel]]
  name = "ops-log"
''')
    actions, _ = sync.plan(layout, [], GID, BOT)
    by_name = {a["name"]: a["overwrites"] for a in actions}
    assert by_name["chat"] == []  # an unlocked channel needs no overwrite for anyone
    for name in ("ADMIN", "ops-log"):
        members = {o["id"]: int(o["allow"]) for o in by_name[name] if o["type"] == 1}
        assert members == {BOT: sync.BOT_ALLOW, "300": sync.BOT_ALLOW}


def test_extra_bots_must_be_id_strings():
    with pytest.raises(ValueError, match="extra_bots"):
        sync.load_layout('[[category]]\nname = "A"\nextra_bots = [300]\n')


def test_adding_an_extra_bot_to_a_converged_guild_patches_only_its_category():
    state = converged_state(LAYOUT)
    sys_id = next(c["id"] for c in state if c["name"] == "general")
    layout = sync.load_layout((Path(__file__).parent / "layout.toml").read_text(), BOTS)
    admin = next(c for c in layout["categories"] if c["name"] == "ADMIN")
    admin["extra_bots"] = ["300"]
    actions, _ = sync.plan(layout, state, GID, BOT, system_channel_id=sys_id)
    assert sorted(a["name"] for a in actions) == ["ADMIN", "ops-log"]
    assert all(a["op"] == "patch" and set(a["fields"]) == {"permission_overwrites"} for a in actions)


# ── {secret:share_link} ──────────────────────────────────────────────────────

def _env(link=FAKE_LINK, date="2026-10-01", **extra):
    env = {"DISCORD_TOKEN": "fake-token", "DISCORD_GUILD_ID": GID, **extra}
    if link is not None:
        env["TAILSCALE_SHARE_LINK"] = link
    if date is not None:
        env["TAILSCALE_SHARE_LINK_DATE"] = date
    return env


class FakeGuild:
    """A recording stand-in for sync.Api over a converged guild: content channels start empty, posts land."""

    def __init__(self, everyone_perms="0", layout=KIT_LAYOUT):  # None: the guild payload carries no roles
        self.everyone_perms = everyone_perms
        self.channels = converged_state(layout)
        self.sys_id = next(c["id"] for c in self.channels if c["name"] == "general")
        self.msgs, self.calls, self.nid = {}, [], 5000

    def __call__(self, method, path, body=None, file=None):
        self.calls.append((method, path, body))
        if path == "/users/@me":
            return {"id": BOT}
        if path == f"/guilds/{GID}/channels":
            return self.channels
        if path == f"/guilds/{GID}/roles/{GID}" and method == "PATCH":
            self.everyone_perms = body["permissions"]
            return {"id": GID, "permissions": body["permissions"]}
        if path == f"/guilds/{GID}":
            guild = {"system_channel_id": self.sys_id}
            if self.everyone_perms is not None:  # @everyone's role id is the guild id
                guild["roles"] = [{"id": "999", "permissions": "1"}, {"id": GID, "permissions": self.everyone_perms}]
            return guild
        m = re.fullmatch(r"/channels/(\d+)/messages(?:\?limit=100)?", path)
        if m and method == "GET":
            return list(reversed(self.msgs.get(m.group(1), [])))  # Discord: newest first
        if m and method == "POST":
            self.nid += 1
            atts = [{"id": "1", "filename": file["filename"], "size": file["size"]}] if file else []
            self.msgs.setdefault(m.group(1), []).append(
                {"id": str(self.nid), "author": {"id": BOT}, "content": body["content"], "attachments": atts})
            return {"id": str(self.nid)}
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def fake_pack(monkeypatch):
    monkeypatch.setattr(sync, "resolve_attachment", lambda name: PACK)


def test_share_link_resolves_and_fills():
    assert sync.resolve_secret("share_link", _env(), TODAY) == (FAKE_LINK, [])
    assert sync.fill_secrets("open <{secret:share_link}> now", {"share_link": FAKE_LINK}) == f"open <{FAKE_LINK}> now"
    assert sync.redact(f"x {FAKE_LINK} y") == "x <share_link> y"


@pytest.mark.parametrize("link", [
    None,                                   # key missing
    "",                                     # key empty
    "http://login.tailscale.com/admin/invite/FakeTestCode0123",
    "https://login.tailscale.com/admin/invite/",
    INVITE + "FakeTestCode0123/",           # trailing slash
    INVITE + "Fake-Test",                   # outside [A-Za-z0-9]
    '"' + FAKE_LINK + '"',                  # quoted in .env (the loader does not strip quotes)
    FAKE_LINK + " ",
    "https://evil.example/admin/invite/FakeTestCode0123",
    "https://login.tailscale.com.evil.example/admin/invite/FakeTestCode0123",
    "https://login-tailscale.com/admin/invite/FakeTestCode0123",   # the dots are literal
    "see " + FAKE_LINK,                                             # anchored at the start
])
def test_bad_share_link_is_refused_without_echoing_it(link):
    with pytest.raises(ValueError) as e:
        sync.resolve_secret("share_link", _env(link=link), TODAY)
    assert ("missing or empty" if not link else "is not a Tailscale share link") in str(e.value)
    assert "FakeTest" not in str(e.value)  # every non-empty bad value carries a FakeTest code


def test_warning_at_25_days_not_at_24():
    assert sync.resolve_secret("share_link", _env(date="2026-09-08"), TODAY)[1] == []  # 24 days
    _, (w,) = sync.resolve_secret("share_link", _env(date="2026-09-07"), TODAY)     # 25 days
    assert w == ("WARNING: share link is 25 days old; unused links may expire at 30 days, "
                 "regenerate it in the Tailscale admin and update discord/.env")
    assert sync.resolve_secret("share_link", _env(date="2026-10-02"), TODAY)[1] == []  # made today


@pytest.mark.parametrize("date", ["20261002", "2026-9-07", "2026-02-30", "yesterday", "2026-10-02T00:00",
                                  "2026-10-03"])  # the last: a future date would silence the warning
def test_bad_share_link_date_is_refused(date):
    with pytest.raises(ValueError, match="TAILSCALE_SHARE_LINK_DATE"):
        sync.resolve_secret("share_link", _env(date=date), TODAY)


def test_missing_date_is_a_note_not_a_failure():
    _, (note,) = sync.resolve_secret("share_link", _env(date=None), TODAY)
    assert "not set" in note and not note.startswith("WARNING")


def test_unknown_or_unresolved_secret_is_refused():
    with pytest.raises(ValueError, match="unknown secret"):
        sync.resolve_secret("nope", _env(), TODAY)
    with pytest.raises(ValueError, match="unresolved"):
        sync.fill_secrets("{secret:share_link}", {})


def _layout_dir(tmp_path, body):
    """A small discord/ for the tests' kit profile: #setup's text is `body` (the profile maps it to kit/setup.md)."""
    (tmp_path / "content" / "kit").mkdir(parents=True)
    (tmp_path / "layout.toml").write_text(
        '[[category]]\nname = "START HERE"\n  [[category.channel]]\n  name = "setup"\n  content = "setup.md"\n'
        '  [[category.channel]]\n  name = "the-story"\n  content = "story.md"\n')  # the kit profile drops it
    (tmp_path / "content" / "kit" / "setup.md").write_text(body)
    return tmp_path


@pytest.mark.parametrize("body", ["{secret:nope}", "{secret:Share_Link}", "{secret:}", "{secret:share link}",
                                  "ok {secret:share_link}\n---\n{secret:other}"])
def test_content_with_an_unknown_secret_name_refuses_the_sync(monkeypatch, tmp_path, body):
    monkeypatch.setattr(sync, "HERE", _layout_dir(tmp_path, body))
    api = FakeGuild()
    with pytest.raises(SystemExit) as e:
        sync.cmd_sync(api, GID, True, _env(), TODAY)
    assert "unknown secret" in str(e.value.code)
    assert api.calls == []


@pytest.mark.parametrize("env", [
    _env(link=None), _env(link=""), _env(link=INVITE + "bad/code"), _env(date="2026-13-01"),
])
@pytest.mark.parametrize("apply", [False, True])
def test_bad_share_link_refuses_the_shipped_sync_before_any_api_call(fake_pack, env, apply):
    api = FakeGuild()
    with pytest.raises(SystemExit) as e:
        sync.cmd_sync(api, GID, apply, env, TODAY)
    assert isinstance(e.value.code, str) and e.value.code.startswith("error:")  # nonzero exit
    assert api.calls == []  # nothing read, nothing applied


@pytest.mark.parametrize("apply", [False, True])
def test_sync_posts_the_link_but_never_prints_it(fake_pack, apply, capsys):
    api = FakeGuild()
    rc = sync.cmd_sync(api, GID, apply, _env(date="2026-09-07"), TODAY)
    out = capsys.readouterr()
    assert rc == (0 if apply else 2)
    assert "WARNING: share link is 25 days old" in out.out  # the warning shows in dry run and apply
    assert FAKE_LINK not in out.out + out.err
    assert "FakeTestCode0123" not in out.out + out.err
    if apply:  # positive half: the link really went into #setup, so its absence above is not vacuous
        posted = [b["content"] for m, p, b in api.writes() if m == "POST"]
        assert any(f"<{FAKE_LINK}>" in c for c in posted)
        assert not any("{secret:" in c for c in posted)
        assert out.out.rstrip().endswith("0 actions")  # converged on the filled text
    else:
        assert api.writes() == []


def test_api_error_never_echoes_a_secret(monkeypatch):
    import urllib.request
    sync.resolve_secret("share_link", _env(), TODAY)

    def bounce(req, timeout=0):  # e.g. a 400 that quotes the offending content back
        raise _http_error(400, json.dumps({"content": [f"bad: {FAKE_LINK}"]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", bounce)
    with pytest.raises(SystemExit) as e:
        sync.Api("t")("POST", "/channels/1/messages", {"content": FAKE_LINK})
    assert "<share_link>" in str(e.value.code) and FAKE_LINK not in str(e.value.code)

    def straddle(req, timeout=0):  # the 300-char cut lands 6 chars into the invite code
        raise _http_error(400, b"x" * (300 - len(INVITE) - 6) + FAKE_LINK.encode())

    monkeypatch.setattr(urllib.request, "urlopen", straddle)
    with pytest.raises(SystemExit) as e:
        sync.Api("t")("POST", "/channels/1/messages", {"content": "x"})
    assert "<share_link>" in str(e.value.code) and "invite/Fake" not in str(e.value.code)


def test_one_off_post_refuses_a_secret_before_printing(tmp_path, capsys):
    post = tmp_path / "p.md"
    post.write_text("join here: {secret:share_link}")
    api = FakeGuild()
    with pytest.raises(SystemExit, match=r"\{secret:\}"):
        sync.cmd_post(api, GID, "announcements", str(post), True)
    assert api.writes() == [] and capsys.readouterr().out == ""


def test_main_refuses_a_bad_dotenv_before_any_api_call(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DISCORD_TOKEN=fake\nTAILSCALE_SHARE_LINK={INVITE}x/y\n")
    monkeypatch.setattr(sync, "DOTENV", dotenv)
    for k in ("DISCORD_TOKEN", "DISCORD_GUILD_ID"):
        monkeypatch.delenv(k, raising=False)
    api = FakeGuild()
    monkeypatch.setattr(sync, "Api", lambda token: api)
    monkeypatch.setattr(sync.sys, "argv", ["sync.py", "--apply"])
    with pytest.raises(SystemExit, match="TAILSCALE_SHARE_LINK"):
        sync.main()
    assert api.calls == []


def test_share_link_is_read_from_the_dotenv_only(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"# c\nTAILSCALE_SHARE_LINK = {FAKE_LINK}\nTAILSCALE_SHARE_LINK_DATE=2026-10-01\n")
    monkeypatch.setattr(sync, "DOTENV", dotenv)
    monkeypatch.setenv("TAILSCALE_SHARE_LINK", INVITE + "FromEnviron")
    env = sync.read_env()
    assert env["TAILSCALE_SHARE_LINK"] == FAKE_LINK and env["TAILSCALE_SHARE_LINK_DATE"] == "2026-10-01"


def test_env_example_placeholders_refuse(monkeypatch):
    monkeypatch.setattr(sync, "DOTENV", Path(__file__).parent / ".env.example")
    env = sync.read_env()
    assert {"TAILSCALE_SHARE_LINK", "TAILSCALE_SHARE_LINK_DATE"} <= set(env)
    with pytest.raises(ValueError, match="TAILSCALE_SHARE_LINK in discord/.env"):
        sync.resolve_secret("share_link", env, TODAY)  # a copied, unedited example never syncs a fake link
    with pytest.raises(ValueError, match="TAILSCALE_SHARE_LINK_DATE"):
        sync.resolve_secret("share_link", {**env, "TAILSCALE_SHARE_LINK": FAKE_LINK}, TODAY)


@pytest.mark.parametrize("name", SETUPS)
def test_shipped_setup_gives_the_share_link_once(name):
    text = (Path(__file__).parent / "content" / name).read_text()
    assert sync.SECRET.findall(text) == ["share_link"]
    assert "<{secret:share_link}>" in sync.split_parts(text)[0]  # step 1, embed suppressed


def test_no_tracked_file_contains_a_share_link():
    """The link is an access credential: it lives only in discord/.env, never in git."""
    repo = Path(__file__).resolve().parent.parent
    try:
        listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                cwd=repo, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    link = re.compile(sync.SHARE_LINK.pattern.strip("^$"))
    names = [n for n in listed.decode().split("\0") if n]
    assert "discord/sync.py" in names  # the listing is real, not vacuously empty
    leaks = [n for n in names if (repo / n).is_file()
             and link.search((repo / n).read_bytes().decode("utf-8", "replace"))]
    assert leaks == []


# ── deploy order: appending the keys to a live discord/.env ──────────────────

def test_missing_link_refusal_names_the_tool_that_replaces_it():
    """Never a `>>` append (review 2026-10-16): the link would go into argv and the shell history, and every rotation
    would leave another pair of lines. kit/secret.py share-link reads it hidden and replaces the key in place."""
    with pytest.raises(ValueError) as e:
        sync.resolve_secret("share_link", _env(link=None), TODAY)
    assert "python3 kit/secret.py share-link" in str(e.value) and ">>" not in str(e.value)
    assert '"share-link": (' in (sync.REPO / "kit" / "secret.py").read_text()  # the command it names exists


@pytest.mark.parametrize("dotenv_text,glued", [
    # last line was the guild id with no trailing newline, then a newline-less append landed on it
    (f"DISCORD_TOKEN=fake\nDISCORD_GUILD_ID={GID}TAILSCALE_SHARE_LINK={INVITE}FakeGlued0\n"
     "TAILSCALE_SHARE_LINK_DATE=2026-10-02\n", "TAILSCALE_SHARE_LINK"),
    # same, onto the token line: without the guard this reads as "link missing" and the bad token goes unnamed
    (f"DISCORD_GUILD_ID={GID}\nDISCORD_TOKEN=fakeTAILSCALE_SHARE_LINK={INVITE}FakeGlued0\n", "TAILSCALE_SHARE_LINK"),
    (f"TAILSCALE_SHARE_LINK={INVITE}FakeGlued0TAILSCALE_SHARE_LINK_DATE=2026-10-02\n"
     f"DISCORD_TOKEN=fake\nDISCORD_GUILD_ID={GID}\n", "TAILSCALE_SHARE_LINK_DATE"),
])
def test_a_glued_dotenv_line_is_refused_before_any_api_call(monkeypatch, tmp_path, capsys, dotenv_text, glued):
    dotenv = tmp_path / ".env"
    dotenv.write_text(dotenv_text)
    monkeypatch.setattr(sync, "DOTENV", dotenv)
    for k in ("DISCORD_TOKEN", "DISCORD_GUILD_ID"):
        monkeypatch.delenv(k, raising=False)
    api = FakeGuild()
    monkeypatch.setattr(sync, "Api", lambda token: api)
    monkeypatch.setattr(sync, "resolve_attachment", lambda name: PACK)
    monkeypatch.setattr(sync.sys, "argv", ["sync.py"])
    with pytest.raises(SystemExit) as e:
        sync.main()
    msg = str(e.value.code)
    assert f"also contains {glued}=" in msg
    assert "FakeGlued0" not in msg + capsys.readouterr().out  # names the keys, never the merged value
    assert api.calls == []


def test_retry_exhaustion_message_is_redacted(monkeypatch):
    import urllib.request
    sync.resolve_secret("share_link", _env(), TODAY)

    def unavailable(req, timeout=0):
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as e:  # e.g. a guild id that absorbed the link puts it in the path
        sync.Api("t")("GET", f"/guilds/{GID}{FAKE_LINK}/channels")
    assert "still failing" in str(e.value.code) and FAKE_LINK not in str(e.value.code)


# ── invite exposure: the link in #setup + Create Invite for @everyone ────────

INVITE_WARNING = "WARNING: @everyone can create invites"


def cfg_with(base=None, **changes):
    """A config (the example's, or `base`) with some keys changed: {"discord.invite_lockdown": False, ...}."""
    cfg = copy.deepcopy(CFG if base is None else base)
    for dotted, value in changes.items():
        section, key = dotted.split(".")
        if value is None:
            cfg[section].pop(key, None)
        else:
            cfg[section][key] = value
    return cfg


NO_LOCKDOWN = {"discord.invite_lockdown": False}


@pytest.mark.parametrize("perms,expect", [
    (str(sync.CREATE_INVITE | sync.VIEW), INVITE_WARNING),
    (str(sync.VIEW | sync.SEND), None),
    (None, "whether members can create invites is unverified"),  # absent is reported, not read as safe
])
@pytest.mark.parametrize("apply", [False, True])
def test_without_lockdown_sync_warns_once_when_everyone_can_create_invites(fake_pack, capsys, perms, expect, apply):
    api = FakeGuild(everyone_perms=perms)
    sync.cmd_sync(api, GID, apply, _env(), TODAY, cfg=cfg_with(**NO_LOCKDOWN))
    out = capsys.readouterr().out
    if expect:
        assert out.count(expect) == 1  # once per run, not once per apply pass
    else:
        assert "invites" not in out
    assert "FakeTestCode0123" not in out
    assert not any("/roles/" in path for _, path, _ in api.writes())  # lockdown off: a warning, never a change


# ── invite lockdown: kit.toml discord.invite_lockdown = true (operator ruling 2026-10-14) ─

def test_lockdown_clears_only_create_invite():
    perms = sync.CREATE_INVITE | sync.VIEW | sync.SEND | (1 << 40)
    guild = {"roles": [{"id": "999", "permissions": "1"}, {"id": GID, "permissions": str(perms)}]}
    a = sync.plan_lockdown(guild, GID)
    assert a["op"] == "role" and a["id"] == GID
    assert a["fields"] == {"permissions": str(sync.VIEW | sync.SEND | (1 << 40))}  # a string, every other bit kept
    guild["roles"][1]["permissions"] = str(sync.VIEW)
    assert sync.plan_lockdown(guild, GID) is None
    guild["roles"][0]["permissions"] = str(sync.CREATE_INVITE)  # another role's bit is not @everyone's
    assert sync.plan_lockdown(guild, GID) is None


@pytest.mark.parametrize("guild", [{}, {"roles": []}, {"roles": [{"id": GID}]}, {"roles": [{"id": "999"}]}])
def test_lockdown_refuses_when_everyone_is_unreadable(guild):
    with pytest.raises(ValueError, match="cannot be verified"):
        sync.plan_lockdown(guild, GID)


def test_lockdown_dry_run_shows_the_change_and_apply_converges(fake_pack, capsys):
    perms = str(sync.CREATE_INVITE | sync.VIEW)
    api = FakeGuild(everyone_perms=perms)
    assert sync.cmd_sync(api, GID, False, _env(), TODAY) == 2
    out = capsys.readouterr().out
    assert "~ role @everyone: Create Invite off" in out and api.writes() == []
    assert INVITE_WARNING not in out  # lockdown on: an action, not a warning
    assert sync.cmd_sync(api, GID, True, _env(), TODAY) == 0
    assert ("PATCH", f"/guilds/{GID}/roles/{GID}", {"permissions": str(sync.VIEW)}) in api.writes()
    assert capsys.readouterr().out.rstrip().endswith("0 actions")
    assert api.everyone_perms == str(sync.VIEW)


def test_lockdown_already_off_is_zero_role_actions(fake_pack, capsys):
    api = FakeGuild(everyone_perms=str(sync.VIEW))
    sync.cmd_sync(api, GID, True, _env(), TODAY)
    assert not any("/roles/" in path for _, path, _ in api.writes())


@pytest.mark.parametrize("apply", [False, True])
def test_lockdown_with_unreadable_roles_refuses_the_sync(fake_pack, apply):
    api = FakeGuild(everyone_perms=None)
    with pytest.raises(SystemExit, match="cannot be verified"):
        sync.cmd_sync(api, GID, apply, _env(), TODAY)
    assert api.writes() == []


INSTANCE_DOC = sync.REPO / "docs" / "instance" / "asus-node.md"


def test_the_merge_gate_in_the_instance_record_names_what_sync_prints():
    """asus-node's gate before the first lockdown --apply (docs/instance/asus-node.md) tells the operator which line
    means the live guild is about to change. If sync's wording drifts, the gate would look for a line that never comes
    and read as all-clear."""
    if not RECORD:
        pytest.skip(NO_RECORD)
    text = INSTANCE_DOC.read_text()  # with the record here, a moved doc fails, never skips
    start = text.index("**Gate before the first `--apply` of a sync with the lockdown and profiles")
    gate = text[start:text.index("\n- ", start)]
    unlocked = {"roles": [{"id": GID, "permissions": str(sync.CREATE_INVITE | sync.VIEW)}]}
    line = sync.describe(sync.plan_lockdown(unlocked, GID)).split(" (kit.toml")[0]
    assert f"`{line}`" in gate
    with pytest.raises(ValueError) as e:
        sync.plan_lockdown({"roles": []}, GID)
    assert "cannot be verified" in str(e.value) and '"cannot be verified" refusal' in gate
    assert "`cp kit/fixtures/asus-node.toml kit.toml`" in gate and "`0 actions`" in gate
    assert "`discord.profile: missing`" in gate


def test_invite_check_reads_only_the_everyone_role():
    guild = {"roles": [{"id": "999", "permissions": str(sync.CREATE_INVITE)}, {"id": GID, "permissions": "0"}]}
    assert sync.invite_exposure(guild, GID) is None
    guild["roles"][1]["permissions"] = str(sync.CREATE_INVITE)
    assert sync.invite_exposure(guild, GID).startswith(INVITE_WARNING)


def test_no_invite_check_without_the_share_link(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "HERE", _layout_dir(tmp_path, "no secrets here"))
    sync.cmd_sync(FakeGuild(everyone_perms=str(sync.CREATE_INVITE)), GID, False, {}, TODAY)
    assert "invites" not in capsys.readouterr().out


# ── kit.toml: the guild id and the bridge bot id have one home ───────────────

def test_the_bridge_bot_is_named_in_the_layout_and_its_id_comes_from_kit_toml():
    text = (Path(__file__).parent / "layout.toml").read_text()
    assert not re.search(r"\b\d{17,20}\b", text)  # no instance id in the layout
    admin = next(c for c in LAYOUT["categories"] if c["name"] == "ADMIN")
    assert admin["extra_bots"] == [KIT["bridge_bot_id"]]
    actions, _ = sync.plan(LAYOUT, [], GID, BOT)
    ops = next(a for a in actions if a["name"] == "ops-log")["overwrites"]
    assert {o["id"] for o in ops if o["type"] == 1} == {BOT, KIT["bridge_bot_id"]}


def test_a_named_bot_without_ids_or_an_unknown_name_is_refused():
    named = '[[category]]\nname = "A"\nprivate = true\nextra_bots = ["bridge"]\n'
    with pytest.raises(ValueError, match="no bot ids"):
        sync.load_layout(named)  # never an overwrite for a user literally called "bridge"
    with pytest.raises(ValueError, match="extra_bots"):
        sync.load_layout(named.replace('"bridge"', '"relay"'), BOTS)
    assert sync.load_layout(named, {"bridge": "300"})["categories"][0]["extra_bots"] == ["300"]


def _main(monkeypatch, tmp_path, dotenv_text, environ_gid=None):
    dotenv = tmp_path / ".env"
    dotenv.write_text(dotenv_text)
    monkeypatch.setattr(sync, "DOTENV", dotenv)
    if environ_gid:
        monkeypatch.setenv("DISCORD_GUILD_ID", environ_gid)
    seen = {}
    monkeypatch.setattr(sync, "Api", lambda token: "api")
    monkeypatch.setattr(sync, "cmd_sync", lambda api, gid, apply, env, bots=None, cfg=None: seen.update(
        gid=gid, bots=bots, profile=cfg["discord"]["profile"]))
    monkeypatch.setattr(sync.sys, "argv", ["sync.py"])
    sync.main()
    return seen


def test_main_takes_the_guild_and_the_bridge_bot_from_kit_toml(monkeypatch, tmp_path):
    seen = _main(monkeypatch, tmp_path, "DISCORD_TOKEN=fake\n")
    assert seen == {"gid": KIT["guild_id"], "bots": {"bridge": KIT["bridge_bot_id"]}, "profile": "kit"}
    # a legacy DISCORD_GUILD_ID that agrees is accepted (an older discord/.env may carry one)
    assert _main(monkeypatch, tmp_path, f"DISCORD_TOKEN=fake\nDISCORD_GUILD_ID={KIT['guild_id']}\n")["gid"] == \
        KIT["guild_id"]


@pytest.mark.parametrize("where", ["dotenv", "environ"])
def test_a_guild_id_that_disagrees_with_kit_toml_refuses_before_any_api(monkeypatch, tmp_path, where):
    made = []
    monkeypatch.setattr(sync, "Api", lambda token: made.append(token))
    dotenv = "DISCORD_TOKEN=fake\n" + ("DISCORD_GUILD_ID=1000000000000000001\n" if where == "dotenv" else "")
    (tmp_path / ".env").write_text(dotenv)
    monkeypatch.setattr(sync, "DOTENV", tmp_path / ".env")
    if where == "environ":
        monkeypatch.setenv("DISCORD_GUILD_ID", "1000000000000000001")
    monkeypatch.setattr(sync.sys, "argv", ["sync.py"])
    with pytest.raises(SystemExit, match="differs from kit.toml"):
        sync.main()
    assert made == []


def test_sync_refuses_without_kit_toml(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "KIT_TOML", tmp_path / "absent.toml")
    with pytest.raises(SystemExit, match="kit.toml"):
        sync.load_kit_config()


# ── {config:} and the guild profile ──────────────────────────────────────────

def _filled(name, cfg):
    text = (Path(__file__).parent / "content" / name).read_text()
    return sync.fill_config(text, sync.resolve_config(sync.CONFIG_REF.findall(text), cfg))


@needs_record
@pytest.mark.parametrize("name", ["welcome.md", "setup.md"])
def test_templated_content_under_the_asus_node_fixture_is_what_the_guild_shows_today(name):
    """The kit gate for content: filled from kit/fixtures/asus-node.toml, the templated file is byte for byte the file
    sync posted before the kit (kit/fixtures/asus-node/discord/, copied from 62c28f1)."""
    assert "{config:" in (Path(__file__).parent / "content" / name).read_text()  # really templated
    assert _filled(name, asus_cfg()) == (GOLDEN / name).read_text()


@pytest.mark.parametrize("name", SETUPS)
def test_a_kit_instance_gets_its_own_address_and_no_asus_node_literal(name):
    cfg = cfg_with(**{"tailscale.host_name": "kit-box", "tailscale.server_address": "kit-box.example-net.ts.net",
                      "server.open_date": None})
    setup, welcome = _filled(name, cfg), _filled("welcome.md", cfg)
    assert "Add Server: `kit-box.example-net.ts.net`" in setup and "list **kit-box**" in setup
    assert "a fresh world is being built right now. Watch {#announcements} for launch." in welcome
    literals = ["asus-node", "October 2, 2026"]
    if RECORD:  # its address and its tailnet's name, read from its record, never spelled here
        address = asus_cfg()["tailscale"]["server_address"]
        literals += [address, address.split(".")[1]]
    for literal in literals:
        assert literal not in setup + welcome


@pytest.mark.parametrize("opened,line", [
    (datetime.date(2026, 10, 2), "the world is open (since October 2, 2026). New chapters get announced in "
                                 "{#announcements}."),
    (datetime.date(2027, 1, 9), "the world is open (since January 9, 2027). New chapters get announced in "
                                "{#announcements}."),   # no zero-padded day
    (None, "a fresh world is being built right now. Watch {#announcements} for launch."),
])
def test_world_status_follows_open_date(opened, line):
    assert sync.world_status(cfg_with(**{"server.open_date": opened})) == line


@pytest.mark.parametrize("body", ["{config:nope}", "{config:tailscale.server_address}", "{config:}",
                                  "{config:Server_Address}", "ok {config:host_name}\n---\n{config:guild_id}"])
def test_content_with_an_unknown_config_name_refuses_the_sync(monkeypatch, tmp_path, body):
    monkeypatch.setattr(sync, "HERE", _layout_dir(tmp_path, body))
    api = FakeGuild()
    with pytest.raises(SystemExit) as e:
        sync.cmd_sync(api, GID, True, _env(), TODAY)
    assert "unknown config" in str(e.value.code)
    assert api.calls == []


def test_unresolved_config_is_refused():
    with pytest.raises(ValueError, match="unresolved config"):
        sync.fill_config("{config:host_name}", {})


def test_one_off_post_fills_config_and_refuses_an_unknown_name(tmp_path, capsys):
    post = tmp_path / "p.md"
    post.write_text("connect to {config:server_address}")
    api = FakeGuild()
    sync.cmd_post(api, GID, "announcements", str(post), True, CFG)
    (posted,) = [b["content"] for m, _, b in api.writes()]
    assert posted == f"connect to {CFG['tailscale']['server_address']}"
    post.write_text("{config:nope}")
    api = FakeGuild()
    with pytest.raises(SystemExit, match="unknown config"):
        sync.cmd_post(api, GID, "announcements", str(post), True, CFG)
    assert api.calls == []


def _layout_text():
    return (Path(__file__).parent / "layout.toml").read_text()


def test_the_asus_node_profile_is_the_layout_as_written():
    assert sync.load_layout(_layout_text(), BOTS, "asus-node") == sync.load_layout(_layout_text(), BOTS) == LAYOUT
    renames = [ch["renamed_from"] for c in LAYOUT["categories"] for ch in c["channels"] if ch["renamed_from"]]
    assert renames  # the asus-node guild's history is still honoured
    story = next(ch for c in LAYOUT["categories"] for ch in c["channels"] if ch["name"] == "the-story")
    assert story["content"] == "story.md"


def test_the_kit_profile_has_no_renames_and_no_story():
    kit = sync.load_layout(_layout_text(), BOTS, "kit")
    chans = [ch for c in kit["categories"] for ch in c["channels"]]
    assert all(ch["renamed_from"] == [] for ch in chans)
    assert "the-story" not in {ch["name"] for ch in chans}
    created = {a["name"] for a in sync.plan(kit, [], GID, BOT)[0]}  # a fresh guild never gets one
    assert "the-story" not in created and "welcome" in created
    # everything else is the same layout: one home, the profile changes only the history
    strip = [{**ch, "renamed_from": [], "content": ch["content"] and Path(ch["content"]).name} for ch in chans]
    assert strip == [{**ch, "renamed_from": []} for c in LAYOUT["categories"] for ch in c["channels"]
                     if ch["name"] != "the-story"]


def test_a_profile_that_drops_a_channel_the_layout_lacks_is_refused(monkeypatch):
    """A renamed #the-story would slip past the drop and reach every kit guild; the stale drop is refused instead."""
    monkeypatch.setitem(sync.PROFILES, "kit", {**sync.PROFILES["kit"], "drop": ("the-saga",)})
    with pytest.raises(ValueError, match="drops the-saga, which layout.toml does not list"):
        sync.load_layout(_layout_text(), BOTS, "kit")


def test_unlisted_is_what_plan_leaves_alone():
    """Discord's template (any language): its #general sits outside THE WORLD, so it is unlisted, as is its
    category and voice channel; the layout's own #general is kept."""
    kit = sync.load_layout(_layout_text(), BOTS, "kit")
    template = [{"id": "1", "name": "Salons textuels", "type": sync.CATEGORY, "position": 0},
                {"id": "2", "name": "général", "type": sync.TEXT, "position": 0, "parent_id": "1"},
                {"id": "3", "name": "general", "type": sync.TEXT, "position": 1, "parent_id": "1"},
                {"id": "4", "name": "Général", "type": sync.VOICE, "position": 0, "parent_id": None}]
    state = converged_state(kit) + template
    assert [c["id"] for c in sync.unlisted(kit, state)] == ["1", "2", "3", "4"]
    notes = sync.plan(kit, state, GID, BOT)[1]
    assert len(notes) == 4 and all("left alone" in n for n in notes)  # the same four, by the same matching
    assert sync.unlisted(kit, converged_state(kit)) == []


def test_a_kit_guild_never_renames_a_channel_that_shares_an_old_name():
    """A fresh guild that happens to hold a voice channel called Overworld keeps it; the kit creates its own."""
    kit = sync.load_layout(_layout_text(), BOTS, "kit")
    voice = next(c for c in kit["categories"] if c["name"] == "VOICE")
    layout = {"system_channel": None, "categories": [voice]}
    current = [{"id": "1", "name": "VOICE", "type": sync.CATEGORY, "position": 0, "permission_overwrites": []},
               {"id": "2", "name": "Overworld", "type": sync.VOICE, "position": 0, "parent_id": "1",
                "permission_overwrites": []}]
    actions, notes = sync.plan(layout, current, GID, BOT)
    assert not any(a["op"] == "patch" for a in actions)
    assert [a["name"] for a in actions if a["op"] == "create"] == [ch["name"] for ch in voice["channels"]]
    assert any("Overworld" in n for n in notes)


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError, match="unknown profile"):
        sync.load_layout(_layout_text(), BOTS, "kitt")


@pytest.mark.parametrize("apply", [False, True])
def test_a_kit_guild_syncs_its_filled_content_and_no_story(fake_pack, capsys, apply):
    cfg = kit_cfg()
    api = FakeGuild(layout=sync.load_layout(_layout_text(), BOTS, "kit"))
    rc = sync.cmd_sync(api, GID, apply, _env(), TODAY, cfg=cfg)
    assert rc == (0 if apply else 2)
    if not apply:
        return
    assert "the-story" not in {c["name"] for c in api.channels}
    posted = {ch: [b["content"] for m, p, b in api.writes() if p == f"/channels/{ch}/messages"]
              for ch in {c["id"] for c in api.channels}}
    everything = "\n".join(sum(posted.values(), []))
    assert "Add Server: `kit-box.example-net.ts.net`" in everything and "{config:" not in everything
    assert "asus-node" not in everything
    # the kit's own #setup (discord/content/kit/setup.md): the Modrinth App's current import path and its warning
    assert "**Create new instance** → **Upload a modpack**" in everything and "unknown modpack" in everything
    assert "**From file**" not in everything
    # the server list names the pack this instance builds (servers.dat = pack/mods.toml [pack] name here)
    assert "Multiplayer → **Vanilla Extended** is already in your list" in everything
    assert capsys.readouterr().out.rstrip().endswith("0 actions")


# ── the P6 stop: a kit guild never gets the asus-node guild's text, guild or address ─────────────────────────────
# Each case runs twice: against asus-node's own record and text (skipped where a copy dropped them), and against a
# stand-in instance built in tmp_path (its own fixture, story, #setup and post), which every copy has.

KIT_GID = "1000000000000000100"  # a kit guild's id: neither instance's
STANDIN_GID = "1700000000000000071"
STANDIN_ADDRESS = "standin.example-net.ts.net"


class Instance:
    """An instance the P6 stop protects: its guild id, its address, its own texts, and its kit.toml."""

    def __init__(self, gid, address, story, posts, cfg):
        self.gid, self.address, self.story, self.posts, self.cfg = gid, address, story, posts, cfg

    def own_texts(self):
        return self.posts + [self.story]


def standin_record(monkeypatch, root, fixture_text, drop=()):
    """A tree holding a stand-in asus-node record (kit/instance.py RECORD, less `drop`), with fixture_text as its
    kit.toml; sync looks for it through INSTANCE_REPO and ASUS_NODE_FIXTURE, which point here for the test."""
    for rel in kit_instance.RECORD:
        p = root / rel
        if rel in drop:
            continue
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("stand-in\n")
    fixture = root / FIXTURE.relative_to(sync.REPO)
    if fixture.exists():
        fixture.write_text(fixture_text)
    monkeypatch.setattr(sync, "INSTANCE_REPO", root)
    monkeypatch.setattr(sync, "ASUS_NODE_FIXTURE", fixture)
    return fixture


def standin_toml(gid=None, address=None):
    """kit/fixtures/example.toml with the stand-in's guild and address."""
    return (EXAMPLE.read_text().replace(f'"{KIT["guild_id"]}"', f'"{gid or STANDIN_GID}"')
            .replace(f'"{CFG["tailscale"]["server_address"]}"', f'"{address or STANDIN_ADDRESS}"'))


def standin_instance(monkeypatch, tmp_path):
    """discord/ with a stand-in instance's record and text: the real layout, kit/ and KIT_SHARED_CONTENT, plus a story,
    a #setup as posted and a one-off post of its own, and a record whose fixture names its guild and address. sync
    reads all of them through HERE, ASUS_NODE_POSTS, INSTANCE_REPO and ASUS_NODE_FIXTURE, which point here."""
    d = tmp_path / "standin-discord"
    (d / "content" / "kit").mkdir(parents=True)
    (d / "posts").mkdir()
    (d / "layout.toml").write_text(_layout_text())
    (d / "content" / "kit" / "setup.md").write_text((sync.HERE / "content" / "kit" / "setup.md").read_text())
    for name in sync.KIT_SHARED_CONTENT:
        (d / "content" / name).write_text((sync.HERE / "content" / name).read_text())
    (d / "content" / "story.md").write_text("The stand-in story: the first night on the stand-in server.\n")
    (d / "content" / "setup.md").write_text((sync.HERE / "content" / "kit" / "setup.md").read_text())
    (d / "posts" / "2026-01-01_standin.md").write_text("The stand-in server opens tonight.\n")
    standin_record(monkeypatch, tmp_path / "standin-repo", standin_toml())
    monkeypatch.setattr(sync, "HERE", d)
    monkeypatch.setattr(sync, "ASUS_NODE_POSTS", d / "posts")
    assert sync.asus_node_facts() == (STANDIN_GID, STANDIN_ADDRESS)
    cfg = cfg_with(**{"discord.profile": "asus-node", "discord.guild_id": STANDIN_GID,
                      "tailscale.host_name": "standin", "tailscale.server_address": STANDIN_ADDRESS})
    return Instance(STANDIN_GID, STANDIN_ADDRESS, d / "content" / "story.md", [d / "posts" / "2026-01-01_standin.md"],
                    cfg)


def asus_node_instance():
    cfg = asus_cfg()  # skips without the record
    return Instance(cfg["discord"]["guild_id"], cfg["tailscale"]["server_address"], sync.HERE / "content" / "story.md",
                    sorted(sync.ASUS_NODE_POSTS.glob("*.md")), cfg)


@pytest.fixture(params=["standin", "asus-node"])
def instance(request, monkeypatch, tmp_path):
    if request.param == "standin":
        return standin_instance(monkeypatch, tmp_path)
    return asus_node_instance()


def kit_cfg(**changes):
    return cfg_with(**{"discord.profile": "kit", "discord.guild_id": KIT_GID, "tailscale.host_name": "kit-box",
                       "tailscale.server_address": "kit-box.example-net.ts.net", "server.open_date": None,
                       **changes})


def _sync_refused(cfg, match, apply=True, **kw):
    api = FakeGuild(**kw)
    with pytest.raises(SystemExit) as e:
        sync.cmd_sync(api, GID, apply, _env(), TODAY, cfg=cfg)
    assert re.search(match, str(e.value.code)), e.value.code
    assert api.calls == []  # refused before the first API call
    return api


def _asus_story_into_kit(monkeypatch):
    """A kit profile that lost its drop: the layout would hand a kit guild discord/content/story.md."""
    monkeypatch.setitem(sync.PROFILES, "kit", {"renames": False, "drop": (), "content": {"setup.md": "kit/setup.md"}})


def _asus_setup_into_kit(monkeypatch):
    monkeypatch.setitem(sync.PROFILES, "kit", {"renames": False, "drop": ("the-story",), "content": {}})


GUARD_CASES = {
    # name: (cfg from the instance, monkeypatch setup or None, what the refusal names)
    "asus_node_profile_on_another_guild": (lambda inst: cfg_with(inst.cfg, **{"discord.guild_id": KIT_GID}), None,
                                           r'profile = "asus-node" .* pinned to that guild'),
    "kit_profile_on_the_asus_node_guild": (lambda inst: kit_cfg(**{"discord.guild_id": inst.gid}), None,
                                           r"asus-node guild's: a kit never syncs that guild"),
    "asus_node_story_in_a_kit_layout": (lambda inst: kit_cfg(), _asus_story_into_kit,
                                        r"#the-story: discord/content/story\.md is the asus-node guild's own text"),
    "asus_node_setup_in_a_kit_layout": (lambda inst: kit_cfg(), _asus_setup_into_kit,
                                        r"#setup: discord/content/setup\.md is the asus-node guild's own text"),
    "asus_node_address_in_a_kit_toml": (lambda inst: kit_cfg(**{"tailscale.server_address": inst.address}),
                                        None, r"holds the asus-node server's address"),
}


@pytest.mark.parametrize("name", sorted(GUARD_CASES))
def test_the_p6_stop_refuses_before_any_api_call(monkeypatch, fake_pack, instance, name):
    cfg, setup, match = GUARD_CASES[name]
    if setup:
        setup(monkeypatch)
    _sync_refused(cfg(instance), match)


def test_a_kit_post_of_the_asus_node_guilds_own_text_is_refused(tmp_path, instance):
    assert instance.posts  # a loop over nothing would pass without refusing anything
    for f in instance.own_texts():
        api = FakeGuild()
        with pytest.raises(SystemExit, match="asus-node guild's own text") as e:
            sync.cmd_post(api, GID, "announcements", str(f), True, kit_cfg())
        assert "from any other path" in str(e.value.code)  # and it says where a kit's own post goes
        assert api.calls == []
    assert "post announcements discord/posts/" not in sync.__doc__  # the usage never sends a kit there
    own = tmp_path / "hello.md"  # a kit's own one-off post goes through
    own.write_text("hello from {config:host_name}")
    api = FakeGuild()
    sync.cmd_post(api, GID, "announcements", str(own), True, kit_cfg())
    assert [b["content"] for _, _, b in api.writes()] == ["hello from kit-box"]


def test_a_kit_post_naming_the_asus_node_address_is_refused(tmp_path, instance):
    post = tmp_path / "p.md"
    post.write_text(f"join us at {instance.address}")
    api = FakeGuild()
    with pytest.raises(SystemExit, match="asus-node server's address"):
        sync.cmd_post(api, GID, "announcements", str(post), True, kit_cfg())
    assert api.calls == []


def test_the_asus_node_profile_still_posts_its_own_text(instance):
    """The stop is for other guilds: the asus-node profile on its own guild posts discord/posts/ as before."""
    api = FakeGuild()
    sync.cmd_post(api, GID, "announcements", str(instance.posts[0]), True, instance.cfg)
    assert api.writes()


def test_the_asus_node_profile_syncs_its_own_guild(monkeypatch, tmp_path, fake_pack):
    """The other half of the pin: on its own guild, the asus-node profile syncs its story (the stand-in's here)."""
    inst = standin_instance(monkeypatch, tmp_path)
    api = FakeGuild(layout=LAYOUT)
    assert sync.cmd_sync(api, GID, True, _env(), TODAY, cfg=inst.cfg) == 0
    assert any("stand-in story" in b["content"] for m, _, b in api.writes() if m == "POST")


@pytest.mark.parametrize("rel,own", [
    ("content/story.md", True), ("content/story-intents.md", True), ("content/setup.md", True),
    ("posts/2026-10-02_launch.md", True), ("content/kit/story.md", False), ("content/kit/setup.md", False),
    ("content/welcome.md", False), ("content/kit/../story.md", True),
])
def test_asus_node_only_classifies_the_guilds_own_text(rel, own):
    assert sync.asus_node_only(sync.HERE / rel) is own  # by path alone: the files need not exist
    assert sync.asus_node_only("/elsewhere/hello.md") is False


# ── without the record (a copy that dropped kit/INSTANCE-PATHS) ──────────────────────────────────────────────────

def _without_the_record(monkeypatch, tmp_path):
    copy = tmp_path / "a-copy"  # a tree with none of kit/instance.py RECORD
    copy.mkdir()
    monkeypatch.setattr(sync, "INSTANCE_REPO", copy)
    monkeypatch.setattr(sync, "ASUS_NODE_FIXTURE", copy / FIXTURE.relative_to(sync.REPO))
    assert sync.asus_node_facts() is None


@pytest.mark.parametrize("apply", [False, True])
def test_without_the_record_a_kit_guild_syncs(monkeypatch, tmp_path, fake_pack, capsys, apply):
    _without_the_record(monkeypatch, tmp_path)
    api = FakeGuild()
    assert sync.cmd_sync(api, GID, apply, _env(), TODAY, cfg=kit_cfg()) == (0 if apply else 2)
    assert bool(api.writes()) is apply
    if apply:
        assert capsys.readouterr().out.rstrip().endswith("0 actions")


def test_without_the_record_a_kit_post_goes_through(monkeypatch, tmp_path):
    _without_the_record(monkeypatch, tmp_path)
    post = tmp_path / "p.md"
    post.write_text("connect to {config:server_address}")
    api = FakeGuild()
    sync.cmd_post(api, GID, "announcements", str(post), True, kit_cfg())
    assert [b["content"] for _, _, b in api.writes()] == ["connect to kit-box.example-net.ts.net"]


def test_without_the_record_the_kit_still_refuses_the_guilds_own_text(monkeypatch, tmp_path, fake_pack):
    """The text checks go by path, not by the record: a kit layout that would post discord/content/story.md still
    stops, with or without the fixture."""
    inst = standin_instance(monkeypatch, tmp_path)
    _without_the_record(monkeypatch, tmp_path)
    _asus_story_into_kit(monkeypatch)
    _sync_refused(kit_cfg(), r"#the-story: discord/content/story\.md is the asus-node guild's own text")
    with pytest.raises(SystemExit, match="asus-node guild's own text"):
        sync.cmd_post(FakeGuild(), GID, "announcements", str(inst.posts[0]), True, kit_cfg())


ABSENT = r'profile = "asus-node" .* instance record .*kit/INSTANCE-PATHS.* is not in this tree'


@pytest.mark.parametrize("apply", [False, True])
def test_without_the_record_the_asus_node_profile_refuses_by_name(monkeypatch, tmp_path, fake_pack, apply):
    """Its record is what pins it to its guild; without it the profile cannot be checked, so it never runs. The
    refusal comes before the content is read, so it names the record, not a missing story.md."""
    _without_the_record(monkeypatch, tmp_path)
    monkeypatch.setattr(sync, "HERE", tmp_path / "no-content")  # a copy: no story.md, no setup.md
    (tmp_path / "no-content").mkdir()
    (tmp_path / "no-content" / "layout.toml").write_text(_layout_text())
    _sync_refused(cfg_with(**{"discord.profile": "asus-node"}), ABSENT, apply=apply)


def test_without_the_record_an_asus_node_post_refuses(monkeypatch, tmp_path):
    _without_the_record(monkeypatch, tmp_path)
    post = tmp_path / "p.md"
    post.write_text("hello")
    api = FakeGuild()
    with pytest.raises(SystemExit, match=ABSENT):
        sync.cmd_post(api, GID, "announcements", str(post), True, cfg_with(**{"discord.profile": "asus-node"}))
    assert api.calls == []


BROKEN = {
    "not_toml": "[discord\n",
    "keys_missing": '[discord]\nguild_id = "1"\n',
    "no_discord": "[tailscale]\n",
    # it parses, but its values would turn the stop off: a list guild id, an empty address (validated, never str()ed)
    "guild_id_a_list": standin_toml().replace(f'guild_id = "{STANDIN_GID}"', "guild_id = []"),
    "address_empty": standin_toml().replace(f'"{STANDIN_ADDRESS}"', '""'),
}


@pytest.mark.parametrize("name", sorted(BROKEN))
@pytest.mark.parametrize("profile", ["kit", "asus-node"])
def test_a_record_that_is_there_but_broken_refuses_both_profiles(monkeypatch, tmp_path, fake_pack, name, profile):
    """Absent is a copy; present and unreadable, or present with values that do not validate, is a defect."""
    assert BROKEN[name] != standin_toml()  # each replace hit
    standin_record(monkeypatch, tmp_path, BROKEN[name])
    _sync_refused(kit_cfg(**{"discord.profile": profile}), r"cannot read the asus-node fixture")


def test_a_record_that_cannot_be_read_refuses(monkeypatch, tmp_path, fake_pack):
    standin_record(monkeypatch, tmp_path, standin_toml())
    monkeypatch.setattr(sync, "ASUS_NODE_FIXTURE", tmp_path)  # a directory: IsADirectoryError, not absence
    _sync_refused(kit_cfg(), r"cannot read the asus-node fixture")


@pytest.mark.parametrize("gone", kit_instance.RECORD)
@pytest.mark.parametrize("profile", ["kit", "asus-node"])
def test_part_of_a_record_refuses_both_profiles(monkeypatch, tmp_path, fake_pack, gone, profile):
    """A live tree that lost one record path (a deleted or renamed fixture, a broken checkout) is damaged, never a
    copy: a kit profile naming the live guild would otherwise pass with the guild check off."""
    standin_record(monkeypatch, tmp_path, standin_toml(), drop=(gone,))
    _sync_refused(kit_cfg(**{"discord.profile": profile, "discord.guild_id": STANDIN_GID}),
                  rf"part of the asus-node instance record is missing: {re.escape(gone)}")
    post = tmp_path / "hello.md"
    post.write_text("hello")
    api = FakeGuild()
    with pytest.raises(SystemExit, match="part of the asus-node instance record is missing"):
        sync.cmd_post(api, GID, "announcements", str(post), True, kit_cfg(**{"discord.profile": profile}))
    assert api.calls == []


def test_a_broken_symlink_fixture_is_a_damaged_record_not_a_copy(monkeypatch, tmp_path, fake_pack):
    fixture = standin_record(monkeypatch, tmp_path, standin_toml())
    fixture.unlink()
    fixture.symlink_to(tmp_path / "nowhere.toml")
    _sync_refused(kit_cfg(**{"discord.guild_id": STANDIN_GID}), r"cannot read the asus-node fixture")


def test_sync_reads_the_record_where_the_manifest_puts_it():
    """ASUS_NODE_FIXTURE is a kit/instance.py RECORD path and a kit/INSTANCE-PATHS line: renamed on one side only,
    the guard would read a fixture the predicate does not know (or none)."""
    rel = sync.ASUS_NODE_FIXTURE.relative_to(sync.REPO).as_posix()
    assert rel in kit_instance.RECORD and rel in kit_instance.entries()
    assert sync.INSTANCE_REPO == sync.REPO == kit_instance.REPO


def test_the_address_stop_ignores_case(monkeypatch, tmp_path):
    inst = standin_instance(monkeypatch, tmp_path)
    post = tmp_path / "p.md"
    post.write_text(f"join us at {inst.address.upper()}")
    with pytest.raises(SystemExit, match="asus-node server's address"):
        sync.cmd_post(FakeGuild(), GID, "announcements", str(post), True, kit_cfg())


@needs_record
def test_the_kit_setup_keeps_every_placeholder_of_the_asus_node_one():
    """discord/content/kit/setup.md is the kit's #setup (gap #15: the Modrinth App's v0.21.9 path, its unknown-pack
    warning); asus-node's setup.md stays as posted. Both must carry the same secret, config, attachment and channel
    placeholders, so the kit's can never lose the share link, the pack or the address."""
    def tokens(text):
        return sorted(set(re.findall(r"\{(?:secret|config|attach):[^}]*\}|\{#[a-z0-9_-]+\}", text)))
    content = sync.HERE / "content"
    kit, asus = (content / "kit" / "setup.md").read_text(), (content / "setup.md").read_text()
    # plus the pack's name: a kit instance may run a custom pack (docs/kit/CUSTOM-PACK.md), asus-node never does
    assert tokens(kit) == sorted({*tokens(asus), "{config:pack_name}"}) and "{secret:share_link}" in tokens(kit)
    assert "Vanilla Extended" not in kit and "Sodium" not in kit


def test_the_pack_name_is_the_pack_this_instance_builds(tmp_path, monkeypatch):
    """{config:pack_name}: pack/mods.toml's [pack] name, or a custom pack's (pack.mode = "custom", pack-custom/), the
    same name servers.dat puts in the friends' server list. A custom instance without its pack refuses."""
    assert sync.CONFIG["pack_name"](CFG) == "Vanilla Extended"
    custom = cfg_with(**{"pack.mode": "custom"})
    monkeypatch.setattr(sync, "REPO", tmp_path)
    with pytest.raises(ValueError, match="pack-custom/mods.toml is missing"):
        sync.CONFIG["pack_name"](custom)
    (tmp_path / "pack-custom").mkdir()
    (tmp_path / "pack-custom" / "mods.toml").write_text(
        '[pack]\nname = "Sky Isles"\nversion = "1.2.0"\nminecraft = "26.1.2"\nfabric_loader = "0.19.5"\n')
    assert sync.CONFIG["pack_name"](custom) == "Sky Isles"


def test_shared_content_names_no_instance():
    """KIT_SHARED_CONTENT goes to every guild: it may hold no instance's address, host or story."""
    for name in sync.KIT_SHARED_CONTENT:
        text = (sync.HERE / "content" / name).read_text()
        assert ".ts.net" not in text and "asus-node" not in text and "Mike" not in text
        assert CFG["tailscale"]["server_address"] not in text


def _load_sync_copy(tmp_path, text, name):
    import importlib.util
    path = tmp_path / f"{name}.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE, mod.REPO = sync.HERE, sync.REPO  # the copy lives in tmp_path: point it back at this checkout
    mod.ASUS_NODE_FIXTURE, mod.ASUS_NODE_POSTS = sync.ASUS_NODE_FIXTURE, sync.ASUS_NODE_POSTS  # or the stand-in's
    mod.INSTANCE_REPO = sync.INSTANCE_REPO
    mod.DOTENV, mod.REDACT, mod.KIT_TOML = sync.DOTENV, {}, EXAMPLE
    mod.resolve_attachment = lambda name: PACK
    return mod


SOURCE = (sync.HERE / "sync.py").read_text()
GUARD_MUTANTS = {
    "asus_node_profile_on_another_guild": ("if gid != facts[0]:", "if False:"),
    "kit_profile_on_the_asus_node_guild": ("if gid == asus_gid:", "if False:"),
    "asus_node_story_in_a_kit_layout": ('if ch["content"] and asus_node_only(HERE / "content" / ch["content"]):',
                                        "if False:"),
    "asus_node_setup_in_a_kit_layout": ('return not (rel.startswith("kit/") or rel in KIT_SHARED_CONTENT)',
                                        'return rel == "story.md"'),
    "asus_node_address_in_a_kit_toml": ("if asus_address and any(asus_address.lower() in t.lower() for t in texts):",
                                        "if False:"),
}


@pytest.mark.parametrize("name", sorted(GUARD_MUTANTS))
def test_each_p6_stop_catches_its_mutant(monkeypatch, tmp_path, instance, name):
    guard, mutant = GUARD_MUTANTS[name]
    assert SOURCE.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    mod = _load_sync_copy(tmp_path, SOURCE.replace(guard, mutant), f"sync_mut_{name}")
    cfg, setup, _ = GUARD_CASES[name]
    cfg = cfg(instance)
    if setup:
        monkeypatch.setattr(sync, "PROFILES", mod.PROFILES)  # the setup patches sync.PROFILES: aim it at the copy
        setup(monkeypatch)
    api = FakeGuild(layout=sync.load_layout(_layout_text(), sync.kit_bots(cfg["discord"])))  # every channel there
    mod.cmd_sync(api, GID, True, _env(), TODAY, cfg=cfg)  # the mutant lets the sync through
    assert api.writes()


ABSENT_GUARD = "        if facts is None:\n            raise ValueError("


def test_the_absent_record_stop_catches_its_mutant(monkeypatch, tmp_path, fake_pack):
    """Without the refusal, the asus-node profile would run on any guild where its record is missing (the stand-in's
    story is still there, so the mutant syncs it)."""
    assert SOURCE.count(ABSENT_GUARD) == 1
    standin_instance(monkeypatch, tmp_path)
    _without_the_record(monkeypatch, tmp_path)
    mod = _load_sync_copy(tmp_path, SOURCE.replace(ABSENT_GUARD, "        if facts is None:\n            return\n"
                                                                 "            raise ValueError("), "sync_mut_absent")
    api = FakeGuild(layout=LAYOUT)
    mod.cmd_sync(api, GID, True, _env(), TODAY, cfg=cfg_with(**{"discord.profile": "asus-node",
                                                                "discord.guild_id": KIT_GID}))
    assert api.writes()


POST_GUARD = 'if cfg["discord"]["profile"] != ASUS_NODE and asus_node_only(file):'


def test_the_post_stop_catches_its_mutant(tmp_path, instance):
    assert SOURCE.count(POST_GUARD) == 1
    mod = _load_sync_copy(tmp_path, SOURCE.replace(POST_GUARD, "if False:"), "sync_mut_post")
    api = FakeGuild()
    mod.cmd_post(api, GID, "announcements", str(instance.story), True, kit_cfg())
    assert api.writes()  # the mutant posts the instance's story into a kit guild


# ── asus-node's sync output is unchanged by the kit (the byte gate on the plan) ──────────────────────────────────

def _golden_content_dir(tmp_path):
    """discord/ as it was before the kit: the layout, story.md, and welcome.md / setup.md as posted (the goldens)."""
    d = tmp_path / "golden-discord"
    (d / "content").mkdir(parents=True)
    (d / "layout.toml").write_text(_layout_text())
    (d / "content" / "story.md").write_text((sync.HERE / "content" / "story.md").read_text())
    for name in ("welcome.md", "setup.md"):
        (d / "content" / name).write_text((GOLDEN / name).read_text())
    return d


@needs_record
def test_the_asus_node_guild_as_posted_before_the_kit_needs_0_actions(monkeypatch, tmp_path, fake_pack, capsys):
    """Post the pre-kit content (the goldens) into a guild, as that guild holds it today; then the kit's sync, dry
    run, against the asus-node fixture: it must plan nothing. Any byte the kit changed in asus-node's messages, or a
    channel it touched, would show as an action."""
    cfg = asus_cfg()
    api = FakeGuild(layout=sync.load_layout(_layout_text(), sync.kit_bots(cfg["discord"])))
    real_here = sync.HERE
    monkeypatch.setattr(sync, "HERE", _golden_content_dir(tmp_path))
    assert sync.cmd_sync(api, GID, True, _env(), TODAY, cfg=cfg) == 0
    monkeypatch.setattr(sync, "HERE", real_here)
    before = len(api.writes())
    capsys.readouterr()
    assert sync.cmd_sync(api, GID, False, _env(), TODAY, cfg=cfg) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("0 actions") and len(api.writes()) == before
