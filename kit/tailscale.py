#!/usr/bin/env python3
"""P3 of the kit (docs/kit/KIT.md): the Tailscale side, through the API. Stdlib only.

  python3 kit/tailscale.py status            no credential: derive host_name and server_address from
                                             `tailscale status --json` and print them for kit.toml [tailscale]
  python3 kit/tailscale.py setup             dry run: reads the policy and devices, validates the merged policy,
                                             prints every change; writes nothing
  python3 kit/tailscale.py setup --apply     do it, check every gate, then delete the token file

What `setup --apply` does, in order. A step whose result is already in place is skipped, so a re-run is safe:
  1. Policy: GET the policy file, merge in the kit's part (merge_policy), check it with the validate endpoint,
     then POST it with If-Match (the ETag of that GET). A 412 means someone changed it in between: GET, merge and
     validate again, once. Before the POST, the old policy is read again as HuJSON (its comments included) and saved
     under ~/.config/mc-kit/ as tailscale-policy-<time>.hujson, mode 600: paste it into the console's policy editor to
     restore it exactly.
  2. Tag the device, keeping any tags it already has, then disable its key expiry.
  3. Create a multi-use device invite (the share link friends accept), unless the link already in discord/.env is
     still one of this device's multi-use invites. Write it to discord/.env as TAILSCALE_SHARE_LINK plus
     TAILSCALE_SHARE_LINK_DATE (render.merge_env under render.env_lock: an existing key is rewritten in place, so
     rotation never leaves two copies; kit/secret.py share-link uses this same writer for a link made by hand).
  4. Gates, read back live: the merged policy needs no further change, the device shows the tag and key expiry
     disabled, `tailscale status --json` shows the tag, and sync.py's own reader accepts the link in discord/.env.
  5. Delete the token file and print the step to revoke the token in the console (design decision, 2026-10-14:
     use the token once). On any failure the file is kept, so a re-run needs no new paste.

The token: a user API access token (tskey-api-...), which the person makes at console -> Settings -> Keys and hands
over with `python3 kit/secret.py tailscale` (a hidden prompt; it checks the shape and writes the file). It must be
a regular file of their own, mode 600. Its value and the share link are never printed: every message goes through
redact(). An OAuth client token (tskey-client-) is refused, because it cannot create device invites.

Sources. Every endpoint and payload comes from the Tailscale API OpenAPI spec, fetched 2026-10-14 from
https://api.tailscale.com/api/v2?outputOpenapiSchema=true (the spec behind https://tailscale.com/api; YAML, sha256
b3532da7...ac38):
  GET  /tailnet/-/acl                  Accept picks JSON or HuJSON (application/hujson keeps the comments);
                                       the response carries an ETag
  POST /tailnet/-/acl/validate         "an empty response body implies passing"; a failure is a 200 with
                                       {"message", "data"}, so the body is checked, not the status
  POST /tailnet/-/acl                  If-Match = that ETag; 412 "If-Match hash mismatch"
  GET  /tailnet/-/devices              {"devices": [...]}; each device's `name` is its MagicDNS name, `nodeId`
                                       the preferred {deviceId}
  GET  /device/{id}                    tags, keyExpiryDisabled
  POST /device/{id}/tags               {"tags": [...]} sets the whole list
  POST /device/{id}/key                {"keyExpiryDisabled": true}
  GET  /device/{id}/device-invites     [DeviceInvite]
  POST /device/{id}/device-invites     [{"multiUse": true, "allowExitNode": false}] -> [DeviceInvite], inviteUrl
                                       https://login.tailscale.com/admin/invite/<code>
  Auth: "Authorization: Bearer tskey-api-..." (spec, Authentication).
The policy rules: `*` as a src also admits autogroup:shared, so the default allow-all is narrowed to
autogroup:member + autogroup:tagged; autogroup:shared is src-only; a tag must be in tagOwners before it is used;
`[]` is short for autogroup:admin; autogroup:member and the legacy autogroup:members cannot both appear; acls accept
the legacy users/ports field names (tailscale.com/docs/reference/syntax/policy-file).

Where this differs from the research or the task brief:
  - The device is found by its `name`. The research said the devices list returns `fqdn`; in the spec, `fqdn` exists only
    in the invite-accept response.
  - The policy is merged and written as JSON (Accept: application/json): stdlib has no HuJSON parser, so the comments
    in the old policy do not survive the POST. The backup is the HuJSON text itself, so nothing is lost for good.

It has run against the real API once: the kit proof run of 2026-10-16, on a fresh tailnet
(docs/kit/2026-10-16_proof-run-findings.md). The tests (kit/test_tailscale.py) use a fake HTTP server built from
the spec.
"""
import argparse
import copy
import datetime
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:  # kit/ only, never the repo root (its discord/ would shadow discord.py)
    sys.path.insert(0, str(HERE))
