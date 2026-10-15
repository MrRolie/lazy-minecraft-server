"""Pure-planner tests for discord/sync.py. Run: uv run --with pytest pytest discord/"""
import datetime
import json
import re
import subprocess
from pathlib import Path

import pytest

import sync

GID, BOT = "100", "200"
LAYOUT = sync.load_layout((Path(__file__).parent / "layout.toml").read_text())
# Built from two literals so this file never itself matches a share link (test_no_tracked_file_...).
INVITE = "https://login.tailscale.com/admin/invite/"
FAKE_LINK = INVITE + "FakeTestCode0123"
TODAY = datetime.date(2026, 10, 2)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No test reads the real discord/.env (live tokens), and no resolved secret leaks across tests."""
    monkeypatch.setattr(sync, "DOTENV", tmp_path / "absent.env")
    monkeypatch.setattr(sync, "REDACT", {})


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


def test_pack_filename_from_compose():
    compose = 'services:\n  minecraft:\n    environment:\n      MODRINTH_MODPACK: "/pack/ve-2.0.0-ch3.mrpack"\n'
    assert sync.pack_filename(compose) == "ve-2.0.0-ch3.mrpack"
    with pytest.raises(ValueError):
        sync.pack_filename("services: {}\n")
    # the shipped compose names the file the server runs
    real = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    assert sync.pack_filename(real).endswith(".mrpack")


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


def test_shipped_setup_attaches_the_pack():
    text = (Path(__file__).parent / "content" / "setup.md").read_text()
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


def test_shipped_content_fits_and_links_resolve():
    names = {ch["name"]: "1" for c in LAYOUT["categories"] for ch in c["channels"]}
    for cat in LAYOUT["categories"]:
        for ch in cat["channels"]:
            if ch["content"]:
                text = (Path(__file__).parent / "content" / ch["content"]).read_text()
                # filled with a longer-than-real link, as sync posts it
                filled = sync.fill_secrets(text, {"share_link": FAKE_LINK + "X" * 40})
                assert "{secret:" not in filled
                assert sync.split_parts(sync.resolve_links(filled, names))


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
    layout = sync.load_layout((Path(__file__).parent / "layout.toml").read_text())
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

    def __init__(self, everyone_perms="0"):  # None: the guild payload carries no roles
        self.everyone_perms = everyone_perms
        self.channels = converged_state(LAYOUT)
        self.sys_id = next(c["id"] for c in self.channels if c["name"] == "general")
        self.msgs, self.calls, self.nid = {}, [], 5000

    def __call__(self, method, path, body=None, file=None):
        self.calls.append((method, path, body))
        if path == "/users/@me":
            return {"id": BOT}
        if path == f"/guilds/{GID}/channels":
            return self.channels
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
    (tmp_path / "content").mkdir()
    (tmp_path / "layout.toml").write_text(
        '[[category]]\nname = "START HERE"\n  [[category.channel]]\n  name = "setup"\n  content = "setup.md"\n')
    (tmp_path / "content" / "setup.md").write_text(body)
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
    dotenv.write_text(f"DISCORD_TOKEN=fake\nDISCORD_GUILD_ID={GID}\nTAILSCALE_SHARE_LINK={INVITE}x/y\n")
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


def test_shipped_setup_gives_the_share_link_once():
    text = (Path(__file__).parent / "content" / "setup.md").read_text()
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

def test_missing_link_refusal_gives_the_safe_append():
    with pytest.raises(ValueError) as e:
        sync.resolve_secret("share_link", _env(link=None), TODAY)
    assert "printf '\\nTAILSCALE_SHARE_LINK=<link>" in str(e.value)  # leading newline: see the glued test


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


@pytest.mark.parametrize("perms,expect", [
    (str(sync.CREATE_INVITE | sync.VIEW), INVITE_WARNING),
    (str(sync.VIEW | sync.SEND), None),
    (None, "whether members can create invites is unverified"),  # absent is reported, not read as safe
])
@pytest.mark.parametrize("apply", [False, True])
def test_sync_warns_once_when_everyone_can_create_invites(fake_pack, capsys, perms, expect, apply):
    sync.cmd_sync(FakeGuild(everyone_perms=perms), GID, apply, _env(), TODAY)
    out = capsys.readouterr().out
    if expect:
        assert out.count(expect) == 1  # once per run, not once per apply pass
    else:
        assert "invites" not in out
    assert "FakeTestCode0123" not in out


def test_invite_check_reads_only_the_everyone_role():
    guild = {"roles": [{"id": "999", "permissions": str(sync.CREATE_INVITE)}, {"id": GID, "permissions": "0"}]}
    assert sync.invite_exposure(guild, GID) is None
    guild["roles"][1]["permissions"] = str(sync.CREATE_INVITE)
    assert sync.invite_exposure(guild, GID).startswith(INVITE_WARNING)


def test_no_invite_check_without_the_share_link(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync, "HERE", _layout_dir(tmp_path, "no secrets here"))
    sync.cmd_sync(FakeGuild(everyone_perms=str(sync.CREATE_INVITE)), GID, False, {}, TODAY)
    assert "invites" not in capsys.readouterr().out
