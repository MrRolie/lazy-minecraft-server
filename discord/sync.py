#!/usr/bin/env python3
"""Sync the friend Discord guild to discord/layout.toml + discord/content/*.md.

Dry run by default (prints every action); --apply executes, then re-plans until
converged. Idempotent: a dry run after --apply must print "0 actions".
Never deletes a channel (unlisted ones are reported). Deletes only the bot's OWN
messages, and only in content-managed channels.

  python3 discord/sync.py                      # dry run
  python3 discord/sync.py --apply
  python3 discord/sync.py post announcements <file>.md [--apply]   # a kit: any path outside discord/posts/

Content: parts split on a line containing only `---`, each <=2000 chars.
`{#name}` in content resolves to a clickable link to that channel.
A line `{attach:name}` attaches one named local file (ATTACHMENTS) to that part
and is removed from the posted text. `pack` = the .mrpack compose runs (a custom pack's override first).
`{secret:name}` anywhere in content is filled at sync time from discord/.env (SECRETS); the value is
never printed. `share_link` = TAILSCALE_SHARE_LINK (+ optional TAILSCALE_SHARE_LINK_DATE, YYYY-MM-DD).
`{config:name}` is filled from kit.toml (CONFIG: server_address, host_name, world_status, pack_name); it is not
secret.
Env (discord/.env or environment): DISCORD_TOKEN. The guild id and the bridge bot id come from kit.toml
(discord.guild_id, discord.bridge_bot_id; layout.toml names the bridge bot "bridge"). kit.toml discord.profile
picks the guild's history (PROFILES), and discord.invite_lockdown = true makes sync turn Create Invite off for
@everyone. A kit's first steps (the ids, Message Content, clearing a fresh guild, the alert webhook, the invite):
discord/bootstrap.py.
The P6 stop (profile_guard, before any API call): the "asus-node" profile syncs only the asus-node guild, and any
other profile refuses that guild, its own text (discord/content/ outside kit/ and KIT_SHARED_CONTENT, and
discord/posts/) and its server address. A tree without the asus-node instance record (kit/instance.py: a public
copy drops kit/INSTANCE-PATHS) refuses the "asus-node" profile and serves "kit" as before; a tree with part of the
record refuses both.
Stdlib only.
"""
import argparse
import datetime
import json
import mimetypes
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
API = "https://discord.com/api/v10"
TEXT, VOICE, CATEGORY = 0, 2, 4
VIEW, SEND, EMBED, HISTORY = 1 << 10, 1 << 11, 1 << 14, 1 << 16
THREADS = (1 << 35) | (1 << 36) | (1 << 38)  # create public/private threads, send in threads
READ_ONLY_DENY = SEND | THREADS
BOT_ALLOW = VIEW | SEND | EMBED | HISTORY  # keeps the bot working if it loses Administrator
SUPPRESS_NOTIFICATIONS = 1 << 12
CREATE_INVITE = 1 << 0  # CREATE_INSTANT_INVITE
MAX_LEN = 2000
LINK = re.compile(r"\{#([a-z0-9_-]+)\}")
ATTACH = re.compile(r"^[ \t]*\{attach:([a-z0-9_-]+)\}[ \t]*$\n?", re.M)
SECRET = re.compile(r"\{secret:([^}]*)\}")  # broad: a malformed name is refused, never posted literally
CONFIG_REF = re.compile(r"\{config:([^}]*)\}")  # the same: an unknown or malformed name is refused
SHARE_LINK = re.compile(r"^https://login\.tailscale\.com/admin/invite/[A-Za-z0-9]+$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # fromisoformat alone also takes 20261002
SHARE_LINK_WARN_DAYS = 25  # Tailscale: unused share links expire after 30 days
# How the person puts a share link in discord/.env: kit/secret.py reads it at a hidden prompt and replaces the key's
# line (and the date's) in place, so the link never lands in argv or the shell history and a rotation never leaves a
# second pair of lines (review 2026-10-16; the old printf `>>` append did both).
SHARE_LINK_CMD = ("python3 kit/secret.py share-link  (in a terminal; paste only the link at its prompt: it replaces "
                  "TAILSCALE_SHARE_LINK and sets TAILSCALE_SHARE_LINK_DATE to today)")
ENV_KEYS = ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "TAILSCALE_SHARE_LINK", "TAILSCALE_SHARE_LINK_DATE")
MIME = mimetypes.MimeTypes()  # built-in table only: content types must not vary with the host's /etc/mime.types
# layout.toml names the bridge bot instead of holding its id; the id's one home is kit.toml discord.bridge_bot_id.
BOT_NAMES = ("bridge",)
# kit.toml discord.profile -> what differs between guilds. "asus-node" is the layout and content as written: that
# guild's story and the renamed_from that carried its channels over. "kit" is a fresh guild: nothing to rename, and
# no #the-story (drop): a kit has no story of its own, and an empty channel only puzzles friends (proof run
# 2026-10-16, finding 1).
PROFILES = {
    "asus-node": {"renames": True, "drop": (), "content": {}},
    "kit": {"renames": False, "drop": ("the-story",), "content": {"setup.md": "kit/setup.md"}},
}
# The P6 stop: what the asus-node guild has and no other guild may get. Its guild id and server address have their
# one home in its fixture; its own text is discord/content/ (its friends' story, the intents behind it, its #setup as
# posted) and discord/posts/. A guild whose profile is not "asus-node" gets only discord/content/kit/ and
# KIT_SHARED_CONTENT (templated text that names no instance), and the "asus-node" profile is pinned to the asus-node
# guild, so a kit that copied the fixture cannot post that story into a new guild. All of these are instance paths
# (kit/INSTANCE-PATHS). Whether this tree holds the record is kit/instance.py's one predicate, never "is the fixture
# file there": a tree without the record (a copy) has no asus-node guild to protect, so the "kit" profile runs and the
# "asus-node" profile refuses (it cannot run without its record); a tree with part of it refuses both, because a
# damaged live tree must never read as a copy. profile_guard().
ASUS_NODE = "asus-node"
ASUS_NODE_FIXTURE = REPO / "kit" / "fixtures" / "asus-node.toml"  # a kit/instance.py RECORD path (a test pins it)
INSTANCE_REPO = REPO  # where kit/instance.py looks for the record; the tests point it at a stand-in tree
KIT_SHARED_CONTENT = ("welcome.md",)
ASUS_NODE_POSTS = HERE / "posts"


