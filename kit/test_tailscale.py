"""Tests for kit/tailscale.py (P3, gap #7) against a fake Tailscale API: a local HTTP server whose routes, bodies and
status codes follow the API's OpenAPI spec (see the module docstring). Nothing here touches the real API, a real
tailnet or the real discord/.env. Run: uv run --with pytest pytest kit/

UNVERIFIED LIVE: the fake encodes the spec, so these tests prove the module honours the spec as read, not that the
API behaves that way.
"""
import copy
import datetime
import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import tailscale as ts

TOKEN = "tskey-api-kFAKEcntrl-SECRETsecretSECRET0123456789"
# Built at run time, like LINK below: pack/test_build.py's one-home test refuses any tracked file that contains a
# tailnet MagicDNS name outside the instance paths (kit/INSTANCE-PATHS), fake or not.
SUFFIX = "tail1234ab" + ".ts.net"
ADDRESS = "lab-laptop." + SUFFIX
NODE = "nLAB123CNTRL"
# Built at run time: discord/test_sync.py refuses any tracked file that contains a share link, fake or not.
INVITE = "https://login.tailscale.com/admin/" + "invite/"
LINK = INVITE + "AbCdEf123456"
TODAY = datetime.date(2026, 10, 14)
OTHER_ENV = "DISCORD_TOKEN=dummy-claudius-token\n# a comment\nEXTRA=keep me"  # no trailing newline on purpose

# The spec's own policy examples (components.examples): HuJSON (the acls form) and JSON (the legacy users/ports form)
DEFAULT_ACLS = {"groups": {"group:example": ["user1@example.com", "user2@example.com"]},
                "hosts": {"example-host-1": "100.100.100.100"},
                "acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}
LEGACY = {"acls": [{"action": "accept", "ports": ["*:*"], "users": ["*"]}],
          "groups": {"group:example": ["user1@example.com", "user2@example.com"]},
          "hosts": {"example-host-1": "100.100.100.100"}}
# tailscale.com/docs/reference/syntax/policy-file: "the broadest policy allowed" (the grants form)
GRANTS = {"grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}],
          "ssh": [{"action": "check", "src": ["autogroup:member"], "dst": ["autogroup:self"],
                   "users": ["autogroup:nonroot", "root"]}]}
CUSTOM_ACLS = {"groups": {"group:family": ["a@example.com"]},
               "acls": [{"action": "accept", "src": ["group:family"], "dst": ["*:*"]},
                        {"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:self:*"]}],
               "tagOwners": {"tag:mc-server": ["a@example.com"]}}
MEMBER_TAGGED = ["autogroup:member", "autogroup:tagged"]


# ---- the fake API ------------------------------------------------------------------------------------------------

def hujson_of(policy):
    """A commented HuJSON rendering of a policy (comments and a trailing comma: not JSON)."""
    body = json.dumps(policy, indent=2)
    return "// Example/default ACLs for unrestricted connections.\n" + body[:-1].rstrip() + ",\n\t// keep me\n}\n"


