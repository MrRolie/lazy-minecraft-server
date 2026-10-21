#!/usr/bin/env python3
"""A kit guild's first steps, once the human has made the guild and the two bots (docs/kit/HUMAN-STEPS.md steps
10-16; docs/kit/KIT.md P1, P6, P12). Every step is idempotent and checks its own result.

  python3 discord/bootstrap.py discover            P1, read-only: discord.guild_id + discord.bridge_bot_id
  python3 discord/bootstrap.py intents [--apply]   P1: Message Content on for the bridge app
  python3 discord/bootstrap.py clean   [--apply]   P6, before sync: delete what Discord's template made (a fresh guild)
  python3 discord/bootstrap.py webhook [--apply]   P6, after sync --apply: the alert webhook in #<alert_channel>
  python3 discord/bootstrap.py invite  [--apply]   P12: the friends' invite into the first channel (#welcome)

Dry run by default. Exit 0: nothing left to do, checked. Exit 2: an action or a human step is pending (printed).
Any other non-zero exit: refused.
Tokens: the admin bot's DISCORD_TOKEN (discord/.env) and the bridge bot's DISCORD_BRIDGE_TOKEN (.env.bridge), sent as
`Authorization: Bot`. Both must belong to bots (`GET /users/@me` says bot: true). A user token is never used: automating
a user account is self-botting, which Discord's terms forbid. Nothing here prints a token or the webhook URL.
The invite lockdown is not here: with kit.toml discord.invite_lockdown = true, discord/sync.py sets it.
Stdlib only.
"""
import argparse
import datetime
import os
import re
import sys
import tomllib
from pathlib import Path

import sync

BRIDGE_DOTENV = sync.REPO / ".env.bridge"  # the bridge bot's token (compose env_file); read for its id and its app
INSTALLED = Path("/etc/mc-host/webhook")   # root:<kit user> 640; host/setup.sh step 7 installs it from the staged copy
STAGED_REL = Path(".config") / "mc-kit" / "webhook"  # under kit.toml host.home; setup.sh reads the same path
WEBHOOK_NAME = "mc-alert"  # Discord refuses names containing "clyde" or "discord"
WEBHOOK_URL = re.compile(r"https://discord\.com/api/webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{1,100}")
MESSAGE_CONTENT = 1 << 18          # GATEWAY_MESSAGE_CONTENT (verified apps, 100+ servers)
MESSAGE_CONTENT_LIMITED = 1 << 19  # under 100 servers; one of the three flags PATCH /applications/@me may set
ADMINISTRATOR = 1 << 3
ADMIN_PERMISSIONS = ADMINISTRATOR  # the admin bot keeps Administrator (AGENTS.md "Discord surface")
USER_JOIN = 7                      # message type: "X joined the server", posted when a bot is added
MESSAGES_LIMIT = 100               # the most GET /channels/{id}/messages returns at once
DISCORD_EPOCH_MS = 1420070400000   # a snowflake's top 42 bits are milliseconds since this (2015-01-01 UTC)
TEMPLATE_WINDOW_MS = 60_000        # the template's channels are made with the guild; a channel made later, a person
                                   # made (one made inside the window is taken for the template's: see not_the_template)
BRIDGE_PERMISSIONS = 2147568640    # View, Send, Embed, History, Use App Commands (bridge/README.md)
INVITE_MAX_AGE = 7 * 24 * 3600     # the longest finite invite: a leaked link stops working within a week
INVITE_MIN_LEFT = datetime.timedelta(days=1)  # reuse a bot-made invite only while it has a day left
PENDING = 2


def human(text):
    print(f"HUMAN STEP: {text}")