# ── pure planning ────────────────────────────────────────────────────────────

def load_layout(text, bots=None, profile=None):
    """bots: {name: user id} for the named extra_bots (BOT_NAMES), from kit.toml (kit_bots). A layout that names a
    bot needs them. profile: kit.toml discord.profile (PROFILES); None is the layout exactly as written."""
    data = tomllib.loads(text)
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r} (known: {', '.join(PROFILES)})")
    prof = PROFILES[profile or ASUS_NODE]  # None: the layout as written (no drop, no remap)
    names = {ch["name"] for c in data["category"] for ch in c.get("channel", [])}
    stale = [n for n in prof["drop"] if n not in names]
    if stale:  # a drop that matches nothing would leave the channel in without a word
        raise ValueError(f"profile {profile!r} drops {', '.join(stale)}, which layout.toml does not list")
    cats = []
    for c in data["category"]:
        ro = c.get("read_only", False)
        chans = [{"type": "text", "topic": "", "read_only": ro, "content": None, "renamed_from": [], **ch}
                 for ch in c.get("channel", []) if ch["name"] not in prof["drop"]]
        for ch in chans:
            if not isinstance(ch["renamed_from"], list):
                raise ValueError(f"{ch['name']}: renamed_from must be a list of old names")
            if not prof["renames"]:
                ch["renamed_from"] = []
            if ch["content"]:
                ch["content"] = prof["content"].get(ch["content"], ch["content"])
        extra = c.get("extra_bots", [])
        if not (isinstance(extra, list) and all(isinstance(b, str) and (b.isdigit() or b in BOT_NAMES)
                                                for b in extra)):
            raise ValueError(f"{c['name']}: extra_bots must be a list of bot user ids (digit strings) or "
                             f"{', '.join(map(repr, BOT_NAMES))}")
        if any(b in BOT_NAMES for b in extra):
            if bots is None:  # an unresolved name would become an overwrite for a user called "bridge"
                raise ValueError(f"{c['name']}: extra_bots names a bot, but no bot ids were given (kit.toml)")
            extra = [bots[b] if b in BOT_NAMES else b for b in extra]
        cats.append({"name": c["name"], "private": c.get("private", False), "extra_bots": extra,
                     "channels": chans})
    return {"system_channel": data.get("guild", {}).get("system_channel"), "categories": cats}


def overwrites(guild_id, bot_id, *, private=False, read_only=False, extra_bots=()):
    """extra_bots: other bots (the bridge) that need this bot's allow in a locked category."""
    deny = (VIEW if private else 0) | (READ_ONLY_DENY if read_only else 0)
    if not deny:
        return []
    return [{"id": str(guild_id), "type": 0, "allow": "0", "deny": str(deny)},
            *({"id": str(b), "type": 1, "allow": str(BOT_ALLOW), "deny": "0"} for b in (bot_id, *extra_bots))]


def _norm(ows):
    return sorted((str(o["id"]), int(o["type"]), int(o["allow"]), int(o["deny"])) for o in ows)


def _type(ch):
    return VOICE if ch["type"] == "voice" else TEXT


def _ordered(chans):
    return sorted(chans, key=lambda c: (c["position"], int(c["id"])))


def plan(layout, current, guild_id, bot_id, system_channel_id=None):
    """Desired layout vs current guild channels -> (actions, notes). No I/O."""
    return _plan(layout, current, guild_id, bot_id, system_channel_id)[:2]


def unlisted(layout, current):
    """The current channels and categories the layout does not list: what plan() leaves alone, by plan()'s own
    matching (so a #general outside the layout's category is unlisted). discord/bootstrap.py clean deletes these."""
    known = _plan(layout, current, "0", "0")[2]  # the two ids only shape overwrites, never the matching
    return [c for c in current if c["id"] not in known]