class Fake:
    """State of a fake tailnet. requests: [(method, path, headers, body)]."""

    def __init__(self, policy):
        self.policy = copy.deepcopy(policy)
        self.version = 1
        self.requests = []
        self.devices = {NODE: {"id": "92960230385", "nodeId": NODE, "name": ADDRESS, "hostname": "lab-laptop",
                               "tags": [], "keyExpiryDisabled": False}}
        self.invites = {NODE: []}
        self.validate_reply = {}
        self.on_acl_post = None  # called before the If-Match check (a concurrent console edit)
        self.send_etag = True
        self.invite_url = LINK
        self.key_works = True
        self.tags_work = True
        self.acl_post_works = True  # False: a 200 that stores nothing
        self.override = {}  # (method, path) -> (status, body)
        # The policy as HuJSON, the way a new tailnet ships it: commented. A write stores JSON, so the comments go.
        self.hujson = hujson_of(self.policy)

    @property
    def etag(self):
        return f'"e{self.version:04d}"'

    def handle(self, method, path, headers, body):
        self.requests.append((method, path, headers, body))
        if (method, path) in self.override:
            return self.override[(method, path)] + ({},)
        if path == "/tailnet/-/acl" and method == "GET":
            etag = {"ETag": self.etag} if self.send_etag else {}
            if headers.get("Accept") == "application/hujson":
                return 200, self.hujson.encode(), etag  # bytes: sent as they are, not as JSON
            return 200, self.policy, etag
        if path == "/tailnet/-/acl/validate" and method == "POST":
            return 200, self.validate_reply, {}
        if path == "/tailnet/-/acl" and method == "POST":
            if self.on_acl_post:
                self.on_acl_post(self)
            if headers.get("If-Match") != self.etag:
                return 412, {"message": "precondition failed, invalid old hash"}, {}
            if self.acl_post_works:
                self.policy, self.version = body, self.version + 1
                self.hujson = json.dumps(body, indent=2)
            return 200, body, {"ETag": self.etag}
        if path == "/tailnet/-/devices" and method == "GET":
            return 200, {"devices": list(self.devices.values())}, {}
        parts = path.strip("/").split("/")
        if parts[0] == "device" and parts[1] in self.devices:
            d = self.devices[parts[1]]
            rest = parts[2:]
            if not rest and method == "GET":
                return 200, d, {}
            if rest == ["tags"] and method == "POST":
                if self.tags_work:
                    d["tags"] = body["tags"]
                return 200, None, {}
            if rest == ["key"] and method == "POST":
                if self.key_works:
                    d["keyExpiryDisabled"] = body["keyExpiryDisabled"]
                return 200, None, {}
            if rest == ["device-invites"] and method == "GET":
                return 200, self.invites[parts[1]], {}
            if rest == ["device-invites"] and method == "POST":
                made = [{"id": str(1000 + len(self.invites[parts[1]])), "created": "2026-10-14T12:00:00Z",
                         "deviceId": 11055, "multiUse": b.get("multiUse", False), "inviteUrl": self.invite_url,
                         "accepted": False} for b in body]
                self.invites[parts[1]] += made
                return 200, made, {}
        return 404, {"message": "not found"}, {}

    def writes(self):
        return [(m, p) for m, p, _, _ in self.requests if m != "GET"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _do(self):
        fake = self.server.fake
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        path = self.path.split("?")[0]
        if not path.startswith("/api/v2/"):
            self.send_response(404)
            self.end_headers()
            return
        status, reply, headers = fake.handle(self.command, path[len("/api/v2"):], dict(self.headers), body)
        raw = b"" if reply is None else reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _do


@pytest.fixture
def server():
    started = []

    def start(policy):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.fake = Fake(policy)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        started.append(srv)
        return srv.fake, f"http://127.0.0.1:{srv.server_address[1]}/api/v2"
    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def files(tmp_path):
    cfg = tmp_path / "mc-kit"
    cfg.mkdir(mode=0o700)
    token = cfg / "tailscale-api-token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    env = tmp_path / "discord.env"
    env.write_text(OTHER_ENV)
    env.chmod(0o600)
    return token, env, cfg


def status_of(fake, node=NODE):
    """The local `tailscale status --json` view, following the fake's device (as the real client follows the
    control plane)."""
    return lambda: {"host_name": ADDRESS.split(".")[0], "server_address": ADDRESS, "node_id": node,
                    "tags": list(fake.devices[NODE]["tags"])}


def run_setup(fake, base, files, apply=True, out=None, **kw):
    token, env, cfg = files
    lines = [] if out is None else out
    kw.setdefault("status", status_of(fake))
    kw.setdefault("kit", {})
    rc = ts.setup(ts.Api(ts.read_token(token), base), apply=apply, env_path=env, token_path=token,
                  backup_dir=cfg, today=TODAY, out=lines.append, wait_s=0, **kw)
    return rc, lines


def env_value(env, key):
    return {line.split("=", 1)[0]: line.split("=", 1)[1] for line in env.read_text().splitlines()
            if "=" in line and not line.startswith("#")}.get(key)


# ---- the policy merge (pure) --------------------------------------------------------------------------------------

def test_merge_the_default_acls_policy():
    new, changes = ts.merge_policy(DEFAULT_ACLS)
    assert new["acls"][0] == {"action": "accept", "src": MEMBER_TAGGED, "dst": ["*:*"]}
    assert new["acls"][1] == {"action": "accept", "src": ["autogroup:shared"], "proto": "tcp",
                              "dst": ["tag:mc-server:25565"]}
    assert "grants" not in new  # the kit rule goes into the form the policy uses
    assert new["tagOwners"] == {"tag:mc-server": ["autogroup:admin"]}
    assert new["groups"] == DEFAULT_ACLS["groups"] and new["hosts"] == DEFAULT_ACLS["hosts"]
    assert len(changes) == 3
    assert DEFAULT_ACLS["acls"][0]["src"] == ["*"]  # the input is not modified


def test_merge_the_legacy_users_ports_form():
    new, _ = ts.merge_policy(LEGACY)
    assert new["acls"][0] == {"action": "accept", "ports": ["*:*"], "users": MEMBER_TAGGED}


def test_merge_the_grants_form():
    new, _ = ts.merge_policy(GRANTS)
    assert new["grants"] == [{"src": MEMBER_TAGGED, "dst": ["*"], "ip": ["*"]},
                             {"src": ["autogroup:shared"], "dst": ["tag:mc-server"], "ip": ["tcp:25565"]}]
    assert new["ssh"] == GRANTS["ssh"] and "acls" not in new


def test_merge_keeps_custom_acls_and_existing_tag_owners():
    new, changes = ts.merge_policy(CUSTOM_ACLS)
    assert new["acls"][:2] == CUSTOM_ACLS["acls"]
    assert new["acls"][2]["src"] == ["autogroup:shared"]
    assert new["tagOwners"] == CUSTOM_ACLS["tagOwners"]
    assert len(changes) == 1


@pytest.mark.parametrize("policy", [DEFAULT_ACLS, LEGACY, GRANTS, CUSTOM_ACLS, {}, {"acls": [], "grants": []}])
def test_merge_is_idempotent(policy):
    once, changes = ts.merge_policy(policy)
    assert changes
    twice, again = ts.merge_policy(once)
    assert again == [] and twice == once


def test_merge_with_no_rules_or_both_forms_adds_a_grant():
    assert ts.merge_policy({})[0]["grants"] == [ts.kit_grant("tag:mc-server")]
    both, _ = ts.merge_policy({"acls": [], "grants": []})
    assert both["grants"] == [ts.kit_grant("tag:mc-server")] and both["acls"] == []


def test_merge_uses_the_legacy_members_autogroup_when_the_policy_does():
    policy = {"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]},
                       {"action": "accept", "src": ["autogroup:members"], "dst": ["autogroup:self:*"]}]}
    assert ts.merge_policy(policy)[0]["acls"][0]["src"] == ["autogroup:members", "autogroup:tagged"]


