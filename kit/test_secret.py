"""Tests for kit/secret.py, the token hand-off for human steps 12, 15 and 18. Run: uv run --with pytest pytest kit/

Fake files and a fake getpass only. Each guard's scenario has a mutant of secret.py that must let it through
(test_each_guard_catches_its_mutant): a guard test counts only once it catches its mutant.
"""
import getpass
import importlib.util
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

import render
import secret
import tailscale as ts

KIT = Path(__file__).resolve().parent
SOURCE = (KIT / "secret.py").read_text()
# Token shapes, assembled at run time so this file never holds one.
DTOKEN = "M" + "Tk5ODYyMjQ4MzQ3MTkyNTI0OA" + "." + "Cl2FMQ" + "." + "ZnCjm1XVW7vRze4b7Cq4se7kKWs"
DTOKEN2 = "N" + "zA0NjMzOTk0NTUxNzI0MzI4" + "." + "Gx9a-b" + "." + "q" * 38
TSKEY = "tskey" + "-api-" + "kAbCdEf1CNTRL-0123456789abcdef"
STEP18 = ("mkdir -p ~/.config/mc-kit && (umask 077; read -rsp 'Paste the Tailscale API token, then Enter: ' T; "
          "echo; printf '%s\\n' \"$T\" > ~/.config/mc-kit/tailscale-api-token)")
STEP12 = ("cd ~/minecraft-ai-host && (umask 077; read -rsp 'Paste the admin bot token, then Enter: ' T; echo; "
          "printf '\\nDISCORD_TOKEN=%s\\n' \"$T\" >> discord/.env) && chmod 600 discord/.env")
# discord/.env as the proof run left it (the share link keys, a mangled first paste, then a second), with CRLF lines.
OTHER = ("# the kit's discord/.env\r\nTAILSCALE_SHARE_LINK=https://example.invalid/x\n"
         "DISCORD_TOKEN=cd ~/minecraft-ai-host && (umask\r\n"
         "TAILSCALE_SHARE_LINK_DATE=2026-10-16\r\n"
         "DISCORD_TOKEN=" + DTOKEN2 + "\n"
         "export DISCORD_TOKEN=" + DTOKEN2 + "\n"
         "# DISCORD_TOKEN=a comment stays\n"
         "LAST=no-newline")


def feed(value):
    return lambda prompt: value


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "discord").mkdir()
    return tmp_path


@pytest.fixture
def tsfile(tmp_path, monkeypatch):
    f = tmp_path / "home" / ".config" / "mc-kit" / "tailscale-api-token"
    monkeypatch.setattr(ts, "TOKEN_FILE", f)
    return f


def mode(p):
    return p.stat().st_mode & 0o777


# ---- discord: the key's line is replaced, every other byte kept ---------------------------------------------------

def test_a_re_paste_collapses_every_line_of_the_key_into_one_in_place(repo):
    env = repo / "discord" / ".env"
    env.write_text(OTHER)
    env.chmod(0o644)
    msg = secret.run("discord-admin", repo, read=feed(DTOKEN))
    assert "replacing 3 old DISCORD_TOKEN lines" in msg and DTOKEN not in msg
    assert env.read_bytes().decode() == ("# the kit's discord/.env\r\nTAILSCALE_SHARE_LINK=https://example.invalid/x\n"
                                         "DISCORD_TOKEN=" + DTOKEN + "\r\n"  # the first line's own ending
                                         "TAILSCALE_SHARE_LINK_DATE=2026-10-16\r\n"
                                         "# DISCORD_TOKEN=a comment stays\n"
                                         "LAST=no-newline")
    assert mode(env) == 0o600
    secret.run("discord-admin", repo, read=feed(DTOKEN))  # a second run is a no-op on the text
    assert env.read_text().count("DISCORD_TOKEN=" + DTOKEN) == 1


def test_a_first_paste_creates_the_file_at_600(repo):
    secret.run("discord-bridge", repo, read=feed(DTOKEN))
    f = repo / ".env.bridge"
    assert f.read_text() == f"DISCORD_BRIDGE_TOKEN={DTOKEN}\n" and mode(f) == 0o600