def _plan(layout, current, guild_id, bot_id, system_channel_id=None):
    """-> (actions, notes, ids of the current channels and categories the layout lists)."""
    actions, notes, known = [], [], set()
    cur_cats = {c["name"]: c for c in current if c["type"] == CATEGORY}
    for ci, cat in enumerate(layout["categories"]):
        want = overwrites(guild_id, bot_id, private=cat["private"], extra_bots=cat["extra_bots"])
        have = cur_cats.get(cat["name"])
        if have is None:
            actions.append({"op": "create", "type": CATEGORY, "name": cat["name"], "parent": None,
                            "position": ci, "topic": None, "overwrites": want})
            children = []
        else:
            known.add(have["id"])
            if _norm(have.get("permission_overwrites", [])) != _norm(want):
                actions.append({"op": "patch", "id": have["id"], "name": cat["name"],
                                "fields": {"permission_overwrites": want}})
            children = [c for c in current if c.get("parent_id") == have["id"]]
        matched = []
        for pi, ch in enumerate(cat["channels"]):
            typ = _type(ch)
            want = overwrites(guild_id, bot_id, private=cat["private"], read_only=ch["read_only"],
                              extra_bots=cat["extra_bots"])
            topic = ch["topic"] if typ == TEXT else None
            cur = next((c for c in children if c["name"] == ch["name"] and c["type"] == typ), None)
            renamed = False
            if cur is None:  # renamed_from: rename the existing channel in place (its history stays)
                cur = next((c for c in children if c["name"] in ch.get("renamed_from", []) and c["type"] == typ),
                           None)
                renamed = cur is not None
            if cur is None:
                actions.append({"op": "create", "type": typ, "name": ch["name"], "parent": cat["name"],
                                "position": pi, "topic": topic, "overwrites": want})
                continue
            known.add(cur["id"])
            matched.append(cur)
            fields = {"name": ch["name"]} if renamed else {}
            if typ == TEXT and (cur.get("topic") or "") != topic:
                fields["topic"] = topic
            if _norm(cur.get("permission_overwrites", [])) != _norm(want):
                fields["permission_overwrites"] = want
            if fields:
                actions.append({"op": "patch", "id": cur["id"], "name": ch["name"], "fields": fields})
        # relative order within the category (absolute positions are Discord's business)
        if matched and [c["name"] for c in _ordered(matched)] != [c["name"] for c in matched]:
            actions.append({"op": "reorder", "name": cat["name"],
                            "positions": [{"id": c["id"], "position": i} for i, c in enumerate(matched)]})
    listed_cats = [cur_cats[c["name"]] for c in layout["categories"] if c["name"] in cur_cats]
    if [c["name"] for c in _ordered(listed_cats)] != [c["name"] for c in listed_cats]:
        actions.append({"op": "reorder", "name": "categories",
                        "positions": [{"id": c["id"], "position": i} for i, c in enumerate(listed_cats)]})
    want_sys = layout.get("system_channel")
    if want_sys:
        sys_ch = next((c for c in current if c["name"] == want_sys and c["type"] == TEXT), None)
        if sys_ch and sys_ch["id"] != system_channel_id:
            actions.append({"op": "guild", "name": f"system channel -> #{want_sys}",
                            "fields": {"system_channel_id": sys_ch["id"]}})
    for c in current:
        if c["id"] not in known:
            notes.append(f"unlisted channel {c['name']!r} (type {c['type']}) - left alone")
    return actions, notes, known


def split_parts(text):
    parts = [p.strip() for p in re.split(r"^---[ \t]*$", text, flags=re.M)]
    parts = [p for p in parts if p]
    for i, p in enumerate(parts):
        if len(p) > MAX_LEN:
            raise ValueError(f"content part {i} is {len(p)} chars (> {MAX_LEN})")
    return parts


def resolve_links(text, ids_by_name):
    def sub(m):
        if m.group(1) not in ids_by_name:
            raise ValueError(f"content links unknown channel {{#{m.group(1)}}}")
        return f"<#{ids_by_name[m.group(1)]}>"
    return LINK.sub(sub, text)


def take_attach(part):
    """-> (text without the directive line, attachment name or None). One per part."""
    names = ATTACH.findall(part)
    if len(names) > 1:
        raise ValueError(f"content part has {len(names)} {{attach:}} lines (max 1)")
    return ATTACH.sub("", part).strip(), (names[0] if names else None)


def _kitconfig():
    if str(REPO / "kit") not in sys.path:
        sys.path.insert(0, str(REPO / "kit"))  # kit/ only: the repo root would make `discord` this directory
    import kitconfig
    return kitconfig


# A custom pack's docker-compose.override.yml (kit/render.py) names it first, as compose reads it.
ATTACHMENTS = {"pack": lambda repo: repo / "dist" / _kitconfig().live_pack_file(repo)}


