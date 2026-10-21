"""discord/bootstrap.py against a fake Discord: two bot tokens, one guild. Run: uv run --with pytest pytest discord/"""
import copy
import datetime
import json
import re
import stat

import pytest

import bootstrap
import sync

EXAMPLE = sync.REPO / "kit" / "fixtures" / "example.toml"  # a made-up kit instance (profile "kit")
sync._kitconfig()  # puts kit/ on sys.path
import instance  # noqa: E402  kit/instance.py: does this tree hold the asus-node record (part of one raises)

NO_RECORD = instance.NO_RECORD


def standin_record(monkeypatch, root, fixture_text):
    """A tree holding a stand-in asus-node record (kit/instance.py RECORD) whose kit.toml is fixture_text."""
    for rel in instance.RECORD:
        p = root / rel
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("stand-in\n")
    fixture = root / sync.ASUS_NODE_FIXTURE.relative_to(sync.REPO)
    fixture.write_text(fixture_text)
    monkeypatch.setattr(sync, "INSTANCE_REPO", root)
    monkeypatch.setattr(sync, "ASUS_NODE_FIXTURE", fixture)
GID, ADMIN, BRIDGE = "1100000000000000001", "1200000000000000002", "1300000000000000003"
WELCOME, OPS = "1400000000000000004", "1400000000000000005"
HOOK_ID, HOOK_TOKEN = "1500000000000000006", "FakeHookToken_0123-abc"
URL = f"https://discord.com/api/webhooks/{HOOK_ID}/{HOOK_TOKEN}"
NOW = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Never the live discord/.env, .env.bridge, kit.toml or /etc/mc-host/webhook."""
    monkeypatch.setattr(sync, "DOTENV", tmp_path / "absent.env")
    monkeypatch.setattr(sync, "KIT_TOML", EXAMPLE)
    monkeypatch.setattr(sync, "REDACT", {})
    monkeypatch.setattr(bootstrap, "BRIDGE_DOTENV", tmp_path / "absent.env.bridge")
    monkeypatch.setattr(bootstrap, "INSTALLED", tmp_path / "etc-mc-host" / "webhook")
    monkeypatch.delenv("MC_KIT_TOML", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 1000)


def cfg(tmp_path, **discord):
    c = copy.deepcopy(sync.load_kit_config())
    c["discord"].update({"guild_id": GID, "profile": "kit", **discord})
    c["host"]["home"] = str(tmp_path / "home")
    return c


class Discord:
    """A recording fake of the REST paths bootstrap uses. Each token is one bot; failures raise sync.ApiError."""

    def __init__(self):
        self.bots = {
            "admin-token": {"user": {"id": ADMIN, "bot": True}, "guilds": [GID],
                            "app": {"id": ADMIN, "name": "MC Admin", "flags": 0}},
            "bridge-token": {"user": {"id": BRIDGE, "bot": True}, "guilds": [GID],
                             "app": {"id": BRIDGE, "name": "MC Relay", "flags": 1 << 23}},
        }
        self.channels = [{"id": WELCOME, "name": "welcome", "type": sync.TEXT},
                         {"id": OPS, "name": "ops-log", "type": sync.TEXT},
                         {"id": "1400000000000000009", "name": "🔥 Campfire", "type": sync.VOICE}]
        self.everyone = str(sync.VIEW)
        self.webhooks, self.invites, self.calls = [], [], []
        self.patch_error, self.patch_sticks = None, True
        self.nid = 1600000000000000000

    def api(self, token):
        return lambda method, path, body=None, file=None: self.call(token, method, path, body)

    def call(self, token, method, path, body):
        self.calls.append((token, method, path, body))
        bot = self.bots[token]
        if path == "/users/@me":
            return dict(bot["user"])
        if path == "/users/@me/guilds":
            return [{"id": g, "name": "x"} for g in bot["guilds"]]
        if path == "/applications/@me":
            if method == "PATCH":
                if self.patch_error:
                    raise sync.ApiError(f"PATCH /applications/@me -> HTTP {self.patch_error}: 'x'", self.patch_error)
                if self.patch_sticks:
                    bot["app"]["flags"] = body["flags"]
            return dict(bot["app"])
        if path == f"/guilds/{GID}":
            return {"id": GID, "roles": [{"id": GID, "permissions": self.everyone}]}
        if path == f"/guilds/{GID}/channels":
            return self.channels
        m = re.fullmatch(r"/channels/(\d+)/webhooks", path)
        if m and method == "GET":
            return [dict(w) for w in self.webhooks if w["channel_id"] == m.group(1)]
        if m and method == "POST":
            hook = {"id": HOOK_ID, "token": HOOK_TOKEN, "type": 1, "name": body["name"], "channel_id": m.group(1),
                    "user": {"id": bot["user"]["id"]}}
            self.webhooks.append(hook)
            return dict(hook)
        m = re.fullmatch(r"/webhooks/(\d+)/(.+)", path)
        if m:
            hook = next((w for w in self.webhooks if w["id"] == m.group(1) and w.get("token") == m.group(2)), None)
            if hook is None:
                raise sync.ApiError(sync.redact(f"GET {path} -> HTTP 404"), 404)
            return {k: v for k, v in hook.items() if k != "user"}
        m = re.fullmatch(r"/channels/(\d+)/invites", path)
        if m and method == "GET":
            return [dict(i) for i in self.invites if i["channel_id"] == m.group(1)]
        if m and method == "POST":
            self.nid += 1
            inv = {"code": f"fakeCode{self.nid}", "channel_id": m.group(1), "inviter": {"id": bot["user"]["id"]},
                   "max_age": body["max_age"], "max_uses": body["max_uses"],
                   "expires_at": (NOW + datetime.timedelta(seconds=body["max_age"])).isoformat(),
                   "guild": {"id": GID}}
            self.invites.append(inv)
            return dict(inv)
        m = re.fullmatch(r"/invites/(\w+)", path)
        if m:
            return next(dict(i) for i in self.invites if i["code"] == m.group(1))
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[1] != "GET"]


@pytest.fixture
def dc():
    return Discord()


def run_discover(dc, kit_path):
    return bootstrap.discover(dc.api("admin-token"), dc.api("bridge-token"), kit_path)


# ── discover ─────────────────────────────────────────────────────────────────

def test_discover_prints_the_two_lines_and_waits_for_kit_toml(dc, tmp_path, capsys):
    assert run_discover(dc, tmp_path / "kit.toml") == bootstrap.PENDING
    out = capsys.readouterr().out
    assert f'guild_id = "{GID}"\nbridge_bot_id = "{BRIDGE}"\n' in out and "does not exist yet" in out
    assert dc.writes() == []  # read-only


def test_discover_names_the_placeholders_still_to_set(dc, tmp_path, capsys):
    kit = tmp_path / "kit.toml"
    kit.write_text('[discord]\nguild_id = "000000000000000000"\nbridge_bot_id = "000000000000000000"\n')
    assert run_discover(dc, kit) == bootstrap.PENDING
    assert "set discord.guild_id, discord.bridge_bot_id in" in capsys.readouterr().out


def test_discover_passes_when_kit_toml_agrees(dc, tmp_path, capsys):
    kit = tmp_path / "kit.toml"
    kit.write_text(f'[discord]\nguild_id = "{GID}"\nbridge_bot_id = "{BRIDGE}"\n')
    assert run_discover(dc, kit) == 0
    assert "agrees" in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [("guild_id", "1100000000000000999"), ("bridge_bot_id", ADMIN)])
def test_discover_refuses_a_kit_toml_that_names_other_ids(dc, tmp_path, key, value):
    kit = tmp_path / "kit.toml"
    kit.write_text(f'[discord]\nguild_id = "{GID}"\nbridge_bot_id = "{BRIDGE}"\n'.replace(
        {"guild_id": GID, "bridge_bot_id": BRIDGE}[key], value))
    with pytest.raises(SystemExit, match=f"discord.{key} = .*but the bots say"):
        run_discover(dc, kit)


def test_discover_refuses_one_bot_in_both_files(dc, tmp_path):
    dc.bots["bridge-token"]["user"]["id"] = ADMIN
    with pytest.raises(SystemExit, match="the same bot"):
        run_discover(dc, tmp_path / "kit.toml")


@pytest.mark.parametrize("who", ["admin-token", "bridge-token"])
@pytest.mark.parametrize("flag", [False, None])
def test_discover_refuses_a_token_that_is_not_a_bot(dc, tmp_path, who, flag):
    if flag is None:
        del dc.bots[who]["user"]["bot"]  # absent is not a yes
    else:
        dc.bots[who]["user"]["bot"] = flag
    with pytest.raises(SystemExit, match="bot tokens only, never a user's"):
        run_discover(dc, tmp_path / "kit.toml")


def test_discover_refuses_an_admin_bot_in_two_guilds(dc, tmp_path):
    dc.bots["admin-token"]["guilds"].append("1100000000000000777")
    with pytest.raises(SystemExit, match="in 2 guilds"):
        run_discover(dc, tmp_path / "kit.toml")


@pytest.mark.parametrize("bridge_guilds", [["1100000000000000777"], [GID, "1100000000000000777"]])
def test_discover_refuses_a_bridge_bot_outside_the_admin_guild(dc, tmp_path, bridge_guilds):
    dc.bots["bridge-token"]["guilds"] = bridge_guilds
    with pytest.raises(SystemExit, match="not only the admin bot's guild"):
        run_discover(dc, tmp_path / "kit.toml")


def test_discover_prints_the_admin_invite_when_the_admin_bot_is_in_no_guild(dc, tmp_path, capsys):
    dc.bots["admin-token"]["guilds"] = []
    assert run_discover(dc, tmp_path / "kit.toml") == bootstrap.PENDING
    out = capsys.readouterr().out
    assert out.startswith("HUMAN STEP: add the admin bot")
    assert f"client_id={ADMIN}&scope=bot%20applications.commands&permissions=8\n" in out
    assert "guild_id=" not in out


def test_discover_prints_the_bridge_invite_pinned_to_the_guild(dc, tmp_path, capsys):
    dc.bots["bridge-token"]["guilds"] = []
    assert run_discover(dc, tmp_path / "kit.toml") == bootstrap.PENDING
    out = capsys.readouterr().out
    assert (f"client_id={BRIDGE}&scope=bot%20applications.commands&permissions=2147568640"
            f"&guild_id={GID}&disable_guild_select=true") in out


# ── intents ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bit", [1 << 18, 1 << 19])
def test_intents_unchanged_when_message_content_is_on(dc, capsys, bit):
    dc.bots["bridge-token"]["app"]["flags"] |= bit
    assert bootstrap.intents(dc.api("bridge-token"), True) == 0
    assert dc.writes() == [] and "unchanged" in capsys.readouterr().out


def test_intents_dry_run_patches_nothing(dc):
    assert bootstrap.intents(dc.api("bridge-token"), False) == bootstrap.PENDING
    assert dc.writes() == []


def test_intents_patches_the_limited_flag_keeping_the_others_and_reads_it_back(dc, capsys):
    assert bootstrap.intents(dc.api("bridge-token"), True) == 0
    assert dc.writes() == [("bridge-token", "PATCH", "/applications/@me", {"flags": (1 << 23) | (1 << 19)})]
    assert dc.calls[-1][1:3] == ("GET", "/applications/@me")  # the read-back is the gate
    assert "read back" in capsys.readouterr().out
    assert all(tok == "bridge-token" for tok, *_ in dc.calls)  # the bridge app, never the admin's


def test_intents_falls_back_to_the_portal_when_the_patch_is_refused(dc, capsys):
    dc.patch_error = 403
    assert bootstrap.intents(dc.api("bridge-token"), True) == bootstrap.PENDING
    out = capsys.readouterr().out
    assert "HUMAN STEP:" in out and "MC Relay → Bot → Privileged Gateway Intents" in out


def test_intents_falls_back_when_the_patch_does_not_stick(dc, capsys):
    dc.patch_sticks = False
    assert bootstrap.intents(dc.api("bridge-token"), True) == bootstrap.PENDING
    assert "HUMAN STEP:" in capsys.readouterr().out


# ── webhook ──────────────────────────────────────────────────────────────────

def staged(tmp_path):
    return tmp_path / "home" / ".config" / "mc-kit" / "webhook"


def test_webhook_needs_the_synced_channel(dc, tmp_path):
    dc.channels = [c for c in dc.channels if c["name"] != "ops-log"]
    with pytest.raises(SystemExit, match="sync.py --apply first"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert dc.writes() == []


def test_webhook_dry_run_creates_and_stages_nothing(dc, tmp_path):
    assert bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, False) == bootstrap.PENDING
    assert dc.writes() == [] and not staged(tmp_path).exists()


def test_webhook_dry_run_over_an_existing_webhook_stages_nothing(dc, tmp_path, capsys):
    dc.webhooks.append({"id": HOOK_ID, "token": HOOK_TOKEN, "name": "mc-alert", "channel_id": OPS,
                        "user": {"id": ADMIN}})
    assert bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, False) == bootstrap.PENDING
    assert dc.writes() == [] and not staged(tmp_path).exists()
    assert "~ stage the webhook URL" in capsys.readouterr().out


def test_webhook_creates_verifies_stages_then_is_unchanged(dc, tmp_path, capsys):
    c = cfg(tmp_path)
    assert bootstrap.webhook(dc.api("admin-token"), c, GID, True) == 0
    assert [w[1:] for w in dc.writes()] == [("POST", f"/channels/{OPS}/webhooks", {"name": "mc-alert"})]
    assert ("admin-token", "GET", f"/webhooks/{HOOK_ID}/{HOOK_TOKEN}", None) in dc.calls  # verified, not posted to
    f = staged(tmp_path)
    assert f.read_text() == URL + "\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    out = capsys.readouterr().out
    assert HOOK_TOKEN not in out and "sudo bash host/setup.sh" in out
    dc.calls.clear()
    assert bootstrap.webhook(dc.api("admin-token"), c, GID, True) == 0  # a second run: no second webhook
    assert dc.writes() == [] and "unchanged: staged" in capsys.readouterr().out


def test_webhook_already_installed_is_left_alone(dc, tmp_path, capsys):
    bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    bootstrap.INSTALLED.parent.mkdir()
    staged(tmp_path).rename(bootstrap.INSTALLED)  # what setup.sh step 7 does
    capsys.readouterr()
    assert bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True) == 0
    assert f"unchanged: {bootstrap.INSTALLED} holds it" in capsys.readouterr().out
    assert not staged(tmp_path).exists()


def test_webhook_restages_when_the_installed_one_differs(dc, tmp_path):
    bootstrap.INSTALLED.parent.mkdir()
    bootstrap.INSTALLED.write_text(f"https://discord.com/api/webhooks/{HOOK_ID}/OldToken\n")
    assert bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True) == 0
    assert staged(tmp_path).read_text() == URL + "\n"


def test_webhook_reuses_its_own_and_ignores_anothers(dc, tmp_path):
    dc.webhooks.append({"id": "1500000000000000999", "token": "Other", "name": "mc-alert", "channel_id": OPS,
                        "user": {"id": "1900000000000000000"}})  # a person's webhook with the same name
    bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert len([w for w in dc.writes() if w[1] == "POST"]) == 1
    assert staged(tmp_path).read_text() == URL + "\n"


def test_webhook_without_a_token_in_the_listing_is_refused_never_duplicated(dc, tmp_path):
    dc.webhooks.append({"id": HOOK_ID, "name": "mc-alert", "channel_id": OPS, "user": {"id": ADMIN}})
    with pytest.raises(SystemExit, match="without its token"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert dc.writes() == []


def test_two_own_webhooks_are_refused(dc, tmp_path):
    for i in "12":
        dc.webhooks.append({"id": "15000000000000000" + i + "0", "token": "t" + i, "name": "mc-alert",
                            "channel_id": OPS, "user": {"id": ADMIN}})
    with pytest.raises(SystemExit, match="2 mc-alert webhooks"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)


def test_webhook_refuses_a_symlinked_stage(dc, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x")
    staged(tmp_path).parent.mkdir(parents=True)
    staged(tmp_path).symlink_to(target)
    with pytest.raises(SystemExit, match="symlink"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert target.read_text() == "x"


def test_webhook_refuses_a_symlinked_stage_directory(dc, tmp_path):
    """A linked ~/.config/mc-kit: the file itself is no link, so only stage()'s directory check stands between the
    URL and wherever the link points (write_atomic would mkdir(exist_ok) and os.replace straight through it)."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    staged(tmp_path).parent.parent.mkdir(parents=True)
    staged(tmp_path).parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(SystemExit, match="symlink"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert list(target.iterdir()) == []


def test_stage_refuses_a_symlinked_file(tmp_path):
    """stage()'s own file check, without read_plain() in front of it (a link made between the read and the write)."""
    target = tmp_path / "elsewhere"
    target.write_text("x")
    path = tmp_path / "home" / "webhook"
    path.parent.mkdir()
    path.symlink_to(target)
    with pytest.raises(SystemExit, match="symlink"):
        bootstrap.stage(path, URL)
    assert target.read_text() == "x"


def test_read_plain_refuses_a_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text(URL + "\n")
    link = tmp_path / "webhook"
    link.symlink_to(target)
    with pytest.raises(SystemExit, match="symlink"):
        bootstrap.read_plain(link)  # never "unchanged" on the strength of a file somewhere else


def test_webhook_refuses_root(dc, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="not root"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert dc.calls == []


def test_webhook_whose_url_answers_for_another_channel_is_refused(dc, tmp_path):
    dc.webhooks.append({"id": HOOK_ID, "token": HOOK_TOKEN, "name": "mc-alert", "channel_id": OPS,
                        "user": {"id": ADMIN}})
    real = dc.call

    def moved(token, method, path, body):
        out = real(token, method, path, body)
        return {**out, "channel_id": WELCOME} if path.startswith("/webhooks/") else out
    dc.call = moved
    with pytest.raises(SystemExit, match="different webhook or channel"):
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert not staged(tmp_path).exists()


def test_a_failing_webhook_check_never_echoes_the_token(dc, tmp_path, monkeypatch):
    import urllib.request
    import io
    import urllib.error
    dc.webhooks.append({"id": HOOK_ID, "token": HOOK_TOKEN, "name": "mc-alert", "channel_id": OPS,
                        "user": {"id": ADMIN}})
    real = dc.call

    def via_http(token, method, path, body):
        if path.startswith("/webhooks/"):  # the real client, bounced with the URL quoted back
            def bounce(req, timeout=0):
                raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(json.dumps({"url": URL}).encode()))
            monkeypatch.setattr(urllib.request, "urlopen", bounce)
            return sync.Api("admin-token")(method, path, body)
        return real(token, method, path, body)
    dc.call = via_http
    with pytest.raises(SystemExit) as e:
        bootstrap.webhook(dc.api("admin-token"), cfg(tmp_path), GID, True)
    assert HOOK_TOKEN not in str(e.value.code) and "<webhook" in str(e.value.code)


def test_the_webhook_name_is_one_discord_accepts():
    assert not re.search("clyde|discord", bootstrap.WEBHOOK_NAME, re.I) and len(bootstrap.WEBHOOK_NAME) <= 80


# ── invite ───────────────────────────────────────────────────────────────────

def test_invite_waits_for_the_lockdown(dc, tmp_path):
    dc.everyone = str(sync.VIEW | sync.CREATE_INVITE)
    with pytest.raises(SystemExit, match="sync.py --apply first"):
        bootstrap.invite(dc.api("admin-token"), cfg(tmp_path), GID, True, NOW)
    assert dc.writes() == []


def test_invite_without_lockdown_does_not_check_it(dc, tmp_path):
    dc.everyone = str(sync.VIEW | sync.CREATE_INVITE)
    assert bootstrap.invite(dc.api("admin-token"), cfg(tmp_path, invite_lockdown=False), GID, True, NOW) == 0


def test_invite_dry_run_then_apply_into_welcome(dc, tmp_path, capsys):
    assert bootstrap.invite(dc.api("admin-token"), cfg(tmp_path), GID, False, NOW) == bootstrap.PENDING
    assert dc.writes() == []
    assert bootstrap.invite(dc.api("admin-token"), cfg(tmp_path), GID, True, NOW) == 0
    assert [w[1:] for w in dc.writes()] == [
        ("POST", f"/channels/{WELCOME}/invites", {"max_age": 604800, "max_uses": 0, "unique": True})]
    code = dc.invites[0]["code"]
    assert ("admin-token", "GET", f"/invites/{code}", None) in dc.calls  # the P12 gate
    assert f"https://discord.gg/{code}  (expires 2026-10-21 12:00 UTC)" in capsys.readouterr().out


@pytest.mark.parametrize("left,inviter,uses,reused", [
    (datetime.timedelta(days=3), ADMIN, 0, True),
    (datetime.timedelta(hours=12), ADMIN, 0, False),            # under a day left: a fresh one
    (datetime.timedelta(days=3), "1900000000000000000", 0, False),  # someone else's
    (datetime.timedelta(days=3), ADMIN, 5, False),              # a capped one may run out
    (None, ADMIN, 0, True),                                     # never expires
])
def test_invite_reuses_only_a_lasting_bot_made_one(dc, tmp_path, left, inviter, uses, reused):
    dc.invites.append({"code": "existing", "channel_id": WELCOME, "inviter": {"id": inviter}, "max_uses": uses,
                       "max_age": 0 if left is None else 604800, "guild": {"id": GID},
                       "expires_at": None if left is None else (NOW + left).isoformat()})
    assert bootstrap.invite(dc.api("admin-token"), cfg(tmp_path), GID, True, NOW) == 0
    assert (dc.writes() == []) is reused


def test_an_invite_listing_with_no_expiry_facts_is_not_reused(dc, tmp_path):
    dc.invites.append({"code": "existing", "channel_id": WELCOME, "inviter": {"id": ADMIN}, "max_uses": 0,
                       "guild": {"id": GID}})
    bootstrap.invite(dc.api("admin-token"), cfg(tmp_path), GID, True, NOW)
    assert len(dc.writes()) == 1


def test_an_invite_into_another_guild_is_refused(dc, tmp_path):
    dc.invites.append({"code": "existing", "channel_id": WELCOME, "inviter": {"id": ADMIN}, "max_uses": 0,
                       "max_age": 0, "expires_at": None, "guild": {"id": "1100000000000000777"}})
    with pytest.raises(SystemExit, match="does not resolve to guild"):
        bootstrap.invite(dc.api("admin-token"), cfg(tmp_path), GID, True, NOW)


# ── clean ────────────────────────────────────────────────────────────────────

OWNER = "1700000000000000007"


def made_after_guild(ms, n=0):
    """A snowflake made `ms` milliseconds after the guild GID (its top 42 bits are a millisecond time)."""
    return str((((int(GID) >> 22) + ms) << 22) + n)


TEMPLATE = [  # Discord's "For me and my friends", as a French client names it: no name of it is ever matched
    {"id": made_after_guild(0, 1), "name": "Salons textuels", "type": sync.CATEGORY, "position": 0},
    {"id": made_after_guild(0, 2), "name": "général", "type": sync.TEXT, "position": 0,
     "parent_id": made_after_guild(0, 1)},
    {"id": made_after_guild(1, 0), "name": "Salons vocaux", "type": sync.CATEGORY, "position": 1},
    {"id": made_after_guild(1, 1), "name": "Général", "type": sync.VOICE, "position": 0,
     "parent_id": made_after_guild(1, 0)},
]
TEMPLATE_GENERAL = TEMPLATE[1]["id"]


def _join(uid):
    return {"id": str(int(uid) + 1), "type": bootstrap.USER_JOIN, "author": {"id": uid}, "content": ""}


class Guild:
    """A recording fake of a fresh kit guild for clean: the template's channels (plus any already synced), the
    owner and the two bots as members, and the two bots' member-join messages in the template's #general."""

    def __init__(self, synced=False):
        self.channels = copy.deepcopy(TEMPLATE)
        if synced:
            self.channels += self.layout_channels()
        self.members, self.count = {OWNER, ADMIN, BRIDGE}, None  # None: the members' own number; False: absent
        self.msgs = {TEMPLATE_GENERAL: [_join(ADMIN), _join(BRIDGE)]}
        self.admin_role, self.admin_perms, self.deletes_stick = "1900000000000000001", bootstrap.ADMINISTRATOR, True
        self.member_error, self.calls = None, []
        self.active_threads, self.archived = [], {"public": {}, "private": {}}  # archived: {channel id: [threads]}

    @staticmethod
    def layout_channels():
        layout = sync.load_layout((sync.HERE / "layout.toml").read_text(), {"bridge": BRIDGE}, "kit")
        chans, nid = [], 2000000000000000000
        for ci, cat in enumerate(layout["categories"]):
            nid += 1
            cid = str(nid)
            chans.append({"id": cid, "name": cat["name"], "type": sync.CATEGORY, "position": ci})
            for pi, ch in enumerate(cat["channels"]):
                nid += 1
                chans.append({"id": str(nid), "name": ch["name"], "type": sync._type(ch), "position": pi,
                              "parent_id": cid})
        return chans

    def __call__(self, method, path, body=None, file=None):
        self.calls.append((method, path))
        if path == "/users/@me":
            return {"id": ADMIN, "bot": True}
        if path == f"/guilds/{GID}/channels":
            return copy.deepcopy(self.channels)
        if path == f"/guilds/{GID}?with_counts=true":
            guild = {"id": GID, "owner_id": OWNER, "approximate_member_count": self.count or len(self.members),
                     "roles": [{"id": GID, "permissions": str(sync.VIEW)},
                               {"id": self.admin_role, "permissions": str(self.admin_perms)}]}
            if self.count is False:  # absent is not a yes
                del guild["approximate_member_count"]
            return guild
        m = re.fullmatch(rf"/guilds/{GID}/members/(\d+)", path)
        if m:
            if self.member_error:
                raise sync.ApiError(f"GET {path} -> HTTP {self.member_error}", self.member_error)
            if m.group(1) not in self.members:
                raise sync.ApiError(f"GET {path} -> HTTP 404: Unknown Member", 404)
            return {"user": {"id": m.group(1)}, "roles": [self.admin_role] if m.group(1) == ADMIN else []}
        m = re.fullmatch(r"/channels/(\d+)/messages\?limit=100", path)
        if m and method == "GET":
            return list(reversed(self.msgs.get(m.group(1), [])))
        if path == f"/guilds/{GID}/threads/active" and method == "GET":
            return {"threads": copy.deepcopy(self.active_threads), "members": []}
        m = re.fullmatch(r"/channels/(\d+)/threads/archived/(public|private)", path)
        if m and method == "GET":
            chan = next(c for c in self.channels if c["id"] == m.group(1))
            assert chan["type"] == sync.TEXT, "threads are read on text channels only"
            return {"threads": self.archived[m.group(2)].get(m.group(1), []), "members": [], "has_more": False}
        m = re.fullmatch(r"/channels/(\d+)", path)
        if m and method == "DELETE":
            gone = next(c for c in self.channels if c["id"] == m.group(1))
            if self.deletes_stick:
                self.channels.remove(gone)
            return gone
        raise AssertionError(f"unexpected {method} {path}")

    def deletes(self):
        return [p for meth, p in self.calls if meth == "DELETE"]


def run_clean(guild, tmp_path, apply, **discord):
    return bootstrap.clean(guild, cfg(tmp_path, bridge_bot_id=BRIDGE, **discord), GID, apply)


def refused(guild, tmp_path, match, **discord):
    with pytest.raises(SystemExit) as e:
        run_clean(guild, tmp_path, True, **discord)
    assert re.search(match, str(e.value.code)), e.value.code
    assert guild.deletes() == []


def test_clean_refuses_the_asus_node_profile_before_any_call(tmp_path):
    guild = Guild()
    refused(guild, tmp_path, 'only with discord.profile = "kit"', profile="asus-node")
    assert guild.calls == []


@pytest.mark.parametrize("record", ["standin", "asus-node"])
def test_clean_refuses_the_asus_node_guild_before_any_call(monkeypatch, tmp_path, record):
    """Against asus-node's own record, and against a stand-in's (every copy has that one)."""
    if record == "standin":
        standin_record(monkeypatch, tmp_path / "standin-repo",
                       re.sub(r'guild_id = "\d+"', 'guild_id = "1700000000000000071"', EXAMPLE.read_text()))
    elif not instance.record():  # part of a record raises: a damaged live tree fails here, never skips
        pytest.skip(NO_RECORD)
    guild = Guild()
    asus_gid = sync.asus_node_facts()[0]
    assert asus_gid != GID
    with pytest.raises(SystemExit, match="a kit never syncs that guild"):
        bootstrap.clean(guild, cfg(tmp_path, bridge_bot_id=BRIDGE, guild_id=asus_gid), asus_gid, True)
    assert guild.calls == []


def test_clean_runs_without_the_asus_node_record(monkeypatch, tmp_path):
    """A copy without kit/INSTANCE-PATHS: no asus-node guild to protect, so a kit's clean goes ahead."""
    monkeypatch.setattr(sync, "INSTANCE_REPO", tmp_path)  # a tree with none of the record
    monkeypatch.setattr(sync, "ASUS_NODE_FIXTURE", tmp_path / "kit" / "fixtures" / "asus-node.toml")
    guild = Guild(synced=True)
    assert run_clean(guild, tmp_path, False) == bootstrap.PENDING


def test_clean_dry_run_lists_the_template_and_deletes_nothing(tmp_path, capsys):
    guild = Guild(synced=True)
    assert run_clean(guild, tmp_path, False) == bootstrap.PENDING
    out = capsys.readouterr().out
    listed = re.findall(r"^- delete (\w+) '(.+)'$", out, re.M)
    assert listed == [("text", "général"), ("voice", "Général"), ("category", "Salons textuels"),
                      ("category", "Salons vocaux")]  # children before their categories
    kept = re.search(r"^keep \(layout\.toml lists them\): (.+)$", out, re.M).group(1).split(", ")
    assert kept == [c["name"] for c in Guild.layout_channels()] and "general" in kept and "the-story" not in kept
    assert guild.deletes() == []


def test_clean_apply_deletes_reads_back_and_a_second_run_is_a_no_op(tmp_path, capsys):
    guild = Guild()
    assert run_clean(guild, tmp_path, True) == 0
    assert guild.deletes() == [f"/channels/{c['id']}" for c in (TEMPLATE[1], TEMPLATE[3], TEMPLATE[0], TEMPLATE[2])]
    assert guild.channels == [] and guild.calls[-1] == ("GET", f"/guilds/{GID}/channels")  # the read back
    assert "read back: none of them is left" in capsys.readouterr().out
    guild.calls.clear()
    assert run_clean(guild, tmp_path, False) == 0
    assert "nothing outside layout.toml to delete" in capsys.readouterr().out
    assert guild.calls == [("GET", f"/guilds/{GID}/channels")]


def test_clean_with_nothing_to_delete_does_not_need_a_fresh_guild(tmp_path):
    """After the first sync friends join; a re-run still says, truthfully, that there is nothing to delete."""
    guild = Guild()
    guild.channels = Guild.layout_channels()
    guild.members.add("1700000000000000099")
    assert run_clean(guild, tmp_path, True) == 0


def test_clean_refuses_a_guild_with_another_member(tmp_path):
    guild = Guild()
    guild.members.add("1700000000000000099")
    refused(guild, tmp_path, r"has 4 members, not only the owner and the two kit bots")


def test_clean_refuses_when_the_count_is_absent(tmp_path):
    guild = Guild()
    guild.count = False
    refused(guild, tmp_path, r"has None members")


def test_clean_without_the_bridge_bot_counts_two(tmp_path):
    guild = Guild()
    guild.members.discard(BRIDGE)
    guild.msgs[TEMPLATE_GENERAL] = [_join(ADMIN)]
    assert run_clean(guild, tmp_path, True) == 0


def test_clean_stops_on_a_member_lookup_that_is_not_a_404(tmp_path):
    guild = Guild()
    guild.member_error = 403
    refused(guild, tmp_path, r"HTTP 403")


@pytest.mark.parametrize("msg", [
    {"id": "9", "type": 0, "author": {"id": OWNER}, "content": ""},     # the owner said hello
    {"id": "9", "type": 18, "author": {"id": OWNER}, "content": ""},    # a thread was started here
    _join("1700000000000000099"),                                       # someone else joined (and left)
])
def test_clean_refuses_a_channel_with_anything_but_the_kit_joins(tmp_path, msg):
    guild = Guild()
    guild.msgs[TEMPLATE_GENERAL].append(msg)
    refused(guild, tmp_path, r"#général holds a message")


THREAD = {"id": made_after_guild(600_000), "type": 12, "name": "notes"}  # a private thread: no marker in its parent


@pytest.mark.parametrize("where", ["active", "public", "private"])
def test_clean_refuses_a_channel_with_a_thread_its_messages_do_not_show(tmp_path, where):
    """The owner's private thread in the template #general leaves no message there, and deleting #general would
    delete it: active threads (the guild's, by parent) and archived ones (the channel's) both block."""
    guild = Guild()
    if where == "active":
        guild.active_threads = [{**THREAD, "parent_id": TEMPLATE_GENERAL}]
    else:
        guild.archived[where] = {TEMPLATE_GENERAL: [{**THREAD, "parent_id": TEMPLATE_GENERAL}]}
    refused(guild, tmp_path, r"#général has 1 thread\(s\), which would be deleted with it")


class LateGuild(Guild):
    """The owner opens a private thread in, or writes in, the template #général after clean's checks read it and
    before its DELETE (codex review 2026-10-16). Discord has no conditional DELETE, so clean looks again just before
    each channel's own DELETE: that narrows the window to one channel's reads, it cannot close it."""

    def __init__(self, late, at="read"):
        super().__init__()
        self.late, self.at, self.reads = late, at, 0

    def act(self):
        if self.late == "thread":
            self.active_threads.append({**THREAD, "parent_id": TEMPLATE_GENERAL})
        else:
            self.msgs[TEMPLATE_GENERAL].append({"id": "9", "type": 0, "author": {"id": OWNER}, "content": ""})

    def __call__(self, method, path, body=None, file=None):
        out = super().__call__(method, path, body, file)
        watched = (f"/guilds/{GID}/threads/active", f"/channels/{TEMPLATE_GENERAL}/messages?limit=100")
        if self.at == "read" and method == "GET" and path in watched:
            self.reads += 1
            if self.reads == 2:  # the checks' reads of #général are done: the owner acts now
                self.act()
        if self.at == "delete" and method == "DELETE":  # the owner acts while another channel is deleted
            self.act()
        return out


LATE = [("thread", r"#général has 1 thread\(s\)"), ("message", r"#général holds a message")]


@pytest.mark.parametrize("late,why", LATE)
def test_clean_looks_again_just_before_each_delete(tmp_path, late, why):
    guild = LateGuild(late)
    with pytest.raises(SystemExit) as e:
        run_clean(guild, tmp_path, True)
    assert re.search(rf"stopped before 'général': {why}.*Already deleted: none", str(e.value.code)), e.value.code
    assert guild.deletes() == []


@pytest.mark.parametrize("late,why", LATE)
def test_clean_names_what_it_deleted_before_a_late_refusal(tmp_path, late, why):
    guild = LateGuild(late, at="delete")
    guild.channels[1], guild.channels[3] = guild.channels[3], guild.channels[1]  # the voice Général goes first
    with pytest.raises(SystemExit) as e:
        run_clean(guild, tmp_path, True)
    assert re.search(rf"stopped before 'général': {why}.*Already deleted: 'Général'", str(e.value.code)), \
        e.value.code
    assert guild.deletes() == [f"/channels/{TEMPLATE[3]['id']}"]


def test_clean_reads_threads_of_text_channels_only_and_ignores_other_parents(tmp_path):
    guild = Guild(synced=True)
    other = next(c for c in guild.channels if c["type"] == sync.TEXT and c["id"] != TEMPLATE_GENERAL)
    guild.active_threads = [{**THREAD, "parent_id": other["id"]}]  # a kit channel's thread: not deleted, no block
    assert run_clean(guild, tmp_path, True) == 0
    read = [p for _, p in guild.calls if "/threads/archived/" in p]
    assert read == [f"/channels/{TEMPLATE_GENERAL}/threads/archived/public",
                    f"/channels/{TEMPLATE_GENERAL}/threads/archived/private"] * 2  # the check, the last look


@pytest.mark.parametrize("chan", [
    {"name": "projets", "type": sync.TEXT, "position": 3},      # an empty private channel the owner made
    {"name": "Salon", "type": sync.VOICE, "position": 3},
    {"name": "Mes salons", "type": sync.CATEGORY, "position": 3},
])
def test_clean_refuses_an_empty_channel_a_person_made(tmp_path, chan):
    """Empty is not proof the template made it (codex review 2026-10-16): only channels made with the guild go."""
    guild = Guild()
    guild.channels.append({**chan, "id": made_after_guild(5 * 60 * 1000)})
    refused(guild, tmp_path, rf"'{chan['name']}' was made 300s after the guild, so not by Discord's template")


def test_clean_after_the_first_sync_never_deletes_a_channel_made_since(tmp_path):
    guild = Guild()
    guild.channels = Guild.layout_channels() + [{"id": made_after_guild(86_400_000), "name": "vide",
                                                 "type": sync.TEXT, "position": 9}]
    refused(guild, tmp_path, r"'vide' was made 86400s after the guild")


def test_clean_takes_the_template_s_channels_within_its_window(tmp_path):
    guild = Guild()
    guild.channels[1]["id"] = made_after_guild(bootstrap.TEMPLATE_WINDOW_MS)  # the edge: still the template's
    guild.msgs = {guild.channels[1]["id"]: [_join(ADMIN), _join(BRIDGE)]}
    assert run_clean(guild, tmp_path, True) == 0
    assert bootstrap.made_ms(GID) - bootstrap.DISCORD_EPOCH_MS == int(GID) >> 22


def test_clean_takes_an_empty_channel_made_inside_the_window_and_lists_it_first(tmp_path, capsys):
    """The accepted residual (review 2026-10-16): a channel's time says when, not who, so an empty channel the person
    made within the window goes with the template's. The dry run names it before anything is deleted."""
    guild = Guild()
    guild.channels.append({"id": made_after_guild(30_000), "name": "règles", "type": sync.TEXT, "position": 1})
    assert run_clean(guild, tmp_path, False) == bootstrap.PENDING
    assert "- delete text 'règles'" in capsys.readouterr().out and guild.deletes() == []


def test_clean_refuses_a_channel_too_full_to_read_at_once(tmp_path):
    guild = Guild()
    guild.msgs[TEMPLATE_GENERAL] = [_join(ADMIN)] * bootstrap.MESSAGES_LIMIT
    refused(guild, tmp_path, r"#général holds 100 or more messages")


def test_clean_refuses_when_the_admin_bot_is_not_administrator(tmp_path):
    """Without Read Message History the message list comes back empty: emptiness is unproven, so it refuses."""
    guild = Guild()
    guild.admin_perms = sync.VIEW | (1 << 4)  # View Channels + Manage Channels, no Administrator
    refused(guild, tmp_path, r"lacks Administrator")


def test_clean_refuses_a_channel_type_it_does_not_clear(tmp_path):
    guild = Guild()
    guild.channels.append({"id": made_after_guild(2), "name": "annonces", "type": 15, "position": 2})
    refused(guild, tmp_path, r"'annonces' is a type 15 channel")


def test_clean_fails_when_the_read_back_still_shows_a_channel(tmp_path):
    guild = Guild()
    guild.deletes_stick = False
    with pytest.raises(SystemExit, match=r"4 channel\(s\) still there after the delete"):
        run_clean(guild, tmp_path, True)


def test_main_clean_refuses_the_asus_node_instance_before_any_call(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("DISCORD_TOKEN=admin-token\n")
    monkeypatch.setattr(sync, "DOTENV", tmp_path / ".env")
    asus = tmp_path / "kit.toml"
    asus.write_text(EXAMPLE.read_text().replace('profile = "kit"', 'profile = "asus-node"'))
    monkeypatch.setattr(sync, "KIT_TOML", asus)
    guild = Guild()
    monkeypatch.setattr(sync, "Api", lambda token: guild)
    with pytest.raises(SystemExit, match='only with discord.profile = "kit"'):
        bootstrap.main(["clean", "--apply"])
    assert guild.calls == []


# ── main: the tokens come from their files ───────────────────────────────────

def test_main_discover_is_read_only():
    with pytest.raises(SystemExit) as e:
        bootstrap.main(["discover", "--apply"])
    assert e.value.code == 2


def test_main_reads_both_tokens_from_their_files(monkeypatch, tmp_path, dc, capsys):
    (tmp_path / ".env").write_text("DISCORD_TOKEN=admin-token\n")
    (tmp_path / ".env.bridge").write_text("DISCORD_BRIDGE_TOKEN=bridge-token\nDISCORD_GUILD_ID=x\n")
    monkeypatch.setattr(sync, "DOTENV", tmp_path / ".env")
    monkeypatch.setattr(bootstrap, "BRIDGE_DOTENV", tmp_path / ".env.bridge")
    monkeypatch.setattr(sync, "Api", dc.api)
    monkeypatch.setattr(sync, "KIT_TOML", tmp_path / "kit.toml")
    assert bootstrap.main(["discover"]) == bootstrap.PENDING
    assert {tok for tok, *_ in dc.calls} == {"admin-token", "bridge-token"}
    assert "admin-token" not in capsys.readouterr().out


@pytest.mark.parametrize("text", [None, "DISCORD_GUILD_ID=1\n", "DISCORD_BRIDGE_TOKEN=\n"])
def test_main_refuses_without_a_bridge_token_before_any_call(monkeypatch, tmp_path, dc, text):
    (tmp_path / ".env").write_text("DISCORD_TOKEN=admin-token\n")
    monkeypatch.setattr(sync, "DOTENV", tmp_path / ".env")
    if text is not None:
        (tmp_path / ".env.bridge").write_text(text)
        monkeypatch.setattr(bootstrap, "BRIDGE_DOTENV", tmp_path / ".env.bridge")
    monkeypatch.setattr(sync, "Api", dc.api)
    for step in ("discover", "intents"):
        with pytest.raises(SystemExit, match="DISCORD_BRIDGE_TOKEN|missing"):
            bootstrap.main([step])
    assert dc.calls == []


# ---- no Discord (kit.toml discord.enabled = false): nothing to sync or bootstrap, refused before any call ----------

def _no_discord_toml(tmp_path):
    f = tmp_path / "kit.toml"
    f.write_text(re.sub(r"(?ms)^\[discord\]\n.*?(?=^\[)", "[discord]\nenabled = false\n\n", EXAMPLE.read_text()))
    return f


def test_sync_refuses_a_kit_with_no_discord(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "KIT_TOML", _no_discord_toml(tmp_path))
    with pytest.raises(SystemExit, match="no Discord"):
        sync.load_kit_config()


@pytest.mark.parametrize("step", ["discover", "intents", "clean", "webhook", "invite"])
def test_bootstrap_refuses_a_kit_with_no_discord_before_reading_a_token(tmp_path, monkeypatch, step):
    monkeypatch.setattr(sync, "KIT_TOML", _no_discord_toml(tmp_path))
    monkeypatch.setattr(sync, "load_env", lambda: pytest.fail("read discord/.env with no Discord"))
    monkeypatch.setattr(bootstrap, "bridge_token", lambda: pytest.fail("read .env.bridge with no Discord"))
    with pytest.raises(SystemExit, match="no Discord"):
        bootstrap.main([step])


def test_the_no_discord_refusals_catch_their_mutants(tmp_path, monkeypatch):
    """With either refusal taken out, the no-Discord kit reaches the token or the config (the tests above pass)."""
    import importlib.util
    monkeypatch.setattr(sync, "KIT_TOML", _no_discord_toml(tmp_path))
    for name, guard in (("sync", "    if not kitconfig.discord_enabled(cfg):\n        sys.exit("),
                        ("bootstrap", "    if no_discord:\n        sys.exit(")):
        src = (sync.HERE / f"{name}.py").read_text()
        assert src.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
        path = tmp_path / f"{name}_mut.py"
        path.write_text(src.replace(guard, guard.replace("if not kitconfig.discord_enabled(cfg)", "if False")
                                    .replace("if no_discord", "if False")))
        spec = importlib.util.spec_from_file_location(f"{name}_mut", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if name == "sync":
            mod.KIT_TOML = sync.KIT_TOML
            mod.load_kit_config()  # the mutant hands a no-Discord config to the guild code
        else:
            reached = []
            monkeypatch.setattr(sync, "load_env", lambda: reached.append("env") or sys_exit("stop"))
            with pytest.raises(SystemExit, match="stop"):
                mod.main(["discover"])
            assert reached == ["env"]


def sys_exit(msg):
    raise SystemExit(msg)


def test_sync_main_says_no_discord_before_asking_for_a_token(tmp_path, monkeypatch):
    """The command line, not just load_kit_config: discord/.env on a no-Discord server holds only the share link, so
    a main that read the token first would say "missing DISCORD_TOKEN" and send Claude back into the Discord path."""
    import importlib.util
    monkeypatch.setattr(sync, "KIT_TOML", _no_discord_toml(tmp_path))
    monkeypatch.setattr(sync.sys, "argv", ["sync.py"])
    monkeypatch.setattr(sync, "read_env", lambda: {"TAILSCALE_SHARE_LINK": "x"})
    with pytest.raises(SystemExit, match="no Discord"):
        sync.main()
    guard = "    refuse_no_discord()  # before discord/.env"
    src = (sync.HERE / "sync.py").read_text()
    assert src.count(guard) == 1, "stale or ambiguous mutation pattern"
    path = tmp_path / "sync_main_mut.py"
    path.write_text(src.replace(guard, "    pass  # before discord/.env"))
    spec = importlib.util.spec_from_file_location("sync_main_mut", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.KIT_TOML = sync.KIT_TOML
    mod.read_env = lambda: {"TAILSCALE_SHARE_LINK": "x"}
    with pytest.raises(SystemExit, match="missing DISCORD_TOKEN"):  # the mutant asks for the token first
        mod.main()
