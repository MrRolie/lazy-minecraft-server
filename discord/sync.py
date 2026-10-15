#!/usr/bin/env python3
"""Sync the friend Discord guild to discord/layout.toml + discord/content/*.md.

Dry run by default (prints every action); --apply executes, then re-plans until
converged. Idempotent: a dry run after --apply must print "0 actions".
Never deletes a channel (unlisted ones are reported). Deletes only the bot's OWN
messages, and only in content-managed channels.

  python3 discord/sync.py                      # dry run
  python3 discord/sync.py --apply
  python3 discord/sync.py post announcements <post>.md [--apply]

Content: parts split on a line containing only `---`, each <=2000 chars.
`{#name}` in content resolves to a clickable link to that channel.
A line `{attach:name}` attaches one named local file (ATTACHMENTS) to that part
and is removed from the posted text. `pack` = the .mrpack docker-compose.yml runs.
`{secret:name}` anywhere in content is filled at sync time from discord/.env (SECRETS); the value is
never printed. `share_link` = TAILSCALE_SHARE_LINK (+ optional TAILSCALE_SHARE_LINK_DATE, YYYY-MM-DD).
Env (discord/.env or environment): DISCORD_TOKEN, DISCORD_GUILD_ID. Stdlib only.
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
SHARE_LINK = re.compile(r"^https://login\.tailscale\.com/admin/invite/[A-Za-z0-9]+$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # fromisoformat alone also takes 20261002
SHARE_LINK_WARN_DAYS = 25  # Tailscale: unused share links expire after 30 days
# Appending to a discord/.env whose last line has no newline glues the new key onto it; the leading \n
# prevents that. Appending also avoids opening a file that holds the Claudius token.
ENV_APPEND = ("printf '\\nTAILSCALE_SHARE_LINK=<link>\\nTAILSCALE_SHARE_LINK_DATE=<YYYY-MM-DD>\\n' "
              ">> discord/.env  (keep it mode 600)")
ENV_KEYS = ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "TAILSCALE_SHARE_LINK", "TAILSCALE_SHARE_LINK_DATE")
MODPACK = re.compile(r'MODRINTH_MODPACK: *"/pack/([^"]+)"')  # same parse as deploy/backup.sh
MIME = mimetypes.MimeTypes()  # built-in table only: content types must not vary with the host's /etc/mime.types


# ── pure planning ────────────────────────────────────────────────────────────

def load_layout(text):
    data = tomllib.loads(text)
    cats = []
    for c in data["category"]:
        ro = c.get("read_only", False)
        chans = [{"type": "text", "topic": "", "read_only": ro, "content": None, "renamed_from": [], **ch}
                 for ch in c.get("channel", [])]
        for ch in chans:
            if not isinstance(ch["renamed_from"], list):
                raise ValueError(f"{ch['name']}: renamed_from must be a list of old names")
        extra = c.get("extra_bots", [])
        if not (isinstance(extra, list) and all(isinstance(b, str) and b.isdigit() for b in extra)):
            raise ValueError(f"{c['name']}: extra_bots must be a list of bot user ids (digit strings)")
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
    return actions, notes


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


def pack_filename(compose_text):
    """The .mrpack docker-compose.yml runs: MODRINTH_MODPACK is the one home of "which pack is live"."""
    m = MODPACK.search(compose_text)
    if not m:
        raise ValueError("docker-compose.yml has no MODRINTH_MODPACK: \"/pack/<file>\"")
    return m.group(1)


ATTACHMENTS = {"pack": lambda repo: repo / "dist" / pack_filename((repo / "docker-compose.yml").read_text())}


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
                         f"append it without opening the file: {ENV_APPEND}")
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
                             f"with no trailing newline). Split it back onto two lines; append with: {ENV_APPEND}")
                env[k] = v
    env.update({k: v for k, v in os.environ.items() if k.startswith("DISCORD_")})
    return env


def load_env():
    env = read_env()
    missing = [k for k in ("DISCORD_TOKEN", "DISCORD_GUILD_ID") if not env.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} (discord/.env or environment)")
    return env


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
                sys.exit(redact(f"{method} {path} -> HTTP {e.code}: {shown!r}"))
        sys.exit(redact(f"{method} {path}: still failing after retries (rate limit or upstream 5xx)"))


def content_inputs(layout, env, today):
    """Resolve every {attach:} and {secret:} in every content file up front, so a missing file or a bad
    secret stops the sync before ANY action (pass 1 may create channels before their content is planned).
    -> (files, secrets, warnings)."""
    files, secrets, warnings = {}, {}, []
    for cat in layout["categories"]:
        for ch in cat["channels"]:
            if ch["content"]:
                text = (HERE / "content" / ch["content"]).read_text()
                for name in SECRET.findall(text):
                    if name not in secrets:
                        secrets[name], w = resolve_secret(name, env, today)
                        warnings += w
                for part in split_parts(text):
                    name = take_attach(part)[1]
                    if name and name not in files:
                        files[name] = resolve_attachment(name)
    return files, secrets, warnings


def invite_exposure(guild, gid):
    """With the share link in #setup, a Discord invite is server access with no human in the loop
    Read-only: role permissions are not managed here. -> warning or None."""
    everyone = next((r for r in guild.get("roles") or [] if str(r.get("id")) == str(gid)), None)
    if everyone is None or "permissions" not in everyone:
        return "WARNING: could not read @everyone's permissions; whether members can create invites is unverified"
    if int(everyone["permissions"]) & CREATE_INVITE:
        return ("WARNING: @everyone can create invites, so anyone a member invites can read the share link in "
                "#setup and join (Tailscale share + /whitelist) with no human in the loop. Decide: "
                "accept it, or turn off Create Invite for @everyone by hand (sync reads role permissions, "
                "never sets them)")
    return None


def full_plan(api, layout, gid, bot_id, files, secrets):
    channels = api("GET", f"/guilds/{gid}/channels")
    guild = api("GET", f"/guilds/{gid}")
    actions, notes = plan(layout, channels, gid, bot_id, guild.get("system_channel_id"))
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
                parts = split_parts(resolve_links(fill_secrets(text, secrets), ids_by_name))
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
        elif op == "edit_msg":
            api("PATCH", f"/channels/{a['channel']}/messages/{a['id']}", message_body(a, {}), a["file"])
        elif op == "delete_msg":
            api("DELETE", f"/channels/{a['channel']}/messages/{a['id']}")
        elif op == "post_msg":
            api("POST", f"/channels/{a['channel']}/messages",
                message_body(a, {"flags": SUPPRESS_NOTIFICATIONS}), a["file"])


def cmd_sync(api, gid, apply, env, today=None):
    """Every content input is resolved before the first API call: a refusal touches nothing."""
    layout = load_layout((HERE / "layout.toml").read_text())
    try:
        files, secrets, warnings = content_inputs(layout, env, today or datetime.date.today())
    except ValueError as e:
        sys.exit(redact(f"error: {e}"))
    for w in warnings:
        print(w if w.startswith("WARNING") else f"  note: {w}")
    bot_id = api("GET", "/users/@me")["id"]
    if "share_link" in secrets:
        warning = invite_exposure(api("GET", f"/guilds/{gid}"), gid)
        if warning:
            print(warning)
    for n in range(4):
        try:
            actions, notes, pending = full_plan(api, layout, gid, bot_id, files, secrets)
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


def cmd_post(api, gid, channel, file, apply):
    channels = api("GET", f"/guilds/{gid}/channels")
    ids = {c["name"]: c["id"] for c in channels if c["type"] == TEXT}
    if channel not in ids:
        sys.exit(f"no text channel {channel!r}")
    text = Path(file).read_text()
    if SECRET.search(text):  # checked before anything is printed: a one-off post echoes its text
        sys.exit("{secret:} is only supported in managed content (discord/content/), not one-off posts")
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
    api, gid = Api(env["DISCORD_TOKEN"]), env["DISCORD_GUILD_ID"]
    if args.post:
        if len(args.post) != 3 or args.post[0] != "post":
            ap.error("usage: post <channel> <file>")
        return cmd_post(api, gid, args.post[1], args.post[2], args.apply)
    return cmd_sync(api, gid, args.apply, env)


if __name__ == "__main__":
    sys.exit(main())