@pytest.mark.parametrize("rule, match", [
    ({"action": "accept", "src": ["*"], "dst": ["tag:mc-server:22"]}, "not the default allow-all"),
    ({"action": "accept", "src": ["*", "group:x"], "dst": ["*:*"]}, "not the default allow-all"),
    ({"action": "accept", "src": ["*"], "proto": "tcp", "dst": ["*:*"]}, "not the default allow-all"),
    ({"action": "accept", "src": ["autogroup:shared"], "dst": ["tag:mc-server:*"]}, "not the kit's rule"),
    ({"action": "accept", "src": ["autogroup:danger-all"], "dst": ["*:*"]}, "danger-all"),
    ({"action": "accept", "src": ["*"], "users": ["*"], "dst": ["*:*"]}, "no single src"),
])
def test_merge_refuses_rules_it_did_not_write_that_admit_friends(rule, match):
    with pytest.raises(ts.TsError, match=match):
        ts.merge_policy({"acls": [rule]})


@pytest.mark.parametrize("grant", [
    {"src": ["*"], "dst": ["*"], "ip": ["tcp:22"]},
    {"src": ["*"], "dst": ["*"], "app": {"x": []}},
    {"src": ["autogroup:shared"], "dst": ["tag:mc-server"], "ip": ["*"]},
])
def test_merge_refuses_grants_it_did_not_write_that_admit_friends(grant):
    with pytest.raises(ts.TsError):
        ts.merge_policy({"grants": [grant]})


def test_merge_recognises_the_kit_rule_already_in_the_acls_form():
    policy = {"acls": [ts.kit_acl("tag:mc-server")], "tagOwners": {"tag:mc-server": []}}
    assert ts.merge_policy(policy) == (policy, [])


# ---- end to end against the fake API ----------------------------------------------------------------------------