import kitconfig  # noqa: E402
import render  # noqa: E402

API = "https://api.tailscale.com/api/v2"
CONFIG_DIR = Path.home() / ".config" / "mc-kit"
TOKEN_FILE = CONFIG_DIR / "tailscale-api-token"
DOTENV = REPO / "discord" / ".env"
KIT_TOML = None  # None: kitconfig's default (<repo>/kit.toml or $MC_KIT_TOML)
DEFAULT_TAG = "tag:mc-server"  # a kit constant
PORT = 25565  # a kit constant
REVOKE_URL = "https://login.tailscale.com/admin/settings/keys"
TOKEN_RX = re.compile(r"tskey-api-[A-Za-z0-9_-]+")
INVITE_RX = re.compile(r"https://login\.tailscale\.com/admin/invite/[A-Za-z0-9]+")
STATUS_WAIT_S = 30  # how long the local client gets to show a tag set through the API


class TsError(Exception):
    pass


# ---- the secrets ---------------------------------------------------------------------------------------------

def redact(text):
    """Every token and share link out of a message, by shape. read_token admits only a token TOKEN_RX matches whole,
    so the shape is enough."""
    return INVITE_RX.sub("<share_link>", TOKEN_RX.sub("<token>", str(text)))


def read_token(path):
    """The API token from its file. Refuses a link, a non-file, another user's file, any group or other permission,
    and anything not shaped like a user API token. No message carries the file's contents."""
    path = Path(path)
    if path.is_symlink():
        raise TsError(f"{path} is a symlink; the token must be a plain file of your own")
    try:
        st = path.stat()
    except FileNotFoundError:
        raise TsError(f"{path} is missing. Make an API access token at console -> Settings -> Keys "
                      f"({REVOKE_URL}) and save it there without showing it: python3 kit/secret.py tailscale") \
            from None
    if not stat.S_ISREG(st.st_mode):
        raise TsError(f"{path} is not a regular file")
    if st.st_uid != os.geteuid():
        raise TsError(f"{path} is owned by uid {st.st_uid}, not you ({os.geteuid()})")
    if st.st_mode & 0o077:
        raise TsError(f"{path} is mode {oct(st.st_mode & 0o777)[2:]}; it holds an owner-level credential, so it "
                      f"must be 600: chmod 600 {path}")
    value = path.read_text().strip()
    if value.startswith("tskey-client-"):
        raise TsError(f"{path} holds an OAuth client secret. Device invites need a user API access token "
                      "(tskey-api-...): OAuth clients cannot create them (API spec, createDeviceInvites)")
    if not TOKEN_RX.fullmatch(value):
        raise TsError(f"{path} does not hold a Tailscale API access token (tskey-api-...); contents not shown")
    return value


# ---- HTTP -------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies the Authorization header onto a redirect, even to another host. The API never needs one.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TsError(f"{req.get_method()} {urllib.parse.urlsplit(req.full_url).path}: refused a redirect "
                      f"(HTTP {code}); the token is never sent anywhere else")