def resolve_attachment(name, repo=REPO):
    """Attachment name -> {path, filename, size}. Fail-closed on an unknown name or a missing file."""
    if name not in ATTACHMENTS:
        raise ValueError(f"content attaches unknown file {{attach:{name}}}")
    path = ATTACHMENTS[name](repo)
    if not path.is_file():
        raise ValueError(f"{{attach:{name}}} -> {path.relative_to(repo)} is missing: "
                         "run python3 pack/build.py first")
    return {"path": path, "filename": path.name, "size": path.stat().st_size}


def share_link(env, today):
    """TAILSCALE_SHARE_LINK -> (link, warnings). Fail-closed; no message ever contains the value."""
    link = env.get("TAILSCALE_SHARE_LINK", "")
    if not link:
        raise ValueError("{secret:share_link}: TAILSCALE_SHARE_LINK is missing or empty in discord/.env; "
                         f"put it there with: {SHARE_LINK_CMD}")
    if not SHARE_LINK.match(link):
        raise ValueError("{secret:share_link}: TAILSCALE_SHARE_LINK in discord/.env is not a Tailscale share link "
                         "(want https://login.tailscale.com/admin/invite/<code>, no quotes or spaces)")
    stamp = env.get("TAILSCALE_SHARE_LINK_DATE", "")
    if not stamp:
        return link, ["TAILSCALE_SHARE_LINK_DATE is not set in discord/.env, so the link's age is not tracked"]
    try:
        made = datetime.date.fromisoformat(stamp) if ISO_DATE.match(stamp) else None
    except ValueError:
        made = None
    if made is None:
        raise ValueError(f"TAILSCALE_SHARE_LINK_DATE {stamp!r} in discord/.env is not a YYYY-MM-DD date")
    age = (today - made).days
    if age < 0:  # a typo'd future date would silence the expiry warning
        raise ValueError(f"TAILSCALE_SHARE_LINK_DATE {stamp} in discord/.env is in the future")
    if age >= SHARE_LINK_WARN_DAYS:
        return link, [f"WARNING: share link is {age} days old; unused links may expire at 30 days, "
                      "regenerate it in the Tailscale admin and update discord/.env"]
    return link, []


SECRETS = {"share_link": share_link}
REDACT = {}  # resolved value -> "<name>", for every resolved secret; redact() applies it to all output


def resolve_secret(name, env, today):
    """Secret name -> (value, warnings). Fail-closed on an unknown name or a bad value."""
    if name not in SECRETS:
        raise ValueError(f"content uses unknown secret {{secret:{name}}} (known: {', '.join(sorted(SECRETS))})")
    value, warnings = SECRETS[name](env, today)
    REDACT[value] = f"<{name}>"
    return value, warnings


def redact(text):
    for value, label in REDACT.items():
        text = text.replace(value, label)
    return text


def fill_secrets(text, secrets):
    """Substitute every {secret:} from the up-front resolution; anything unresolved is a refusal."""
    def sub(m):
        if m.group(1) not in secrets:
            raise ValueError(f"content uses unresolved secret {{secret:{m.group(1)}}}")
        return secrets[m.group(1)]
    return SECRET.sub(sub, text)


def world_status(cfg):
    """The #welcome status line: before server.open_date is set the world is still being built (the line welcome.md
    carried before launch), after it the world is open. Setting open_date is the launch step (KIT.md P12)."""
    opened = cfg["server"].get("open_date")
    if opened is None:
        return "a fresh world is being built right now. Watch {#announcements} for launch."
    return (f"the world is open (since {opened.strftime('%B')} {opened.day}, {opened.year}). "
            "New chapters get announced in {#announcements}.")


# {config:name} -> its value from the validated kit.toml. An allowlist, never an arbitrary kit.toml path.
CONFIG = {
    "server_address": lambda cfg: cfg["tailscale"]["server_address"],
    "host_name": lambda cfg: cfg["tailscale"]["host_name"],
    "world_status": world_status,
    # the [pack] name of the pack this instance builds (pack.mode): the name servers.dat puts in the friends' list
    "pack_name": lambda cfg: _kitconfig().pack_section(_kitconfig().pack_dir(cfg, REPO))["name"],
}


def resolve_config(names, cfg):
    """{config:} names -> {name: value}. Fail-closed on an unknown name."""
    out = {}
    for name in names:
        if name not in CONFIG:
            raise ValueError(f"content uses unknown config {{config:{name}}} (known: {', '.join(sorted(CONFIG))})")
        out[name] = CONFIG[name](cfg)
    return out


def fill_config(text, values):
    """Substitute every {config:} from the up-front resolution; anything unresolved is a refusal. Runs before
    fill_secrets and resolve_links, so a value may carry a {#channel} link."""
    def sub(m):
        if m.group(1) not in values:
            raise ValueError(f"content uses unresolved config {{config:{m.group(1)}}}")
        return values[m.group(1)]
    return CONFIG_REF.sub(sub, text)


def _current(msg, part):
    text, file = part
    atts = msg.get("attachments", [])
    if file is None:
        return msg["content"] == text and not atts
    return (msg["content"] == text and len(atts) == 1
            and (atts[0]["filename"], atts[0]["size"]) == (file["filename"], file["size"]))