def test_a_missing_key_is_appended_after_a_newline(repo):
    f = repo / ".env.bridge"
    f.write_text("DISCORD_GUILD_ID=123")  # no trailing newline: the append must not glue onto it
    secret.run("discord-bridge", repo, read=feed(DTOKEN))
    assert f.read_text() == f"DISCORD_GUILD_ID=123\nDISCORD_BRIDGE_TOKEN={DTOKEN}\n"


def test_every_reader_reads_what_it_writes(repo, monkeypatch):
    env, bridge = repo / "discord" / ".env", repo / ".env.bridge"
    env.write_text(OTHER.replace("export DISCORD_TOKEN=" + DTOKEN2 + "\n", ""))  # sync's reader skips no `export`
    bridge.write_text("DISCORD_BRIDGE_TOKEN=old\nDISCORD_GUILD_ID=123\nDISCORD_BRIDGE_TOKEN=" + DTOKEN2 + "\n")
    secret.run("discord-admin", repo, read=feed(DTOKEN))
    secret.run("discord-bridge", repo, read=feed(DTOKEN))
    sync = ts._sync()
    monkeypatch.setattr(sync, "DOTENV", env)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    assert sync.load_env()["DISCORD_TOKEN"] == DTOKEN
    sys.path.insert(0, str(KIT.parent / "discord"))
    try:
        import bootstrap
    finally:
        sys.path.remove(str(KIT.parent / "discord"))
    monkeypatch.setattr(bootstrap, "BRIDGE_DOTENV", bridge)
    assert bootstrap.bridge_token() == DTOKEN
    # render.py merges DISCORD_GUILD_ID into the same file later: it must still accept it, and keep the token line
    merged = render.merge_env(bridge.read_text(), {"DISCORD_GUILD_ID": "456"}, ".env.bridge")
    assert merged == f"DISCORD_BRIDGE_TOKEN={DTOKEN}\nDISCORD_GUILD_ID=456\n"


def test_tailscale_writes_the_file_its_reader_takes(tsfile):
    msg = secret.run("tailscale", None, read=feed(TSKEY))
    assert TSKEY not in msg
    assert tsfile.read_text() == TSKEY + "\n" and mode(tsfile) == 0o600 and mode(tsfile.parent) == 0o700
    assert ts.read_token(tsfile) == TSKEY
    tsfile.write_text(STEP18 + "\n")  # what the proof run's first try left in it
    tsfile.parent.chmod(0o755)
    secret.run("tailscale", None, read=feed(TSKEY))
    assert tsfile.read_text() == TSKEY + "\n" and mode(tsfile.parent) == 0o700


# ---- refusals: the file is untouched, and nothing pasted is shown --------------------------------------------------

@pytest.mark.parametrize("target,pasted,says", [
    ("tailscale", STEP18, "looks like a command"),          # proof run 2026-10-16, step 18
    ("discord-admin", STEP12, "looks like a command"),      # the same mistake at step 12
    ("discord-admin", DTOKEN + " ", "looks like a command"),
    ("discord-admin", "Bot " + DTOKEN, "looks like a command"),
    ("discord-admin", "$T", "looks like a command"),
    ("discord-admin", "", "nothing was pasted"),
    ("discord-admin", "a" * 32, "not shaped like a Discord bot token"),  # the client secret: no dots
    ("discord-admin", DTOKEN + ".x", "not shaped like a Discord bot token"),
    ("discord-bridge", TSKEY, "not shaped like a Discord bot token"),
    ("tailscale", DTOKEN, "not shaped like a Tailscale API access token"),
    ("tailscale", "tskey" + "-auth-k1-abc", "not shaped like a Tailscale API access token"),
    ("tailscale", "tskey" + "-client-k1-abc", "OAuth client secret"),
    ("share-link", "printf '\\nTAILSCALE_SHARE_LINK=" + "https://login.tailscale.com/admin/invite/" + "Zx9\\n' >> "
     "discord/.env", "looks like a command"),                # sync.py's old ENV_APPEND, pasted at the prompt
    ("share-link", "https://login.tailscale.com/admin/invite/" + "Zx9 ", "looks like a command"),
    ("share-link", "https://example.com/admin/invite/" + "Zx9", "not shaped like a Tailscale share link"),
    ("share-link", TSKEY, "not shaped like a Tailscale share link"),
], ids=["step18-command", "step12-command", "trailing-space", "bot-prefix", "variable", "empty", "client-secret",
        "four-parts", "tskey-as-discord", "discord-as-tskey", "auth-key", "oauth-client", "link-printf",
        "link-trailing-space", "link-elsewhere", "tskey-as-link"])