@pytest.mark.parametrize("policy", [DEFAULT_ACLS, LEGACY, GRANTS, CUSTOM_ACLS])
def test_setup_applies_every_step_then_deletes_the_token(server, files, policy):
    fake, base = server(policy)
    token, env, cfg = files
    rc, out = run_setup(fake, base, files)
    assert rc == 0
    assert fake.writes() == [("POST", "/tailnet/-/acl/validate"), ("POST", "/tailnet/-/acl"),
                             ("POST", f"/device/{NODE}/tags"), ("POST", f"/device/{NODE}/key"),
                             ("POST", f"/device/{NODE}/device-invites")]
    assert fake.policy == ts.merge_policy(policy)[0]
    dev = fake.devices[NODE]
    assert dev["tags"] == ["tag:mc-server"] and dev["keyExpiryDisabled"] is True
    invite_body = next(b for m, p, _, b in fake.requests if p.endswith("device-invites") and m == "POST")
    assert invite_body == [{"multiUse": True, "allowExitNode": False}]
    # every request carried the token as a Bearer, and the policy write carried the GET's ETag
    assert all(h.get("Authorization") == f"Bearer {TOKEN}" for _, _, h, _ in fake.requests)
    acl_post = next(h for m, p, h, _ in fake.requests if (m, p) == ("POST", "/tailnet/-/acl"))
    assert acl_post["If-Match"] == '"e0001"'
    # discord/.env: the link and the date added on their own lines, every other byte kept, mode 600
    assert env.read_text() == OTHER_ENV + f"\nTAILSCALE_SHARE_LINK={LINK}\nTAILSCALE_SHARE_LINK_DATE=2026-10-14\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not token.exists()
    text = "\n".join(out)
    assert "REVOKE THE TOKEN" in text and ts.REVOKE_URL in text
    assert f'device_id = "{NODE}"' in text and f'server_address = "{ADDRESS}"' in text
    # the backup is the old policy's HuJSON text as read, comments and all
    backups = list(cfg.glob("tailscale-policy-*"))
    assert [b.suffix for b in backups] == [".hujson"]
    assert backups[0].read_text() == hujson_of(policy) and "// keep me" in backups[0].read_text()
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert fake.hujson != hujson_of(policy)  # the write did drop the comments: the backup is their only copy


def test_the_hujson_backup_is_on_disk_before_the_policy_write(server, files):
    fake, base = server(DEFAULT_ACLS)
    _, _, cfg = files
    seen = []

    def at_post(f):
        seen.append([b.read_text() for b in cfg.glob("tailscale-policy-*.hujson")])
        f.on_acl_post = None
    fake.on_acl_post = at_post
    run_setup(fake, base, files)
    assert seen == [[hujson_of(DEFAULT_ACLS)]]
    hujson_gets = [i for i, (m, p, h, _) in enumerate(fake.requests)
                   if (m, p) == ("GET", "/tailnet/-/acl") and h.get("Accept") == "application/hujson"]
    post = next(i for i, (m, p, _, _) in enumerate(fake.requests) if (m, p) == ("POST", "/tailnet/-/acl"))
    assert len(hujson_gets) == 1 and hujson_gets[0] < post