class Api:
    """A Tailscale API client. call() -> (status, headers, parsed body or None)."""

    def __init__(self, token, base=None):
        self.base = (base or API).rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost"):
            raise TsError(f"refusing a non-https API base {self.base}: the token would cross the network in clear")
        self.token = token
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(self, method, path, body=None, headers=None, ok=(200,), raw=False):
        """raw=True: the body as text, unparsed (the HuJSON policy)."""
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                "User-Agent": "minecraft-ai-host kit/tailscale.py"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        for attempt in range(4):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
            try:
                with self.opener.open(req, timeout=30) as r:
                    body_in = r.read()
                    return r.status, r.headers, (body_in.decode("utf-8") if raw else _parse(body_in, method, path))
            except urllib.error.HTTPError as e:
                raw = e.read()
                if e.code in ok:
                    return e.code, e.headers, _parse(raw, method, path)
                if e.code == 429 and attempt < 3:
                    time.sleep(min(float(e.headers.get("Retry-After") or 2), 30))
                    continue
                # Only reads are retried: a write that 5xx'd may still have landed.
                if e.code in (500, 502, 503, 504) and method == "GET" and attempt < 3:
                    time.sleep(2)
                    continue
                # _message redacts the body; the rest holds no secret, and main() redacts every TsError it prints
                raise TsError(f"{method} {path} -> HTTP {e.code}: {_message(raw)}{_hint(e.code)}") from None
            except urllib.error.URLError as e:
                raise TsError(f"{method} {path}: {e.reason}") from None
        raise TsError(f"{method} {path}: still failing after retries")


def _parse(raw, method, path):
    if not raw.strip():
        return None  # tags and key return no body
    try:
        return json.loads(raw)
    except ValueError:
        raise TsError(f"{method} {path}: the response is not JSON") from None


def _message(raw):
    try:
        msg = json.loads(raw).get("message")
    except (ValueError, AttributeError):
        msg = None
    # redact BEFORE truncating: a cut inside a secret would leave a prefix redact() cannot match
    return redact(msg if isinstance(msg, str) else raw.decode("utf-8", "replace"))[:300]


def _hint(code):
    if code == 401:
        return " (the token was refused: expired or revoked? make a new one at " + REVOKE_URL + ")"
    if code == 403:
        return " (the token's user lacks the permission: it must be the tailnet's Owner or an Admin)"
    return ""


# ---- the policy merge (pure) ------------------------------------------------------------------------------------

SHARED = "autogroup:shared"


def kit_acl(tag):
    return {"action": "accept", "src": [SHARED], "proto": "tcp", "dst": [f"{tag}:{PORT}"]}


def kit_grant(tag):
    # the rule AGENTS.md "Access" records
    return {"src": [SHARED], "dst": [tag], "ip": [f"tcp:{PORT}"]}


def _rules(policy, section):
    rules = policy.get(section, [])
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise TsError(f"policy: {section!r} is not a list of rules; the kit will not edit it")
    return rules


def _sources(rule, section, i):
    keys = [k for k in ("src", "users") if k in rule] if section == "acls" else ["src"]
    if len(keys) != 1 or not isinstance(rule.get(keys[0]), list):
        raise TsError(f"policy {section}[{i}]: no single src list; the kit will not edit it")
    return keys[0], rule[keys[0]]


def _is_allow_all(rule, section, src_key):
    if rule[src_key] != ["*"]:
        return False
    if section == "acls":
        dst_key = "dst" if src_key == "src" else "ports"
        return rule.get("action") == "accept" and rule.get(dst_key) == ["*:*"] and \
            set(rule) == {"action", src_key, dst_key}
    return rule.get("dst") == ["*"] and rule.get("ip") == ["*"] and set(rule) == {"src", "dst", "ip"}