def test_a_bad_paste_is_refused_and_writes_nothing(repo, tsfile, capsys, target, pasted, says):
    env = repo / "discord" / ".env"
    env.write_bytes(OTHER.encode())
    tsfile.parent.mkdir(parents=True)
    tsfile.write_text("before\n")
    with pytest.raises(secret.SecretError) as e:
        secret.run(target, repo, read=feed(pasted))
    assert says in str(e.value) and "Nothing was written" in str(e.value)
    if pasted:
        assert pasted.strip() not in str(e.value) and pasted.split()[0] not in str(e.value)
    assert env.read_bytes() == OTHER.encode() and tsfile.read_text() == "before\n"
    assert not (repo / ".env.bridge").exists()


def test_a_glued_line_is_refused(repo):
    env = repo / "discord" / ".env"
    glued = "TAILSCALE_SHARE_LINK_DATE=2026-10-16DISCORD_TOKEN=" + DTOKEN2 + "\n"
    env.write_text(glued)
    with pytest.raises(secret.SecretError, match="line 1 sets another key but also holds DISCORD_TOKEN="):
        secret.run("discord-admin", repo, read=feed(DTOKEN))
    assert env.read_text() == glued


def test_a_symlinked_env_is_refused(repo, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("X=1\n")
    (repo / "discord" / ".env").symlink_to(target)
    with pytest.raises(secret.SecretError, match="symlink"):
        secret.run("discord-admin", repo, read=feed(DTOKEN))
    assert target.read_text() == "X=1\n"


LINK_OLD = "https://login.tailscale.com/admin/invite/" + "Old0ne"
LINK_NEW = "https://login.tailscale.com/admin/invite/" + "N3wOne"


def test_a_new_share_link_replaces_the_old_pair_and_sync_reads_it(repo, monkeypatch):
    """R1 after 30 days: the link's line and its date are replaced in place, never appended (review 2026-10-16),
    and sync.py's own reader, the one #setup is built from, reads back the new link."""
    env = repo / "discord" / ".env"
    before = (f"DISCORD_TOKEN={DTOKEN2}\r\nTAILSCALE_SHARE_LINK={LINK_OLD}\nTAILSCALE_SHARE_LINK_DATE=2026-09-01\n"
              "LAST=kept")
    env.write_bytes(before.encode())
    for _ in range(2):  # a second rotation still leaves one pair
        msg = secret.run("share-link", repo, read=feed(LINK_NEW))
        assert LINK_NEW not in msg and "TAILSCALE_SHARE_LINK written to discord/.env" in msg
    text = env.read_bytes().decode()  # no newline translation: the CRLF line is kept as it was
    today = __import__("datetime").date.today().isoformat()
    assert text.count("TAILSCALE_SHARE_LINK=") == 1 and text.count("TAILSCALE_SHARE_LINK_DATE=") == 1
    assert f"TAILSCALE_SHARE_LINK={LINK_NEW}\n" in text and f"TAILSCALE_SHARE_LINK_DATE={today}\n" in text
    assert text.startswith(f"DISCORD_TOKEN={DTOKEN2}\r\n") and "LAST=kept" in text and mode(env) == 0o600
    sync = ts._sync()
    monkeypatch.setattr(sync, "DOTENV", env)
    assert sync.share_link(sync.read_env(), __import__("datetime").date.today())[0] == LINK_NEW


def held(path):
    """Hold render.env_lock's lock on `path` from outside (another descriptor, as another process would)."""
    import fcntl
    fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


@pytest.mark.parametrize("target,rel,pasted", [
    ("discord-admin", "discord/.env", DTOKEN),
    ("discord-bridge", ".env.bridge", DTOKEN),
    ("share-link", "discord/.env", LINK_NEW),
])
def test_a_write_waits_for_another_writer_and_then_refuses(repo, monkeypatch, target, rel, pasted):
    """render.py merging DISCORD_GUILD_ID into .env.bridge while the person pastes the bridge token: whichever comes
    second must not write from a stale read (codex review 2026-10-16). Held past the wait, it refuses."""
    monkeypatch.setattr(render, "LOCK_WAIT_SECS", 0.3)
    path = repo / rel
    path.write_text("KEEP=1\n")
    fd = held(path)
    try:
        with pytest.raises(secret.SecretError, match="being written by another kit command"):
            secret.run(target, repo, read=feed(pasted))
        assert path.read_text() == "KEEP=1\n"
    finally:
        os.close(fd)
    secret.run(target, repo, read=feed(pasted))  # released: it goes through
    assert path.read_text().startswith("KEEP=1\n") and pasted in path.read_text()


@pytest.mark.parametrize("target,rel,pasted", [
    ("discord-admin", "discord/.env", DTOKEN),
    ("share-link", "discord/.env", LINK_NEW),
])
def test_a_lock_the_filesystem_cannot_take_is_a_refusal_not_a_traceback(repo, monkeypatch, target, rel, pasted):
    """flock fails for reasons other than another writer (ENOLCK on an NFS mount whose lockd is down): that is
    the same clean "nothing was written" refusal, never a raw OSError (review 2026-10-16)."""
    import errno

    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
    monkeypatch.setattr(render.fcntl, "flock", no_locks)
    path = repo / rel
    path.write_text("KEEP=1\n")
    with pytest.raises(secret.SecretError, match=r"can't lock \.env\.lock next to \.env: No locks available"):
        secret.run(target, repo, read=feed(pasted))
    assert path.read_text() == "KEEP=1\n"
    with pytest.raises(render.RenderError, match="No locks available"):
        with render.env_lock(path):
            pass


def test_no_terminal_is_refused_rather_than_read_visibly(monkeypatch):
    """getpass falls back to a visible stdin read, after a GetPassWarning, when it has no terminal."""
    def fallback(prompt):
        import warnings
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("read the token with echo on")
    monkeypatch.setattr(getpass, "getpass", fallback)
    with pytest.raises(secret.SecretError, match="no terminal"):
        secret.read_hidden("token: ")


def test_cli_with_no_terminal_writes_nothing(repo):
    """The real getpass, the real CLI, no controlling terminal (start_new_session) and the token on a pipe."""
    r = subprocess.run([sys.executable, str(KIT / "secret.py"), "discord-admin", "--repo", str(repo)],
                       input=DTOKEN + "\n", capture_output=True, text=True, start_new_session=True)
    assert r.returncode == 1 and "no terminal" in r.stderr, r.stderr
    assert DTOKEN not in r.stdout + r.stderr
    assert not (repo / "discord" / ".env").exists()


def test_cli_in_a_terminal_hides_the_token_and_writes_it(repo):
    """The real getpass in a real pseudo-terminal: what the terminal shows never contains the token."""
    pid, fd = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the pty
        os.execv(sys.executable, [sys.executable, str(KIT / "secret.py"), "discord-admin", "--repo", str(repo)])
    shown = b""
    while b"then Enter: " not in shown:
        shown += os.read(fd, 1024)
    os.write(fd, DTOKEN.encode() + b"\n")
    while True:
        try:
            chunk = os.read(fd, 1024)
        except OSError:  # EIO: the child closed the terminal
            break
        if not chunk:
            break
        shown += chunk
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, shown
    assert DTOKEN.encode() not in shown and b"DISCORD_TOKEN written to discord/.env" in shown
    assert (repo / "discord" / ".env").read_text() == f"DISCORD_TOKEN={DTOKEN}\n"


def test_the_cli_refuses_root(monkeypatch, repo):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(secret, "read_hidden", lambda prompt: pytest.fail("read a token as root"))
    assert secret.main(["discord-admin", "--repo", str(repo)]) == 1


# ---- mutants -------------------------------------------------------------------------------------------------------

def load(text, tmp_path, name):
    path = tmp_path / f"{name}.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _replace_scenario(mod, repo):
    env = repo / "discord" / ".env"
    env.write_text(OTHER)
    try:
        mod.run("discord-admin", repo, read=feed(DTOKEN))
    except mod.SecretError:
        pass  # the read-back refuses the mutant's file, after writing it: the file is what counts
    return env.read_text().count("DISCORD_TOKEN=") != 2  # the new line + the comment


def _command_scenario(mod, repo):
    """The shape check still refuses "Bot <token>"; the guard is what tells the person they pasted a command."""
    try:
        mod.run("discord-admin", repo, read=feed("Bot " + DTOKEN))
    except mod.SecretError as e:
        return "looks like a command" not in str(e)
    return True


def _shape_scenario(mod, repo):
    try:
        mod.run("discord-admin", repo, read=feed("a" * 32))
    except mod.SecretError:
        return False
    return True


def _glued_scenario(mod, repo):
    env = repo / "discord" / ".env"
    env.write_text("A=1DISCORD_TOKEN=x\n")
    try:
        mod.run("discord-admin", repo, read=feed(DTOKEN))
    except mod.SecretError:
        return False
    return True


def _mode_scenario(mod, repo):
    mod.run("discord-bridge", repo, read=feed(DTOKEN))
    return mode(repo / ".env.bridge") != 0o600


MUTANTS = {
    # name -> (guard text in secret.py, its mutant, scenario(mod, repo) -> True when the mutant let it go wrong)
    "replace_not_append": ("            continue\n        if f\"{key}=\" in line",
                           "            out.append(line)\n            continue\n        if f\"{key}=\" in line",
                           _replace_scenario),
    "commandish": ('COMMANDISH = re.compile(r"[\\s$\'\\"`;|&<>()~/\\\\]")', 'COMMANDISH = re.compile(r"NEVER")',
                   _command_scenario),
    "discord_shape": (r'DISCORD_RX = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")',
                      r'DISCORD_RX = re.compile(r"[A-Za-z0-9_-]+")', _shape_scenario),
    "glued": ('        if f"{key}=" in line and not line.lstrip().startswith("#"):\n',
              '        if False:\n', _glued_scenario),
    "mode_600": ("        render.write_atomic(path, new_text, 0o600)\n",
                 "        render.write_atomic(path, new_text, 0o644)\n", _mode_scenario),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_guard_catches_its_mutant(tmp_path, name):
    guard, mutant, scenario = MUTANTS[name]
    assert SOURCE.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    repo = tmp_path / "repo"
    (repo / "discord").mkdir(parents=True)
    assert not scenario(secret, repo), f"{name}: the real module went wrong"
    repo2 = tmp_path / "repo2"
    (repo2 / "discord").mkdir(parents=True)
    mod = load(SOURCE.replace(guard, mutant), tmp_path, f"secret_mut_{name}")
    assert scenario(mod, repo2), name


@pytest.mark.parametrize("toml,says,not_says", [
    (None, "python3 discord/sync.py (dry run)", "friends.py"),
    ("[discord]\nenabled = false\n", "python3 kit/friends.py", "sync.py"),
    ("not [toml", "with no Discord", None),
])
def test_the_share_link_names_the_next_step_the_choice_needs(repo, tmp_path, monkeypatch, toml, says, not_says):
    """R1 with no Discord goes on to the friend message, never to sync.py (which refuses there)."""
    kit = tmp_path / "kit.toml"
    if toml is not None:
        kit.write_text(toml)
    monkeypatch.setenv("MC_KIT_TOML", str(kit))
    msg = secret.run("share-link", repo, read=feed(LINK_NEW))
    assert says in msg and LINK_NEW not in msg
    if not_says:
        assert not_says not in msg.split("Next:")[1]