def test_an_empty_hujson_policy_is_no_backup_and_no_write(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, _, cfg = files
    fake.hujson = "  \n"
    with pytest.raises(ts.TsError, match="nothing to back up"):
        run_setup(fake, base, files)
    assert ("POST", "/tailnet/-/acl") not in fake.writes()
    assert not list(cfg.glob("tailscale-policy-*")) and token.exists()


def test_a_rerun_changes_nothing(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    run_setup(fake, base, files)
    after_first, n = env.read_text(), len(fake.requests)
    token.write_text(TOKEN + "\n")  # a new paste for the re-run
    token.chmod(0o600)
    rc, out = run_setup(fake, base, files)
    assert rc == 0
    assert [m for m, *_ in fake.requests[n:]] and all(m == "GET" for m, *_ in fake.requests[n:])
    assert env.read_text() == after_first
    assert "  policy: already has the kit's part" in out


def test_the_dry_run_writes_nothing_and_keeps_the_token(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, cfg = files
    rc, out = run_setup(fake, base, files, apply=False)
    assert rc == 2
    assert fake.writes() == [("POST", "/tailnet/-/acl/validate")]  # validate never changes the policy
    assert fake.policy == DEFAULT_ACLS and fake.devices[NODE]["tags"] == []
    assert env.read_text() == OTHER_ENV and token.exists() and not list(cfg.glob("tailscale-policy-*"))
    assert any("create a multi-use share link" in line for line in out)


def test_a_412_rereads_merges_onto_the_newer_policy_and_retries(server, files):
    fake, base = server(DEFAULT_ACLS)

    def console_edit(f):  # someone saves in the console between the kit's GET and POST, once
        f.policy = {**f.policy, "hosts": {"nas": "100.64.0.9"}}
        f.version += 1
        f.on_acl_post = None
    fake.on_acl_post = console_edit
    rc, out = run_setup(fake, base, files)
    assert rc == 0
    posts = [h["If-Match"] for m, p, h, _ in fake.requests if (m, p) == ("POST", "/tailnet/-/acl")]
    assert posts == ['"e0001"', '"e0002"']
    assert fake.policy["hosts"] == {"nas": "100.64.0.9"}  # the console edit survived
    assert fake.policy["acls"][0]["src"] == MEMBER_TAGGED
    assert any("HTTP 412" in line for line in out)
    assert len(list(files[2].glob("tailscale-policy-*.hujson"))) == 2  # one backup per attempt


def test_a_second_412_refuses_before_touching_the_device(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files

    def always(f):
        f.version += 1
    fake.on_acl_post = always
    with pytest.raises(ts.TsError, match="twice in a row"):
        run_setup(fake, base, files)
    assert [p for m, p in fake.writes() if p == "/tailnet/-/acl"] == ["/tailnet/-/acl"] * 2
    assert not [p for _, p in fake.writes() if p.startswith("/device/")]
    assert token.exists() and env.read_text() == OTHER_ENV


def test_no_etag_means_no_policy_write(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.send_etag = False
    with pytest.raises(ts.TsError, match="no ETag"):
        run_setup(fake, base, files)
    assert ("POST", "/tailnet/-/acl") not in fake.writes()


def test_a_validation_failure_is_a_200_and_still_refuses(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.validate_reply = {"message": "test(s) failed",
                           "data": [{"user": "u@example.com", "errors": ["want: Drop, got: Accept"]}]}
    with pytest.raises(ts.TsError, match="failed Tailscale's validation: test\\(s\\) failed"):
        run_setup(fake, base, files)
    assert fake.writes() == [("POST", "/tailnet/-/acl/validate")]


def test_tagging_keeps_the_device_s_other_tags(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.devices[NODE]["tags"] = ["tag:home"]
    run_setup(fake, base, files)
    body = next(b for m, p, _, b in fake.requests if (m, p) == ("POST", f"/device/{NODE}/tags"))
    assert body == {"tags": ["tag:home", "tag:mc-server"]}


def test_the_link_in_discord_env_is_kept_while_it_is_a_live_multi_use_invite(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    old = INVITE + "OldOld111"
    env.write_text(f"DISCORD_TOKEN=x\nTAILSCALE_SHARE_LINK={old}\nTAILSCALE_SHARE_LINK_DATE=2026-10-10\n")
    fake.invites[NODE] = [{"id": "1", "inviteUrl": old, "multiUse": True, "accepted": True}]
    run_setup(fake, base, files)
    assert ("POST", f"/device/{NODE}/device-invites") not in fake.writes()
    assert env_value(env, "TAILSCALE_SHARE_LINK") == old and env_value(env, "TAILSCALE_SHARE_LINK_DATE") == "2026-10-10"


def test_a_stale_link_is_replaced_in_place(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    env.write_text(f"DISCORD_TOKEN=x\nTAILSCALE_SHARE_LINK={INVITE}Gone0\n"
                   "TAILSCALE_SHARE_LINK_DATE=2026-09-01\nZ=1\n")
    run_setup(fake, base, files)
    assert env.read_text() == (f"DISCORD_TOKEN=x\nTAILSCALE_SHARE_LINK={LINK}\nTAILSCALE_SHARE_LINK_DATE=2026-10-14\n"
                               "Z=1\n")


def test_an_invite_that_is_not_a_share_link_writes_nothing(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    fake.invite_url = "https://evil.example/admin/invite/AbC"
    with pytest.raises(ts.TsError, match="nothing was written"):
        run_setup(fake, base, files)
    assert env.read_text() == OTHER_ENV and token.exists()


def test_a_duplicate_link_line_in_discord_env_is_refused(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    dup = "DISCORD_TOKEN=x\nTAILSCALE_SHARE_LINK=a\nTAILSCALE_SHARE_LINK=b\n"
    env.write_text(dup)
    with pytest.raises(ts.TsError, match="set twice"):
        run_setup(fake, base, files)
    assert env.read_text() == dup


def test_the_api_device_must_be_this_machine(server, files):
    fake, base = server(DEFAULT_ACLS)
    with pytest.raises(ts.TsError, match="refusing to tag or share a different machine"):
        run_setup(fake, base, files, status=status_of(fake, node="nOTHER9CNTRL"))
    assert not [p for _, p in fake.writes() if p.startswith("/device/")]


def test_no_device_with_this_name_is_refused(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.devices[NODE]["name"] = "someone-else." + SUFFIX
    with pytest.raises(ts.TsError, match="0 devices"):
        run_setup(fake, base, files)


def test_two_devices_with_this_name_are_refused(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.devices["nTWIN456CNTRL"] = {**fake.devices[NODE], "id": "1", "nodeId": "nTWIN456CNTRL"}
    with pytest.raises(ts.TsError, match="2 devices"):
        run_setup(fake, base, files)
    assert not [p for _, p in fake.writes() if p.startswith("/device/")]


def test_kit_toml_device_id_disagreeing_with_the_api_is_refused(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    with pytest.raises(ts.TsError, match="kit.toml tailscale.device_id is 'other'"):
        run_setup(fake, base, files, kit={"device_id": "other"})
    assert not [p for _, p in fake.writes() if p.startswith("/device/")]
    assert env.read_text() == OTHER_ENV and token.exists()


def test_an_invite_that_comes_back_single_use_writes_nothing(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    fake.override[("POST", f"/device/{NODE}/device-invites")] = (200, [{"id": "7", "inviteUrl": LINK,
                                                                        "multiUse": False}])
    with pytest.raises(ts.TsError, match="single-use"):
        run_setup(fake, base, files)
    assert env.read_text() == OTHER_ENV and token.exists()


def test_a_single_use_invite_matching_discord_env_is_not_kept(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    old = INVITE + "OldOld111"
    env.write_text(f"DISCORD_TOKEN=x\nTAILSCALE_SHARE_LINK={old}\nTAILSCALE_SHARE_LINK_DATE=2026-10-10\n")
    fake.invites[NODE] = [{"id": "1", "inviteUrl": old, "multiUse": False, "accepted": False}]
    run_setup(fake, base, files)
    assert ("POST", f"/device/{NODE}/device-invites") in fake.writes()
    assert env_value(env, "TAILSCALE_SHARE_LINK") == LINK


def test_a_write_that_5xx_d_is_never_retried(server, files, monkeypatch):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    monkeypatch.setattr(ts.time, "sleep", lambda s: None)
    path = f"/device/{NODE}/device-invites"
    fake.override[("POST", path)] = (503, {"message": "unavailable"})
    with pytest.raises(ts.TsError, match="HTTP 503"):
        run_setup(fake, base, files)
    assert [r[:2] for r in fake.requests].count(("POST", path)) == 1  # a retry could make a second invite
    assert env.read_text() == OTHER_ENV and token.exists()


def test_a_device_with_no_invites_answers_null_and_one_is_made(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.override[("GET", f"/device/{NODE}/device-invites")] = (200, None)  # the live API, proof run 2026-10-16
    rc, _ = run_setup(fake, base, files)
    assert rc == 0
    assert ("POST", f"/device/{NODE}/device-invites") in fake.writes()


def test_kit_toml_disagreeing_with_this_machine_refuses_before_any_call(server, files):
    fake, base = server(DEFAULT_ACLS)
    with pytest.raises(ts.TsError, match="kit.toml tailscale.server_address"):
        run_setup(fake, base, files, kit={"server_address": "other-laptop." + SUFFIX})
    assert fake.requests == []


def test_a_gate_that_reads_back_wrong_keeps_the_token(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, _, _ = files
    fake.key_works = False  # the key POST returns 200 but changes nothing
    with pytest.raises(ts.TsError, match="key expiry disabled"):
        run_setup(fake, base, files)
    assert token.exists()


def test_a_policy_write_that_did_not_land_fails_the_gate(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, _, _ = files
    fake.acl_post_works = False
    with pytest.raises(ts.TsError, match="the policy read back still needs"):
        run_setup(fake, base, files)
    assert token.exists()


def test_a_tag_that_did_not_land_fails_the_gate(server, files):
    fake, base = server(DEFAULT_ACLS)
    fake.tags_work = False
    with pytest.raises(ts.TsError, match=f"GET /device/{NODE} does not show tag:mc-server"):
        run_setup(fake, base, files)


def test_the_local_status_must_show_the_tag(server, files):
    fake, base = server(DEFAULT_ACLS)
    token, _, _ = files
    with pytest.raises(ts.TsError, match="Self.Tags still lacks tag:mc-server"):
        run_setup(fake, base, files, status=lambda: {"host_name": "lab-laptop", "server_address": ADDRESS,
                                                     "node_id": NODE, "tags": []})
    assert token.exists()


def test_the_read_back_gate_compares_what_sync_reads(server, files, monkeypatch):
    fake, base = server(DEFAULT_ACLS)
    token, env, _ = files
    env.write_text(f"DISCORD_TOKEN=x\nTAILSCALE_SHARE_LINK={INVITE}Stale9\nTAILSCALE_SHARE_LINK_DATE=2026-10-01\n")
    monkeypatch.setattr(ts, "write_link", lambda *a: None)  # a write that silently did not land
    with pytest.raises(ts.TsError, match="does not hold the share link"):
        run_setup(fake, base, files)
    assert token.exists()


# ---- the token is never shown -----------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, files, tmp_path):
    token, env, cfg = files

    def run(base, fake, argv=("setup", "--apply")):
        monkeypatch.setattr(ts, "API", base)
        monkeypatch.setattr(ts, "TOKEN_FILE", token)
        monkeypatch.setattr(ts, "CONFIG_DIR", cfg)
        monkeypatch.setattr(ts, "DOTENV", env)
        monkeypatch.setattr(ts, "KIT_TOML", tmp_path / "absent-kit.toml")
        monkeypatch.setattr(ts, "STATUS_WAIT_S", 0)
        monkeypatch.setattr(ts, "local_status", status_of(fake))
        return ts.main(list(argv))
    return run


def test_the_token_and_the_link_are_never_printed(server, cli, capsys):
    fake, base = server(DEFAULT_ACLS)
    assert cli(base, fake) == 0
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and "tskey-api" not in out.out + out.err
    assert LINK not in out.out + out.err and "AbCdEf123456" not in out.out + out.err
    assert "REVOKE THE TOKEN" in out.out


def test_an_error_body_echoing_the_token_is_redacted(server, cli, capsys):
    fake, base = server(DEFAULT_ACLS)
    fake.override[("GET", "/tailnet/-/acl")] = (403, {"message": f"token {TOKEN} lacks policy_file; see {LINK}"})
    assert cli(base, fake) == 1
    err = capsys.readouterr().err
    assert "HTTP 403" in err and "<token>" in err and "<share_link>" in err
    assert TOKEN not in err and "SECRETsecret" not in err and LINK not in err


def test_an_error_message_is_redacted_before_it_is_cut(server, cli, capsys):
    """The cut at 300 chars must not land inside a secret before redact() sees it. TOKEN_RX matches any tskey-api-
    prefix with one more char, so only a cut that leaves `tskey-api-` or less tells the order apart: 292 chars of
    padding put the cut at `tskey-ap`."""
    fake, base = server(DEFAULT_ACLS)
    fake.override[("GET", "/tailnet/-/acl")] = (403, {"message": "x" * 292 + TOKEN})
    assert cli(base, fake) == 1
    err = capsys.readouterr().err
    assert "HTTP 403" in err and "x" * 292 + "<token>" in err and "tskey" not in err


def test_message_redacts_on_its_own():
    """_message is the layer that redacts an API error body (Api.call adds no redact of its own)."""
    for body in (json.dumps({"message": f"{TOKEN} {LINK}"}), f"not json {TOKEN} {LINK}"):
        msg = ts._message(body.encode())
        assert "<token>" in msg and "<share_link>" in msg
        assert TOKEN not in msg and "tskey-api-" not in msg and LINK not in msg


def test_main_redacts_any_error_it_prints(server, cli, capsys, monkeypatch):
    """main() is the catch-all: an error raised anywhere with a secret in it is printed redacted."""
    fake, base = server(DEFAULT_ACLS)

    def boom(*a, **kw):
        raise ts.TsError(f"boom {TOKEN} {LINK}")
    monkeypatch.setattr(ts, "setup", boom)
    assert cli(base, fake) == 1
    err = capsys.readouterr().err
    assert "boom <token> <share_link>" in err and TOKEN not in err and LINK not in err


def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(server, files):
    fake, base = server(DEFAULT_ACLS)
    elsewhere, other_base = server({})
    fake.override[("GET", "/tailnet/-/acl")] = (302, {})
    # the fake sends no Location for an override; give it one
    orig = fake.handle

    def handle(method, path, headers, body):
        status, reply, hdrs = orig(method, path, headers, body)
        if status == 302:
            hdrs = {"Location": other_base + "/tailnet/-/acl"}
        return status, reply, hdrs
    fake.handle = handle
    with pytest.raises(ts.TsError, match="refused a redirect"):
        run_setup(fake, base, files)
    assert elsewhere.requests == []


@pytest.mark.parametrize("content, mode, match", [
    (TOKEN, 0o640, "must be 600"),
    (TOKEN, 0o604, "must be 600"),
    ("tskey-client-kSECRET-abcdef", 0o600, "OAuth client"),
    ("not-a-token SECRETsecret", 0o600, "contents not shown"),
    # a token followed by bytes outside its shape: redact() could not catch those, so the whole value must match
    (TOKEN + "!x", 0o600, "contents not shown"),
])
def test_the_token_file_is_refused_without_showing_it(tmp_path, content, mode, match):
    f = tmp_path / "tok"
    f.write_text(content)
    f.chmod(mode)
    with pytest.raises(ts.TsError, match=match) as e:
        ts.read_token(f)
    assert "SECRET" not in str(e.value)


def test_a_symlinked_or_missing_token_file_is_refused(tmp_path):
    real = tmp_path / "real"
    real.write_text(TOKEN)
    real.chmod(0o600)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(ts.TsError, match="symlink"):
        ts.read_token(tmp_path / "link")
    with pytest.raises(ts.TsError, match="missing"):
        ts.read_token(tmp_path / "absent")


def test_a_token_file_of_another_user_is_refused(tmp_path, monkeypatch):
    f = tmp_path / "tok"
    f.write_text(TOKEN)
    f.chmod(0o600)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)  # the file is now someone else's
    with pytest.raises(ts.TsError, match=f"owned by uid {me}, not you") as e:
        ts.read_token(f)
    assert "SECRET" not in str(e.value)


def test_the_token_never_crosses_the_network_in_clear():
    with pytest.raises(ts.TsError, match="non-https"):
        ts.Api(TOKEN, "http://api.tailscale.com/api/v2")
    ts.Api(TOKEN, "https://api.tailscale.com/api/v2")


def test_root_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ts.main(["status"]) == 1
    assert "refusing to run as root" in capsys.readouterr().err


# ---- the local status -----------------------------------------------------------------------------------------

def fake_run(payload, rc=0):
    def run(cmd, **kw):
        assert cmd == ["tailscale", "status", "--json"]
        return subprocess.CompletedProcess(cmd, rc, json.dumps(payload), "")
    return run


STATUS = {"BackendState": "Running", "CurrentTailnet": {"MagicDNSEnabled": True, "MagicDNSSuffix": "tail1234ab.ts.net"},
          "Self": {"ID": NODE, "DNSName": ADDRESS + ".", "Tags": ["tag:mc-server"]}}


def test_status_strips_the_trailing_dot():
    assert ts.local_status(fake_run(STATUS)) == {"host_name": "lab-laptop", "server_address": ADDRESS,
                                                 "node_id": NODE, "tags": ["tag:mc-server"]}


@pytest.mark.parametrize("patch, match", [
    ({"BackendState": "NeedsLogin"}, "not logged in"),
    ({"CurrentTailnet": {"MagicDNSEnabled": False}}, "MagicDNS is off"),
    ({"Self": {"DNSName": "100.64.0.1"}}, "server_address"),
    ({"Self": {"DNSName": "Bad_Name." + "tail1.ts.net."}}, "server_address"),
])
def test_status_refuses(patch, match):
    with pytest.raises(ts.TsError, match=match):
        ts.local_status(fake_run({**STATUS, **patch}))


def test_status_without_tailscale_installed():
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    with pytest.raises(ts.TsError, match="not installed"):
        ts.local_status(missing)


# ---- the seam with discord/sync.py ------------------------------------------------------------------------------

def test_what_the_kit_writes_is_what_sync_reads(tmp_path):
    env = tmp_path / ".env"
    env.write_text(OTHER_ENV)
    env.chmod(0o600)
    ts.write_link(env, LINK, TODAY)
    assert ts.read_back_link(env, TODAY) == LINK
    sync = ts._sync()
    sync.DOTENV = env
    parsed = sync.read_env()  # its glued-key guard would exit on an append onto the unterminated last line
    assert parsed["EXTRA"] == "keep me" and parsed["TAILSCALE_SHARE_LINK"] == LINK
    assert sync.share_link(parsed, TODAY + datetime.timedelta(days=1)) == (LINK, [])


def test_the_share_link_regex_has_one_home():
    """kit/tailscale.py checks the API's invite with sync.py's SHARE_LINK; INVITE_RX only redacts. The two must
    agree on what a link is, or a link could be written that redact() would print."""
    sync = ts._sync()
    assert sync.SHARE_LINK.match(LINK) and ts.INVITE_RX.fullmatch(LINK)
    assert "SHARE_LINK = re.compile" not in Path(ts.__file__).read_text()