def merge_policy(policy, tag=DEFAULT_TAG):
    """-> (new policy, [change descriptions]). The input is not modified. An empty list means nothing to do.

    The kit's part of the policy:
      - tagOwners has `tag` (added as ["autogroup:admin"] when absent; existing owners are kept)
      - the default allow-all rule (src "*", any destination) admits autogroup:shared too, so its src becomes
        autogroup:member + autogroup:tagged (the legacy autogroup:members when the policy already uses it)
      - friends (autogroup:shared) reach `tag` on tcp:25565 and nothing else
    Refused, naming the rule: any other rule with "*" or autogroup:danger-all in its src, and any rule for
    autogroup:shared that is not exactly the kit's. Rewriting a rule the kit did not write could take access away
    from someone; leaving one would let friends past port 25565."""
    if not isinstance(policy, dict):
        raise TsError("policy: not a JSON object")
    new, changes = copy.deepcopy(policy), []
    member = "autogroup:members" if '"autogroup:members"' in json.dumps(policy) else "autogroup:member"
    present = False
    for section, kit_rule in (("acls", kit_acl(tag)), ("grants", kit_grant(tag))):
        for i, rule in enumerate(_rules(new, section)):
            src_key, sources = _sources(rule, section, i)
            if rule == kit_rule:
                present = True
                continue
            if SHARED in sources:
                raise TsError(f"policy {section}[{i}] already gives {SHARED} (friends) access that is not the kit's "
                              f"rule ({json.dumps(kit_rule)}); change it by hand, then re-run")
            if "autogroup:danger-all" in sources:
                raise TsError(f"policy {section}[{i}]: src autogroup:danger-all would let friends past port {PORT}; "
                              "narrow it by hand, then re-run")
            if "*" in sources:
                if not _is_allow_all(rule, section, src_key):
                    raise TsError(f'policy {section}[{i}]: src "*" also admits {SHARED} (friends), and this is not '
                                  f"the default allow-all, so the kit will not rewrite it. Replace \"*\" with "
                                  f"{member} + autogroup:tagged (or narrower) by hand, then re-run")
                rule[src_key] = [member, "autogroup:tagged"]
                changes.append(f'{section}[{i}]: the allow-all src "*" -> {member} + autogroup:tagged '
                               f"(friends no longer match it)")
    if not present:
        # grants is the form Tailscale's docs now lead with; acls only when that is all the policy uses
        section = "acls" if "acls" in new and "grants" not in new else "grants"
        rule = kit_acl(tag) if section == "acls" else kit_grant(tag)
        new.setdefault(section, []).append(rule)
        changes.append(f"{section}: + {json.dumps(rule)}")
    owners = new.setdefault("tagOwners", {})
    if not isinstance(owners, dict):
        raise TsError("policy: tagOwners is not an object; the kit will not edit it")
    if tag not in owners:
        owners[tag] = ["autogroup:admin"]
        changes.append(f'tagOwners: + "{tag}": ["autogroup:admin"]')
    return new, changes


# ---- local status -----------------------------------------------------------------------------------------------

def local_status(run=subprocess.run):
    """`tailscale status --json` -> {host_name, server_address, node_id, tags}. Refuses unless logged in and MagicDNS
    is on: friends reach a shared machine only by its MagicDNS name."""
    try:
        r = run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise TsError("tailscale is not installed (KIT.md P2)") from None
    if r.returncode != 0:
        raise TsError(f"tailscale status --json exited {r.returncode}: {r.stderr.strip()[:200]}")
    try:
        st = json.loads(r.stdout)
    except ValueError:
        raise TsError("tailscale status --json: not JSON") from None
    if st.get("BackendState") != "Running":
        raise TsError(f"tailscale is not logged in (BackendState {st.get('BackendState')!r}); run "
                      "`sudo tailscale up --qr` and finish the login first (HUMAN-STEPS step 17)")
    if not (st.get("CurrentTailnet") or {}).get("MagicDNSEnabled"):
        raise TsError("MagicDNS is off for this tailnet; friends reach a shared machine only by its MagicDNS name. "
                      "Turn it on at console -> DNS")
    me = st.get("Self") or {}
    address = (me.get("DNSName") or "").rstrip(".")
    _check_kit("server_address", address)
    host = address.split(".")[0]
    _check_kit("host_name", host)
    return {"host_name": host, "server_address": address, "node_id": me.get("ID"), "tags": me.get("Tags") or []}


def _check_kit(key, value):
    """kit.toml's own check for a [tailscale] key (kitconfig.SCHEMA is its one home)."""
    check = kitconfig.SCHEMA["tailscale"][key][2]
    try:
        check(value)
    except kitconfig.ConfigError as e:
        raise TsError(f"tailscale.{key} {value!r}: {e}") from None


def kit_tailscale(path=None):
    """kit.toml's [tailscale] table if kit.toml exists, else {}. Read raw: at P3 the rest of kit.toml may not be
    filled in yet, so only these keys are checked."""
    f = kitconfig.path(path or KIT_TOML)
    if not f.exists():
        return {}
    try:
        table = tomllib.loads(f.read_text()).get("tailscale", {})
    except tomllib.TOMLDecodeError as e:
        raise TsError(f"{f}: not valid TOML: {e}") from None
    return table if isinstance(table, dict) else {}


# ---- discord/.env ------------------------------------------------------------------------------------------------