def bridge_token():
    """DISCORD_BRIDGE_TOKEN from .env.bridge (compose env_file format: KEY=value, raw)."""
    if not BRIDGE_DOTENV.is_file():
        sys.exit(f"{BRIDGE_DOTENV.name} is missing: paste the bridge bot's token first "
                 "(docs/kit/HUMAN-STEPS.md step 15; .env.bridge.example)")
    for line in BRIDGE_DOTENV.read_text().splitlines():
        if line.startswith("DISCORD_BRIDGE_TOKEN="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    sys.exit(f"{BRIDGE_DOTENV.name} has no DISCORD_BRIDGE_TOKEN")


def bot_id(api, who):
    me = api("GET", "/users/@me")
    if me.get("bot") is not True:  # absent is not a yes
        sys.exit(f"the {who} token does not belong to a bot; the kit uses bot tokens only, never a user's")
    return str(me["id"])


def guild_ids(api):
    return [str(g["id"]) for g in api("GET", "/users/@me/guilds")]


def authorize_url(client_id, permissions, guild=None):
    url = f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=bot%20applications.commands" \
          f"&permissions={permissions}"
    return url + (f"&guild_id={guild}&disable_guild_select=true" if guild else "")


# ── discover (P1) ────────────────────────────────────────────────────────────

def kit_toml_discord(path):
    """kit.toml's [discord] table as written (not validated: discover runs before kit.toml is complete), or None
    when there is no kit.toml yet."""
    if not path.exists():
        return None
    try:
        return tomllib.loads(path.read_text()).get("discord") or {}
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{path}: not valid TOML: {e}")


def discover(admin, bridge, kit_path):
    admin_id, bridge_id = bot_id(admin, "admin (discord/.env DISCORD_TOKEN)"), bot_id(bridge, "bridge")
    if admin_id == bridge_id:
        sys.exit("discord/.env and .env.bridge hold the same bot: the kit keeps two (the admin token never enters "
                 "the bridge container)")
    mine, theirs = guild_ids(admin), guild_ids(bridge)
    if len(mine) > 1:
        sys.exit(f"the admin bot is in {len(mine)} guilds ({', '.join(mine)}); a kit's private bot belongs in "
                 "exactly one: remove it from the others")
    if not mine:
        human("add the admin bot to your server: open this link, pick the server, Continue, Authorize:\n  "
              + authorize_url(admin("GET", "/applications/@me")["id"], ADMIN_PERMISSIONS))
        return PENDING
    gid = mine[0]
    if len(theirs) > 1 or (theirs and theirs[0] != gid):
        sys.exit(f"the bridge bot is in {', '.join(theirs)}, not only the admin bot's guild {gid}: "
                 "remove it from the others")
    if not theirs:
        human("add the bridge bot to the same server: open this link, Continue, Authorize:\n  "
              + authorize_url(bridge("GET", "/applications/@me")["id"], BRIDGE_PERMISSIONS, gid))
        return PENDING
    want = {"guild_id": gid, "bridge_bot_id": bridge_id}
    print("[discord]\n" + "".join(f'{k} = "{v}"\n' for k, v in want.items()), end="")
    have = kit_toml_discord(kit_path)
    if have is None:
        print(f"{kit_path} does not exist yet: put the two lines above in its [discord] table")
        return PENDING
    pending = []
    for key, value in want.items():
        cur = have.get(key)
        if cur == value:
            continue
        if cur in (None, "") or (isinstance(cur, str) and set(cur) == {"0"}):  # unset, or the example placeholder
            pending.append(key)
        else:
            sys.exit(f"kit.toml discord.{key} = {cur!r}, but the bots say {value!r}. The guild changed, or a bot was "
                     "re-made: check which is right before changing kit.toml (render copies the guild id into "
                     ".env.bridge)")
    if pending:
        print(f"set {', '.join('discord.' + k for k in pending)} in {kit_path} to the values above")
        return PENDING
    print(f"{kit_path} agrees: both bots are in guild {gid}")
    return 0


# ── intents (P1) ─────────────────────────────────────────────────────────────

def intents(bridge, apply):
    """Message Content for the bridge (it reads #game-chat). GET flags, OR in GATEWAY_MESSAGE_CONTENT_LIMITED, PATCH,
    GET again. A refused PATCH, or a GET that does not show the bit, falls back to the portal toggle."""
    app = bridge("GET", "/applications/@me")
    name, flags = app.get("name", "the bridge app"), int(app.get("flags") or 0)
    fallback = (f"discord.com/developers/applications → {name} → Bot → Privileged Gateway Intents → turn on "
                "Message Content Intent → Save Changes. Then re-run: python3 discord/bootstrap.py intents")
    if flags & (MESSAGE_CONTENT | MESSAGE_CONTENT_LIMITED):
        print(f"unchanged: Message Content is on for {name}")
        return 0
    print(f"~ PATCH /applications/@me ({name}): flags + GATEWAY_MESSAGE_CONTENT_LIMITED (1<<19)")
    if not apply:
        print("dry run; --apply to execute")
        return PENDING
    try:
        bridge("PATCH", "/applications/@me", {"flags": flags | MESSAGE_CONTENT_LIMITED})
    except sync.ApiError as e:
        print(f"the API refused it: {e.code}")
        human(fallback)
        return PENDING
    if int(bridge("GET", "/applications/@me").get("flags") or 0) & MESSAGE_CONTENT_LIMITED:
        print(f"set: Message Content is on for {name} (read back with GET /applications/@me)")
        return 0
    print("the PATCH returned, but GET /applications/@me does not show the flag")
    human(fallback)
    return PENDING


# ── clean (P6, before the first sync) ────────────────────────────────────────

def is_admin(api, gid, me, guild):
    """True when the admin bot holds Administrator in the guild (its roles plus @everyone, whose id is the guild's)."""
    roles = {str(r["id"]): int(r.get("permissions") or 0) for r in guild.get("roles") or []}
    mine = [str(r) for r in api("GET", f"/guilds/{gid}/members/{me}").get("roles") or []] + [str(gid)]
    return any(roles.get(r, 0) & ADMINISTRATOR for r in mine)


def members_of(api, gid, ids):
    """The ids that are members of the guild. GET /guilds/{id}/members/{user.id} needs no privileged intent (only
    the member list does), so the kit's admin bot never needs Server Members Intent."""
    found = set()
    for uid in ids:
        try:
            api("GET", f"/guilds/{gid}/members/{uid}")
        except sync.ApiError as e:
            if e.status != 404:  # 404 Unknown Member is the one "no"; anything else is unverified
                raise
            continue
        found.add(uid)
    return found


def made_ms(snowflake):
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def not_the_template(gid, chan):
    """Why this channel is not one Discord's template made, or None. The template's channels are created with the
    guild, so their ids carry (almost) the guild id's own time; a channel made more than TEMPLATE_WINDOW_MS later,
    even an empty one, is a person's (codex review 2026-10-16), and a re-run after the first sync finds only channels
    made since. The time says when, not who: an empty channel a person made inside the window passes as the
    template's (review 2026-10-16, accepted). It is never one holding a message or a thread (blocking), and the dry
    run lists it by name before --apply deletes it."""
    late = made_ms(chan["id"]) - made_ms(gid)
    if late > TEMPLATE_WINDOW_MS:
        return (f"{chan['name']!r} was made {late // 1000}s after the guild, so not by Discord's template: someone "
                "made it. Delete it by hand if it should go")
    return None


def blocking(api, chan, allowed, threads):
    """Why this channel must not be deleted, or None. Only `type` and `author.id` are read: without Message Content
    the API still returns both (content comes back empty). A public thread leaves a THREAD_CREATED (18) in its parent,
    but a private one leaves nothing there, and deleting the parent deletes its threads: so the guild's active threads
    (`threads`, their parent ids) and the channel's archived public and private ones are read too."""
    if chan["type"] == sync.CATEGORY:
        return None  # a category holds no messages; its children are checked one by one
    if chan["type"] not in (sync.TEXT, sync.VOICE):
        return f"{chan['name']!r} is a type {chan['type']} channel; the kit only clears text and voice channels"
    if chan["type"] == sync.TEXT:
        n = sum(1 for t in threads if str(t.get("parent_id")) == str(chan["id"]))
        for kind in ("public", "private"):
            n += len(api("GET", f"/channels/{chan['id']}/threads/archived/{kind}").get("threads") or [])
        if n:
            return f"#{chan['name']} has {n} thread(s), which would be deleted with it"
    msgs = api("GET", f"/channels/{chan['id']}/messages?limit={MESSAGES_LIMIT}")
    if len(msgs) >= MESSAGES_LIMIT:
        return f"#{chan['name']} holds {MESSAGES_LIMIT} or more messages, more than a fresh guild has"
    for m in msgs:
        if m.get("type") != USER_JOIN or str((m.get("author") or {}).get("id")) not in allowed:
            return f"#{chan['name']} holds a message (type {m.get('type')}) other than a bot or the owner joining"
    return None


def clean(api, cfg, gid, apply):
    """Delete every channel and category the kit layout does not list (Discord's template: Text Channels, #general,
    Voice Channels, General), so the first sync starts from an empty guild. Template names depend on the client's
    language, so none are matched: the template's age and emptiness are the safety. Refused unless the profile is
    "kit", the guild's members are only the owner and the two kit bots, the admin bot can read every channel
    (Administrator: without Read Message History the API returns an empty list, not an error), every channel to
    delete was made within TEMPLATE_WINDOW_MS of the guild, and each holds nothing but member-join messages from
    those three and no thread. Discord has no conditional DELETE, so a thread or message the owner adds after the
    checks read a channel would go with it: each channel is checked again just before its own DELETE, which narrows
    that window to a few calls and cannot close it (codex review 2026-10-16)."""
    if cfg["discord"]["profile"] != "kit":
        sys.exit(f'clean deletes channels, so it runs only with discord.profile = "kit", never '
                 f'{cfg["discord"]["profile"]!r}')
    try:
        sync.profile_guard(cfg)  # never the asus-node guild
    except ValueError as e:
        sys.exit(f"error: {e}")
    layout = sync.load_layout((sync.HERE / "layout.toml").read_text(), sync.kit_bots(cfg["discord"]), "kit")
    channels = api("GET", f"/guilds/{gid}/channels")
    gone = sync.unlisted(layout, channels)
    kept = [c["name"] for c in channels if c not in gone]
    if kept:
        print(f"keep (layout.toml lists them): {', '.join(kept)}")
    if not gone:
        print("unchanged: nothing outside layout.toml to delete")
        return 0
    me = bot_id(api, "admin")
    guild = api("GET", f"/guilds/{gid}?with_counts=true")
    allowed = members_of(api, gid, {str(guild["owner_id"]), me, str(cfg["discord"]["bridge_bot_id"])})
    count = guild.get("approximate_member_count")
    if count != len(allowed):
        sys.exit(f"the guild has {count} members, not only the owner and the two kit bots: clean is for a guild "
                 "minutes old. Delete the extra channels by hand instead")
    if not is_admin(api, gid, me, guild):
        sys.exit("the admin bot lacks Administrator, so it cannot prove a channel is empty (the API hides history it "
                 "cannot read). Re-add it with the link discover prints")
    why = [w for w in (not_the_template(gid, c) for c in gone) if w]
    threads = api("GET", f"/guilds/{gid}/threads/active").get("threads") or []
    why += [w for w in (blocking(api, c, allowed, threads) for c in gone) if w]
    if why:
        sys.exit("refusing, nothing deleted:\n  " + "\n  ".join(why))
    kinds = {sync.TEXT: "text", sync.VOICE: "voice", sync.CATEGORY: "category"}
    gone.sort(key=lambda c: c["type"] == sync.CATEGORY)  # children first: deleting a category orphans, never cascades
    for c in gone:
        print(f"- delete {kinds[c['type']]} {c['name']!r}")
    if not apply:
        print("dry run; --apply to execute")
        return PENDING
    for n, c in enumerate(gone):
        if c["type"] != sync.CATEGORY:  # the last look (a category holds no messages and never cascades)
            late = blocking(api, c, allowed, api("GET", f"/guilds/{gid}/threads/active").get("threads") or [])
            if late:
                sys.exit(f"refusing, stopped before {c['name']!r}: {late}. Already deleted: "
                         f"{', '.join(repr(d['name']) for d in gone[:n]) or 'none'}. Re-run clean once it is clear")
        api("DELETE", f"/channels/{c['id']}")
    left = {str(c["id"]) for c in api("GET", f"/guilds/{gid}/channels")} & {str(c["id"]) for c in gone}
    if left:
        sys.exit(f"{len(left)} channel(s) still there after the delete ({', '.join(sorted(left))}); re-run clean")
    print(f"deleted {len(gone)}; read back: none of them is left")
    return 0


# ── webhook (P6) ─────────────────────────────────────────────────────────────

def text_channel(api, gid, name):
    found = [c for c in api("GET", f"/guilds/{gid}/channels") if c["type"] == sync.TEXT and c["name"] == name]
    if not found:
        sys.exit(f"no #{name} in the guild yet: run python3 discord/sync.py --apply first")
    if len(found) > 1:
        sys.exit(f"{len(found)} text channels are named #{name}; rename or delete the extra one by hand")
    return str(found[0]["id"])


def webhook_url(hook):
    """The execute URL, built from the id and token (the `url` field is only returned by the OAuth2 flow). It is a
    secret from here on: every later print and API error redacts it."""
    url = f"https://discord.com/api/webhooks/{hook['id']}/{hook.get('token', '')}"
    if not WEBHOOK_URL.fullmatch(url):
        sys.exit(f"webhook {hook['id']} did not come back with a usable token")
    sync.REDACT[url] = "<webhook>"
    sync.REDACT[hook["token"]] = "<webhook token>"
    return url


def staged_path(cfg):
    return Path(cfg["host"]["home"]) / STAGED_REL


def read_plain(path):
    """A file's text, or None when absent or unreadable. A link is refused."""
    if path.is_symlink():
        sys.exit(f"{path} is a symlink; refusing to use it (remove it)")
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return None


def stage(path, url):
    """Write the URL for host/setup.sh step 7, which installs it as root:<kit user> 640 and removes this copy."""
    import render  # kit/render.py (sync put kit/ on sys.path): the same atomic write, mode 600
    if path.parent.is_symlink() or path.is_symlink():
        sys.exit(f"{path} or its directory is a symlink; refusing to write the webhook through it")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    render.write_atomic(path, url + "\n", 0o600)


def webhook(api, cfg, gid, apply):
    if os.geteuid() == 0:
        sys.exit("run as the kit user, not root: the staged webhook must be the kit user's file")
    name = cfg["discord"]["alert_channel"]
    me = bot_id(api, "admin")
    cid = text_channel(api, gid, name)
    hooks = [w for w in api("GET", f"/channels/{cid}/webhooks")
             if w.get("name") == WEBHOOK_NAME and str((w.get("user") or {}).get("id")) == me]
    if len(hooks) > 1:
        sys.exit(f"{len(hooks)} {WEBHOOK_NAME} webhooks in #{name} ({', '.join(str(w['id']) for w in hooks)}); "
                 "delete all but one (Channel settings → Integrations → Webhooks), then re-run")
    if hooks:
        hook = hooks[0]
        if not hook.get("token"):
            sys.exit(f"webhook {hook['id']} in #{name} came back without its token; delete it (Channel settings → "
                     "Integrations → Webhooks) and re-run, never a second one beside it")
        print(f"unchanged: webhook {WEBHOOK_NAME} in #{name}")
    else:
        print(f"+ create webhook {WEBHOOK_NAME} in #{name}")
        if not apply:
            print("dry run; --apply to execute")
            return PENDING
        hook = api("POST", f"/channels/{cid}/webhooks", {"name": WEBHOOK_NAME})
    url = webhook_url(hook)
    got = api("GET", f"/webhooks/{hook['id']}/{hook['token']}")  # proves the URL works, without posting
    if str(got.get("id")) != str(hook["id"]) or str(got.get("channel_id")) != cid:
        sys.exit(f"the webhook's own URL answers for a different webhook or channel than #{name}")
    staged = staged_path(cfg)
    if read_plain(INSTALLED) == url:
        print(f"unchanged: {INSTALLED} holds it")
        return 0
    if read_plain(staged) == url:
        print(f"unchanged: staged at {staged}; next: sudo bash host/setup.sh (step 7 installs it)")
        return 0
    print(f"~ stage the webhook URL at {staged} (mode 600) for host/setup.sh step 7")
    if not apply:
        print("dry run; --apply to execute")
        return PENDING
    stage(staged, url)
    print(f"staged; next: sudo bash host/setup.sh (step 7 installs it at {INSTALLED})")
    return 0


# ── invite (P12) ─────────────────────────────────────────────────────────────

def expires(inv):
    """-> an aware datetime, None for a never-expiring invite, or False when the listing says neither."""
    if inv.get("expires_at"):
        return datetime.datetime.fromisoformat(inv["expires_at"])
    if inv.get("max_age") == 0:
        return None
    return False


def invite(api, cfg, gid, apply, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if cfg["discord"]["invite_lockdown"]:
        try:
            unlocked = sync.plan_lockdown(api("GET", f"/guilds/{gid}"), gid)
        except ValueError as e:
            sys.exit(f"error: {e}")
        if unlocked:
            sys.exit("@everyone can still create invites: run python3 discord/sync.py --apply first (the lockdown "
                     "comes before the first invite)")
    layout = sync.load_layout((sync.HERE / "layout.toml").read_text(), sync.kit_bots(cfg["discord"]),
                              cfg["discord"]["profile"])
    first = next(ch["name"] for c in layout["categories"] for ch in c["channels"] if ch["type"] == "text")
    me = bot_id(api, "admin")
    cid = text_channel(api, gid, first)
    mine = []
    for inv in api("GET", f"/channels/{cid}/invites"):
        until = expires(inv)
        if str((inv.get("inviter") or {}).get("id")) == me and inv.get("max_uses", 0) == 0 \
                and (until is None or (until is not False and until - now >= INVITE_MIN_LEFT)):
            mine.append((until or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc), inv))
    if mine:
        inv = max(mine, key=lambda m: m[0])[1]
        print(f"unchanged: a bot-made invite into #{first}")
    else:
        print(f"+ create an invite into #{first} (7 days, any number of uses)")
        if not apply:
            print("dry run; --apply to execute")
            return PENDING
        inv = api("POST", f"/channels/{cid}/invites", {"max_age": INVITE_MAX_AGE, "max_uses": 0, "unique": True})
    got = api("GET", f"/invites/{inv['code']}")
    if str((got.get("guild") or {}).get("id") or got.get("guild_id")) != str(gid):
        sys.exit(f"invite {inv['code']} does not resolve to guild {gid}")
    until = expires(inv)
    print(f"https://discord.gg/{inv['code']}  ({'never expires' if until is None else f'expires {until:%Y-%m-%d %H:%M} UTC'})")
    print("With the lockdown on this is the one way in: share it with friends only.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["discover", "intents", "clean", "webhook", "invite"])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    # Before any token is read or any call made: a kit that chose no Discord has no guild and no bots (KIT.md "With
    # no Discord"). discover and intents run before kit.toml is complete, so this reads the choice as written.
    sys.path.insert(0, str(sync.REPO / "kit"))
    import kitconfig
    try:
        no_discord = not kitconfig.discord_choice(sync.KIT_TOML)
    except kitconfig.ConfigError as e:
        sys.exit(f"discord/bootstrap.py: {e}")
    if no_discord:
        sys.exit("discord/bootstrap.py: kit.toml says discord.enabled = false: this server has no Discord, so there "
                 "is nothing to bootstrap (docs/kit/KIT.md \"With no Discord\")")
    if args.step == "discover":
        if args.apply:
            ap.error("discover is read-only: it prints the two kit.toml lines")
        env = sync.load_env()
        return discover(sync.Api(env["DISCORD_TOKEN"]), sync.Api(bridge_token()), kitconfig.path(sync.KIT_TOML))
    if args.step == "intents":
        return intents(sync.Api(bridge_token()), args.apply)
    env = sync.load_env()
    cfg = sync.load_kit_config()
    gid = sync.guild_id(cfg["discord"], env)
    api = sync.Api(env["DISCORD_TOKEN"])
    if args.step == "clean":
        return clean(api, cfg, gid, args.apply)
    if args.step == "webhook":
        return webhook(api, cfg, gid, args.apply)
    return invite(api, cfg, gid, args.apply)


if __name__ == "__main__":
    sys.exit(main())