def plan_content(name, channel_id, parts, bot_messages):
    """parts: [(text, file or None)]. bot_messages: the bot's own messages in the channel, oldest first."""
    if len(bot_messages) == len(parts):
        return [{"op": "edit_msg", "name": name, "channel": channel_id, "id": m["id"],
                 "content": text, "file": file}
                for m, (text, file) in zip(bot_messages, parts) if not _current(m, (text, file))]
    return ([{"op": "delete_msg", "name": name, "channel": channel_id, "id": m["id"]} for m in bot_messages]
            + [{"op": "post_msg", "name": name, "channel": channel_id, "content": text, "file": file}
               for text, file in parts])


def multipart(payload, file, data, boundary):
    """multipart/form-data body: payload_json + files[0] (Discord "Uploading Files") -> (body, content type)."""
    ctype = MIME.guess_type(file["filename"])[0] or "application/octet-stream"
    b = boundary.encode()
    body = b"".join([
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload).encode(), b"\r\n",
        b"--" + b + b"\r\n",
        f'Content-Disposition: form-data; name="files[0]"; filename="{file["filename"]}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        data, b"\r\n",
        b"--" + b + b"--\r\n"])
    return body, f"multipart/form-data; boundary={boundary}"


def message_body(a, base):
    """JSON body for a content message. A file is listed as the only attachment; a file-less edit sends
    `attachments: []` so a stale file is dropped (PATCH keeps only the listed attachments)."""
    body = {"content": a["content"], "allowed_mentions": {"parse": []}, **base}
    if a["file"]:
        body["attachments"] = [{"id": 0, "filename": a["file"]["filename"]}]
    elif a["op"] == "edit_msg":
        body["attachments"] = []
    return body


def describe(a):
    kind = {TEXT: "text", VOICE: "voice", CATEGORY: "category"}
    op = a["op"]
    if op == "create":
        where = f" in {a['parent']}" if a["parent"] else ""
        return f"+ create {kind[a['type']]} {a['name']!r}{where}"
    if op == "patch":
        return f"~ patch {a['name']!r}: {', '.join(a['fields'])}"
    if op == "reorder":
        return f"~ reorder {a['name']}"
    if op == "guild":
        return f"~ guild {a['name']}"
    if op == "role":
        return f"~ role {a['name']}"
    att = f" + {a['file']['filename']} ({a['file']['size']} B)" if a.get("file") else ""
    if op == "edit_msg":
        return f"~ edit bot message in #{a['name']}{att}"
    if op == "delete_msg":
        return f"- delete bot message in #{a['name']}"
    return f"+ post message in #{a['name']} ({len(a['content'])} chars){att}"


# ── I/O ──────────────────────────────────────────────────────────────────────

DOTENV = HERE / ".env"  # separate from the Compose .env: the token must never reach the game container


def read_env():
    """discord/.env as a dict, with DISCORD_* overridable from the environment (secrets are file-only)."""
    env = {}
    if DOTENV.exists():
        for line in DOTENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                glued = next((g for g in ENV_KEYS if f"{g}=" in v), None)
                if glued:  # the value may be the token or the link: name the keys, never the value
                    sys.exit(f"discord/.env: the {k} line also contains {glued}= (an append onto a last line "
                             f"with no trailing newline). Split it back onto two lines; set the link with: "
                             f"{SHARE_LINK_CMD}")
                env[k] = v
    env.update({k: v for k, v in os.environ.items() if k.startswith("DISCORD_")})
    return env


def load_env():
    env = read_env()
    if not env.get("DISCORD_TOKEN"):
        sys.exit("missing DISCORD_TOKEN (discord/.env or environment)")
    return env


KIT_TOML = None  # None: <repo>/kit.toml or $MC_KIT_TOML (kit/kitconfig.py); the tests use kit/fixtures/example.toml


def load_kit_config():
    """kit.toml, validated. The guild id and the bridge bot id live there and nowhere else."""
    if str(REPO / "kit") not in sys.path:
        sys.path.insert(0, str(REPO / "kit"))  # kit/ only: the repo root would make `discord` this directory
    import kitconfig
    try:
        return kitconfig.load(KIT_TOML)
    except kitconfig.ConfigError as e:
        sys.exit(f"discord/sync.py: {e}")


def guild_id(kit, env):
    """kit.toml discord.guild_id. A DISCORD_GUILD_ID still in discord/.env (or the environment) must agree, or sync
    refuses: two homes that disagree would point the sync at a different guild than the bridge."""
    gid, legacy = kit["guild_id"], env.get("DISCORD_GUILD_ID")
    if legacy and legacy != gid:
        sys.exit("DISCORD_GUILD_ID in discord/.env (or the environment) differs from kit.toml discord.guild_id; "
                 "kit.toml is its home: delete the DISCORD_GUILD_ID line from discord/.env")
    return gid


def kit_bots(kit):
    return {"bridge": kit["bridge_bot_id"]}