def _sync():
    """discord/sync.py, loaded by path (the repo root on sys.path would make `discord` that directory). The share-link
    regex and the reader that will consume what this writes live there."""
    spec = importlib.util.spec_from_file_location("_kit_discord_sync", REPO / "discord" / "sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def current_link(env_path):
    """The TAILSCALE_SHARE_LINK in discord/.env, or None. The rest of the file is not looked at."""
    text = render._read_secret_file(Path(env_path), writing=False)
    for line in text.splitlines():
        if line.startswith("TAILSCALE_SHARE_LINK="):
            return line.split("=", 1)[1].strip()
    return None


def write_link(env_path, link, made):
    """Set TAILSCALE_SHARE_LINK and TAILSCALE_SHARE_LINK_DATE in discord/.env: every other byte is kept, the write is
    atomic at mode 600, and a symlinked, foreign-owned or ambiguous file is refused (render.merge_env)."""
    if not _sync().SHARE_LINK.match(link):
        raise TsError("the API returned an invite that is not a https://login.tailscale.com/admin/invite/<code> link; "
                      "nothing was written")
    path = Path(env_path)
    try:
        with render.env_lock(path):  # no other kit writer between this read and the replace
            old = render._read_secret_file(path)
            new = render.merge_env(old, {"TAILSCALE_SHARE_LINK": link,
                                         "TAILSCALE_SHARE_LINK_DATE": made.isoformat()}, "discord/.env")
            if new != old or not path.exists():
                render.write_atomic(path, new, 0o600)
            elif path.stat().st_mode & 0o777 != 0o600:
                os.chmod(path, 0o600)
    except render.RenderError as e:
        raise TsError(redact(f"discord/.env: {e}")) from None


def read_back_link(env_path, today):
    """What sync.py will read from discord/.env, through its own reader and checks."""
    sync = _sync()
    sync.DOTENV = Path(env_path)
    try:
        link, _ = sync.share_link(sync.read_env(), today)
    except (ValueError, SystemExit) as e:
        raise TsError(redact(f"discord/.env read back through sync.py: {e}")) from None
    return link


# ---- the steps -------------------------------------------------------------------------------------------------

class Ts:
    """The API steps, each skipped when its result is already in place."""

    def __init__(self, api, out=print):
        self.api, self.out = api, out

    def policy(self):
        _, headers, body = self.api.call("GET", "/tailnet/-/acl")
        if not isinstance(body, dict):
            raise TsError("GET /tailnet/-/acl: not a JSON policy object")
        return body, headers.get("ETag")

    def policy_text(self):
        """The policy file as HuJSON, comments and all: the backup. Never parsed (stdlib has no HuJSON parser)."""
        _, _, text = self.api.call("GET", "/tailnet/-/acl", headers={"Accept": "application/hujson"}, raw=True)
        if not text.strip():
            raise TsError("GET /tailnet/-/acl as HuJSON: empty, so there is nothing to back up; refusing to write")
        return text

    def validate(self, policy):
        _, _, body = self.api.call("POST", "/tailnet/-/acl/validate", policy)
        if body:  # 200 either way: an empty body (or {}) passes, {"message", "data"} fails
            detail = json.dumps(body.get("data")) if isinstance(body, dict) else json.dumps(body)
            msg = body.get("message") if isinstance(body, dict) else None
            raise TsError(redact(f"the merged policy failed Tailscale's validation: {msg or 'see data'}: "
                                 f"{detail[:600]}"))

    def apply_policy(self, tag, apply, backup_dir):
        """-> the changes made (or, dry run, planned). Validates whenever there is a change, in a dry run too."""
        for attempt in range(2):
            policy, etag = self.policy()
            new, changes = merge_policy(policy, tag)
            if not changes:
                return []
            self.validate(new)
            if not apply:
                return changes
            if not etag:
                raise TsError("GET /tailnet/-/acl returned no ETag, so the write could overwrite a newer policy; "
                              "refusing (set the policy in the console instead)")
            # the backup is the HuJSON text, read after the JSON one: if it changed in between, the POST's If-Match 412s
            backup = save_backup(backup_dir, self.policy_text())
            self.out(f"  saved the old policy to {backup}")
            status, _, _ = self.api.call("POST", "/tailnet/-/acl", new, {"If-Match": etag}, ok=(200, 412))
            if status == 200:
                return changes
            self.out("  the policy changed since it was read (HTTP 412); reading it again")
        raise TsError("the policy changed under the kit twice in a row (HTTP 412). Is someone editing it in the "
                      "console? Re-run when they are done")

    def device(self, address, node_id):
        _, _, body = self.api.call("GET", "/tailnet/-/devices")
        devices = body.get("devices") if isinstance(body, dict) else None
        if not isinstance(devices, list):
            raise TsError("GET /tailnet/-/devices: no devices list")
        found = [d for d in devices if isinstance(d, dict) and (d.get("name") or "").rstrip(".") == address]
        if len(found) != 1:
            raise TsError(f"{len(found)} devices in the tailnet are named {address} (want exactly 1)")
        d = found[0]
        dev_id = d.get("nodeId") or d.get("id")
        if not dev_id:
            raise TsError(f"the device {address} has no nodeId or id")
        if node_id and d.get("nodeId") and d["nodeId"] != node_id:
            raise TsError(f"the API's device {address} is {d['nodeId']}, but this machine is {node_id}; "
                          "refusing to tag or share a different machine")
        return str(dev_id)

    def get_device(self, dev_id):
        _, _, d = self.api.call("GET", f"/device/{dev_id}")
        if not isinstance(d, dict):
            raise TsError(f"GET /device/{dev_id}: not a device")
        return d

    def tag(self, dev_id, tag, apply):
        tags = self.get_device(dev_id).get("tags") or []
        if tag in tags:
            return None
        if apply:  # the POST sets the whole list: keep what is there
            self.api.call("POST", f"/device/{dev_id}/tags", {"tags": tags + [tag]})
        return f"tag the device {tag}"

    def key_expiry(self, dev_id, apply):
        if self.get_device(dev_id).get("keyExpiryDisabled") is True:
            return None
        if apply:
            self.api.call("POST", f"/device/{dev_id}/key", {"keyExpiryDisabled": True})
        return "disable the device's key expiry"

    def invite(self, dev_id, have, apply):
        """-> (link or None, created: bool). Keeps `have` (the link in discord/.env) while it is one of the device's
        multi-use invites; otherwise creates one."""
        _, _, invites = self.api.call("GET", f"/device/{dev_id}/device-invites")
        if invites is None:  # a device with no invites yet: the live API answers null, not [] (proof run 2026-10-16)
            invites = []
        if not isinstance(invites, list):
            raise TsError(f"GET /device/{dev_id}/device-invites: not a list")
        if have and any(isinstance(i, dict) and i.get("inviteUrl") == have and i.get("multiUse") is True
                        for i in invites):
            return have, False
        if not apply:
            return None, True
        _, _, made = self.api.call("POST", f"/device/{dev_id}/device-invites",
                                   [{"multiUse": True, "allowExitNode": False}])
        if not (isinstance(made, list) and len(made) == 1 and isinstance(made[0], dict)):
            raise TsError("POST device-invites: expected one invite back")
        if made[0].get("multiUse") is False:
            raise TsError("POST device-invites: the invite came back single-use; friends need a multi-use link")
        link = made[0].get("inviteUrl")
        if not isinstance(link, str):
            raise TsError("POST device-invites: no inviteUrl")
        return link, True


def save_backup(backup_dir, text):
    """The old policy's HuJSON text as read (decoded as UTF-8, written back as text), at mode 600."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = backup_dir / f"tailscale-policy-{stamp}.hujson"
    render.write_atomic(path, text, 0o600)
    return path


def setup(api, *, apply, env_path, token_path, status=None, kit=None, backup_dir=None, today=None, out=print,
          wait_s=None):
    """P3. -> 0 done (or dry run with nothing to do), 2 dry run with work planned. Raises TsError on any refusal."""
    status = status or local_status
    backup_dir = backup_dir or CONFIG_DIR
    wait_s = STATUS_WAIT_S if wait_s is None else wait_s
    today = today or datetime.date.today()
    kit = kit_tailscale() if kit is None else kit
    tag = kit.get("tag", DEFAULT_TAG)
    _check_kit("tag", tag)
    me = status()
    for key in ("server_address", "host_name"):
        if key in kit and kit[key] != me[key]:
            raise TsError(f"kit.toml tailscale.{key} is {kit[key]!r}, but this machine is {me[key]!r}; "
                          "fix kit.toml (it is the address's one home), then re-run")
    ts = Ts(api, out)
    out(f"machine {me['server_address']} (tag {tag})")
    plan = ts.apply_policy(tag, apply, backup_dir)
    for c in plan:
        out(f"  policy: {c}")
    if not plan:
        out("  policy: already has the kit's part")
    dev_id = ts.device(me["server_address"], me["node_id"])
    if "device_id" in kit and kit["device_id"] != dev_id:
        raise TsError(f"kit.toml tailscale.device_id is {kit['device_id']!r}, but the API's device is {dev_id!r}")
    steps = [s for s in (ts.tag(dev_id, tag, apply), ts.key_expiry(dev_id, apply)) if s]
    have = current_link(env_path)
    link, new_invite = ts.invite(dev_id, have, apply)
    if new_invite:
        steps.append("create a multi-use share link and write it to discord/.env")
    for s in steps:
        out(f"  device: {s}")
    if not apply:
        work = bool(plan or steps)
        out(f"{len(plan) + len(steps)} change(s)" + (" (dry run; --apply to make them)" if work else ""))
        out(f"token kept at {token_path} (a dry run never deletes it)")
        return 2 if work else 0
    if new_invite:
        write_link(env_path, link, today)
    gates(ts, tag, dev_id, env_path, link, today, status, wait_s)
    out("gates: policy has the kit's part; device tagged, key expiry disabled; local status shows the tag; "
        "discord/.env holds the link")
    out(f"\nkit.toml [tailscale]:\n  host_name = \"{me['host_name']}\"\n  server_address = "
        f"\"{me['server_address']}\"\n  tag = \"{tag}\"\n  device_id = \"{dev_id}\"")
    forget_token(token_path, out)
    return 0


def gates(ts, tag, dev_id, env_path, link, today, status, wait_s):
    """Read every result back live. A POST's 200 is not a gate."""
    policy, _ = ts.policy()
    _, left = merge_policy(policy, tag)
    if left:
        raise TsError("gate: the policy read back still needs: " + "; ".join(left))
    d = ts.get_device(dev_id)
    if tag not in (d.get("tags") or []):
        raise TsError(f"gate: GET /device/{dev_id} does not show {tag}")
    if d.get("keyExpiryDisabled") is not True:
        raise TsError(f"gate: GET /device/{dev_id} does not show key expiry disabled")
    deadline = time.monotonic() + wait_s
    while tag not in status()["tags"]:
        if time.monotonic() >= deadline:
            raise TsError(f"gate: `tailscale status --json` Self.Tags still lacks {tag} after {wait_s} s")
        time.sleep(2)
    if read_back_link(env_path, today) != link:
        raise TsError("gate: discord/.env does not hold the share link the API returned")


def forget_token(token_path, out):
    """Operator ruling 2026-10-14: the token is used once. Delete the file and have the person revoke it."""
    Path(token_path).unlink(missing_ok=True)
    out(f"\ndeleted {token_path}.\nNOW REVOKE THE TOKEN: open {REVOKE_URL}, find the API access token under "
        "\"API access tokens\", and click Revoke. It carries your full Tailscale permissions until then.\n"
        "To make a new share link later: console -> Machines -> this machine -> Share -> Copy share link, then "
        "python3 kit/secret.py share-link (it replaces TAILSCALE_SHARE_LINK and its date in discord/.env).")


# ---- CLI -------------------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="derive host_name and server_address (no credential)")
    s = sub.add_parser("setup", help="policy, tag, key expiry, share link (dry run unless --apply)")
    s.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise TsError("refusing to run as root: discord/.env and the token file are the kit user's; run it "
                          "without sudo")
        if args.cmd == "status":
            me = local_status()
            print(f"[tailscale]\nhost_name = \"{me['host_name']}\"\nserver_address = \"{me['server_address']}\"")
            print(f"# tags now: {', '.join(me['tags']) or 'none'}")
            return 0
        token_path = TOKEN_FILE
        try:
            api = Api(read_token(token_path), API)
            return setup(api, apply=args.apply, env_path=DOTENV, token_path=token_path)
        except TsError:
            if token_path.exists():
                print(f"(the token file is kept at {token_path} for a re-run; if you stop here, delete it and "
                      f"revoke the token at {REVOKE_URL})", file=sys.stderr)
            raise
    except TsError as e:
        print(f"tailscale: {redact(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