class ApiError(SystemExit):
    """An HTTP failure. Uncaught it exits like sys.exit(message); a caller with a fallback (bootstrap's Message
    Content PATCH) catches it and reads .status."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, token):
        self.token = token

    def __call__(self, method, path, body=None, file=None):
        if file:  # multipart: body goes in payload_json, the file as files[0]
            data, ctype = multipart(body, file, file["path"].read_bytes(), uuid.uuid4().hex)
        else:
            data, ctype = (json.dumps(body).encode() if body is not None else None), "application/json"
        for _ in range(6):
            req = urllib.request.Request(API + path, data=data, method=method, headers={
                "Authorization": f"Bot {self.token}",
                "User-Agent": "DiscordBot (minecraft-ai-host discord/sync.py, 1)",
                "Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                body_txt = e.read()
                if e.code == 429:
                    time.sleep(float(json.loads(body_txt).get("retry_after", 1)) + 0.2)
                    continue
                # Upstream blips (seen live 2026-10-01: a 503 on a read). Only reads are retried: a write
                # that 5xx'd may still have landed, and the next sync's plan reconciles it instead.
                if e.code in (500, 502, 503, 504) and method == "GET":
                    time.sleep(2)
                    continue
                # redact BEFORE truncating: a cut inside a secret would leave a prefix redact() can't match
                shown = redact(body_txt.decode("utf-8", "replace"))[:300]
                raise ApiError(redact(f"{method} {path} -> HTTP {e.code}: {shown!r}"), e.code)
        raise ApiError(redact(f"{method} {path}: still failing after retries (rate limit or upstream 5xx)"))


def content_inputs(layout, env, today, cfg):
    """Resolve every {attach:}, {secret:} and {config:} in every content file up front, so a missing file, a bad
    secret or an unknown config name stops the sync before ANY action (pass 1 may create channels before their
    content is planned). -> (files, secrets, config, warnings)."""
    files, secrets, config, warnings = {}, {}, {}, []
    for cat in layout["categories"]:
        for ch in cat["channels"]:
            if ch["content"]:
                text = (HERE / "content" / ch["content"]).read_text()
                config.update(resolve_config(CONFIG_REF.findall(text), cfg))
                for name in SECRET.findall(text):
                    if name not in secrets:
                        secrets[name], w = resolve_secret(name, env, today)
                        warnings += w
                for part in split_parts(text):
                    name = take_attach(part)[1]
                    if name and name not in files:
                        files[name] = resolve_attachment(name)
    return files, secrets, config, warnings


def asus_node_facts():
    """-> (guild id, server address) of the asus-node instance, from its fixture (their one home), or None when this
    tree holds no instance record (kit/instance.py record(): a copy). With the record here, a fixture that cannot be
    read, parsed or validated (kit/kitconfig.py) is a defect, never an absence, and refuses; so does part of a
    record."""
    kitconfig = _kitconfig()  # and kit/ is on sys.path
    import instance
    try:
        if not instance.record(INSTANCE_REPO):
            return None
    except instance.InstanceError as e:
        raise ValueError(f"{e} (the guild profile cannot be checked)") from None
    try:
        cfg = kitconfig.load(ASUS_NODE_FIXTURE)
    except (OSError, ValueError) as e:  # kitconfig.ConfigError is a ValueError
        raise ValueError(f"cannot read the asus-node fixture ({ASUS_NODE_FIXTURE.name}: {e}), so the guild profile "
                         "cannot be checked; refusing") from None
    return cfg["discord"]["guild_id"], cfg["tailscale"]["server_address"]


def asus_node_only(path):
    """True for a file that is the asus-node guild's own text: discord/posts/, or discord/content/ outside kit/ and
    KIT_SHARED_CONTENT."""
    p, content = Path(path).resolve(), (HERE / "content").resolve()
    if p.is_relative_to(ASUS_NODE_POSTS.resolve()):
        return True
    if p.is_relative_to(content):
        rel = p.relative_to(content).as_posix()
        return not (rel.startswith("kit/") or rel in KIT_SHARED_CONTENT)
    return False


def profile_guard(cfg, layout=None, texts=()):
    """The P6 stop, before any API call. The asus-node profile is pinned to the asus-node guild; any other profile
    never targets that guild and never posts its content (layout channels resolving outside discord/content/kit/
    and KIT_SHARED_CONTENT) or its server address (in a content file, a one-off post or a {config:} value)."""
    profile, gid = cfg["discord"]["profile"], str(cfg["discord"]["guild_id"])
    facts = asus_node_facts()
    if profile == ASUS_NODE:
        if facts is None:
            raise ValueError('discord.profile = "asus-node" is the original server\'s, and its instance record '
                             "(kit/instance.py RECORD, listed in kit/INSTANCE-PATHS) is not in this tree, so it "
                             'cannot run here. A new guild uses discord.profile = "kit"')
        if gid != facts[0]:
            raise ValueError('discord.profile = "asus-node" posts the original server\'s story and history, and it '
                             "is pinned to that guild (kit/fixtures/asus-node.toml); this kit.toml names another "
                             'guild. A new guild uses discord.profile = "kit"')
        return
    asus_gid, asus_address = facts or (None, None)
    if gid == asus_gid:
        raise ValueError(f"discord.profile = {profile!r} but discord.guild_id is the asus-node guild's: a kit never "
                         "syncs that guild")
    for cat in (layout or {"categories": []})["categories"]:
        for ch in cat["channels"]:
            if ch["content"] and asus_node_only(HERE / "content" / ch["content"]):
                raise ValueError(f"#{ch['name']}: discord/content/{ch['content']} is the asus-node guild's own text; "
                                 f"a {profile!r} guild gets only discord/content/kit/ and "
                                 f"{', '.join(KIT_SHARED_CONTENT)} (sync.PROFILES)")
    if asus_address and any(asus_address.lower() in t.lower() for t in texts):  # a DNS name ignores case
        raise ValueError(f"the content holds the asus-node server's address; a {profile!r} guild never gets it "
                         "(kit.toml tailscale.server_address is this instance's own)")


def invite_exposure(guild, gid):
    """With the share link in #setup, a Discord invite is server access with no human in the loop
    (proposal 2026-10-02 item 3). For kit.toml discord.invite_lockdown = false: a warning, never a change (with
    lockdown on, plan_lockdown sets it instead). -> warning or None."""
    everyone = next((r for r in guild.get("roles") or [] if str(r.get("id")) == str(gid)), None)
    if everyone is None or "permissions" not in everyone:
        return "WARNING: could not read @everyone's permissions; whether members can create invites is unverified"
    if int(everyone["permissions"]) & CREATE_INVITE:
        return ("WARNING: @everyone can create invites, so anyone a member invites can read the share link in "
                "#setup and join (Tailscale share + /whitelist) with no human in the loop. kit.toml "
                "discord.invite_lockdown = false leaves it on; set it to true and sync turns it off")
    return None


def plan_lockdown(guild, gid):
    """kit.toml discord.invite_lockdown = true (operator ruling 2026-10-14): @everyone has no Create Invite, so the
    only way in is an invite an admin made. -> an action, or None when it is already off. @everyone's role id is the
    guild id. Unreadable permissions are a refusal: a "0 actions" over an unverified lockdown would be a false
    all-clear."""
    everyone = next((r for r in guild.get("roles") or [] if str(r.get("id")) == str(gid)), None)
    if everyone is None or "permissions" not in everyone:
        raise ValueError("could not read @everyone's permissions, so the invite lockdown (kit.toml "
                         "discord.invite_lockdown) cannot be verified")
    perms = int(everyone["permissions"])
    if not perms & CREATE_INVITE:
        return None
    return {"op": "role", "id": str(gid), "name": "@everyone: Create Invite off (kit.toml discord.invite_lockdown)",
            "fields": {"permissions": str(perms & ~CREATE_INVITE)}}  # every other bit kept


def full_plan(api, layout, gid, bot_id, files, secrets, config=None, lockdown=False):
    channels = api("GET", f"/guilds/{gid}/channels")
    guild = api("GET", f"/guilds/{gid}")
    actions, notes = plan(layout, channels, gid, bot_id, guild.get("system_channel_id"))
    if lockdown:
        role = plan_lockdown(guild, gid)
        if role:
            actions.append(role)
    cats = {c["id"]: c["name"] for c in channels if c["type"] == CATEGORY}
    ids_by_name = {c["name"]: c["id"] for c in channels if c["type"] == TEXT}
    pending = 0
    for cat in layout["categories"]:
        for ch in cat["channels"]:
            if not ch["content"]:
                continue
            cur = next((c for c in channels if c["type"] == TEXT and c["name"] == ch["name"]
                        and cats.get(c.get("parent_id")) == cat["name"]), None)
            if cur is None:
                pending += 1
                continue
            text = (HERE / "content" / ch["content"]).read_text()
            try:
                parts = split_parts(resolve_links(fill_secrets(fill_config(text, config or {}), secrets),
                                                  ids_by_name))
            except ValueError as e:  # a linked channel not created yet -> next pass
                if "unknown channel" in str(e):
                    pending += 1
                    continue
                raise
            parts = [(text, files[att] if att else None) for text, att in map(take_attach, parts)]
            msgs = api("GET", f"/channels/{cur['id']}/messages?limit=100")
            mine = [m for m in reversed(msgs) if m["author"]["id"] == bot_id]
            actions += plan_content(ch["name"], cur["id"], parts, mine)
    if pending:
        notes.append(f"{pending} content channel(s) planned once their channels exist (next pass)")
    return actions, notes, pending


def execute(api, gid, actions):
    cat_ids = {c["name"]: c["id"] for c in api("GET", f"/guilds/{gid}/channels") if c["type"] == CATEGORY}
    for a in actions:
        print(redact(describe(a)))
        op = a["op"]
        if op == "create":
            body = {"name": a["name"], "type": a["type"], "position": a["position"],
                    "permission_overwrites": a["overwrites"]}
            if a["topic"]:
                body["topic"] = a["topic"]
            if a["parent"]:
                body["parent_id"] = cat_ids[a["parent"]]
            made = api("POST", f"/guilds/{gid}/channels", body)
            if a["type"] == CATEGORY:
                cat_ids[a["name"]] = made["id"]
        elif op == "patch":
            api("PATCH", f"/channels/{a['id']}", a["fields"])
        elif op == "reorder":
            api("PATCH", f"/guilds/{gid}/channels", a["positions"])
        elif op == "guild":
            api("PATCH", f"/guilds/{gid}", a["fields"])
        elif op == "role":
            api("PATCH", f"/guilds/{gid}/roles/{a['id']}", a["fields"])
        elif op == "edit_msg":
            api("PATCH", f"/channels/{a['channel']}/messages/{a['id']}", message_body(a, {}), a["file"])
        elif op == "delete_msg":
            api("DELETE", f"/channels/{a['channel']}/messages/{a['id']}")
        elif op == "post_msg":
            api("POST", f"/channels/{a['channel']}/messages",
                message_body(a, {"flags": SUPPRESS_NOTIFICATIONS}), a["file"])


def cmd_sync(api, gid, apply, env, today=None, bots=None, cfg=None):
    """Every content input is resolved before the first API call: a refusal touches nothing.
    cfg: the validated kit.toml (None: load it)."""
    cfg = load_kit_config() if cfg is None else cfg
    lockdown = cfg["discord"]["invite_lockdown"]
    layout = load_layout((HERE / "layout.toml").read_text(), kit_bots(cfg["discord"]) if bots is None else bots,
                         cfg["discord"]["profile"])
    try:
        profile_guard(cfg, layout)  # the guild and the layout's files first: an absent record refuses by name
        files, secrets, config, warnings = content_inputs(layout, env, today or datetime.date.today(), cfg)
        texts = [(HERE / "content" / ch["content"]).read_text()
                 for c in layout["categories"] for ch in c["channels"] if ch["content"]]
        profile_guard(cfg, layout, texts + list(config.values()))
    except ValueError as e:
        sys.exit(redact(f"error: {e}"))
    for w in warnings:
        print(w if w.startswith("WARNING") else f"  note: {w}")
    bot_id = api("GET", "/users/@me")["id"]
    if "share_link" in secrets and not lockdown:
        warning = invite_exposure(api("GET", f"/guilds/{gid}"), gid)
        if warning:
            print(warning)
    for n in range(4):
        try:
            actions, notes, pending = full_plan(api, layout, gid, bot_id, files, secrets, config, lockdown)
        except ValueError as e:
            sys.exit(redact(f"error: {e}"))
        for note in notes:
            print(redact(f"  note: {note}"))
        if not apply or (not actions and not pending):
            for a in ([] if apply else actions):
                print(redact(describe(a)))
            print(f"{len(actions)} actions" + ("" if apply else " (dry run; --apply to execute)"))
            return 0 if (apply or not actions) else 2
        print(f"-- pass {n + 1}: applying {len(actions)} actions")
        execute(api, gid, actions)
    sys.exit("did not converge after 4 passes")


def cmd_post(api, gid, channel, file, apply, cfg=None):
    text = Path(file).read_text()
    if SECRET.search(text):  # checked before anything is printed: a one-off post echoes its text
        sys.exit("{secret:} is only supported in managed content (discord/content/), not one-off posts")
    cfg = load_kit_config() if cfg is None else cfg
    try:  # {config:} is not secret: a one-off post may use it; resolved before any API call
        if cfg["discord"]["profile"] != ASUS_NODE and asus_node_only(file):
            raise ValueError(f"{file} is the asus-node guild's own text (discord/posts/, or discord/content/ outside "
                             f"kit/); a {cfg['discord']['profile']!r} guild never gets it (sync.profile_guard). A "
                             "kit's own one-off post goes from any other path, e.g. posts/<file>.md")
        text = fill_config(text, resolve_config(CONFIG_REF.findall(text), cfg))
        profile_guard(cfg, texts=[text])
    except ValueError as e:
        sys.exit(f"error: {e}")
    channels = api("GET", f"/guilds/{gid}/channels")
    ids = {c["name"]: c["id"] for c in channels if c["type"] == TEXT}
    if channel not in ids:
        sys.exit(f"no text channel {channel!r}")
    parts = split_parts(resolve_links(text, ids))
    if any(take_attach(p)[1] for p in parts):
        sys.exit("{attach:} is only supported in managed content (discord/content/), not one-off posts")
    for p in parts:
        print(f"+ post to #{channel} ({len(p)} chars)\n{p}\n")
        if apply:
            api("POST", f"/channels/{ids[channel]}/messages",
                {"content": p, "allowed_mentions": {"parse": []}})
    print("posted" if apply else "dry run; --apply to post")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("post", nargs="*", help="post <channel> <file>: one-off, unmanaged message")
    args = ap.parse_args()
    env = load_env()
    cfg = load_kit_config()
    gid = guild_id(cfg["discord"], env)  # before the client exists: a refusal makes no call at all
    api = Api(env["DISCORD_TOKEN"])
    if args.post:
        if len(args.post) != 3 or args.post[0] != "post":
            ap.error("usage: post <channel> <file>")
        return cmd_post(api, gid, args.post[1], args.post[2], args.apply, cfg)
    return cmd_sync(api, gid, args.apply, env, bots=kit_bots(cfg["discord"]), cfg=cfg)


if __name__ == "__main__":
    sys.exit(main())
