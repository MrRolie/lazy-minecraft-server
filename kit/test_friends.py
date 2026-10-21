"""Tests for kit/friends.py, the friend message for a server with no Discord. Run: uv run --with pytest pytest kit/

Fake files only: a made-up kit.toml (kit/fixtures/example.toml with discord.enabled = false), a fake discord/.env
and a stand-in dist/ pack. The share link is assembled at run time, so this file never holds one. Each guard has a
mutant of friends.py that must get past it (test_each_guard_catches_its_mutant).
"""
import datetime
import importlib.util
import re
import shutil
from pathlib import Path

import pytest

import friends
import kitconfig

KIT = Path(__file__).resolve().parent
REPO = KIT.parent
SOURCE = (KIT / "friends.py").read_text()
EXAMPLE = KIT / "fixtures" / "example.toml"
LINK = "https://login.tailscale.com" + "/admin/invite/" + "FakeShare0123abcd"
TODAY = datetime.date(2026, 10, 20)


def no_discord_toml(text):
    """example.toml with its [discord] section replaced by the no-Discord choice."""
    return re.sub(r"(?ms)^\[discord\]\n.*?(?=^\[)", "[discord]\nenabled = false\n\n", text)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """-> (the module, the out file). kit.toml says no Discord; discord/.env holds the fake link; the stand-in repo
    holds compose's pack line and that pack in dist/."""
    kit = tmp_path / "kit.toml"
    kit.write_text(no_discord_toml(EXAMPLE.read_text()))
    monkeypatch.setenv(kitconfig.PATH_ENV, str(kit))
    repo = tmp_path / "repo"
    (repo / "dist").mkdir(parents=True)
    shutil.copy(REPO / "docker-compose.yml", repo / "docker-compose.yml")
    (repo / "dist" / kitconfig.live_pack_file(REPO)).write_bytes(b"PK fake mrpack")
    env = tmp_path / "discord.env"
    env.write_text(f"TAILSCALE_SHARE_LINK={LINK}\nTAILSCALE_SHARE_LINK_DATE=2026-10-19\n")
    sync = friends._sync()
    monkeypatch.setattr(sync, "DOTENV", env)
    monkeypatch.setattr(friends, "REPO", repo)
    return friends, tmp_path / "home" / "minecraft-friend-message.txt"


def run(mod, out, *args):
    out.parent.mkdir(exist_ok=True)
    return mod.main(["--out", str(out), *args], today=TODAY)


def test_it_writes_the_message_at_mode_600_and_prints_only_the_path(setup, capsys):
    mod, out = setup
    assert run(mod, out) == 0
    shown = capsys.readouterr()
    assert str(out) in shown.out and LINK not in shown.out + shown.err and "FakeShare" not in shown.out + shown.err
    assert out.stat().st_mode & 0o777 == 0o600
    text = out.read_text()
    cfg = kitconfig.load()
    for want in (LINK, cfg["tailscale"]["host_name"], cfg["tailscale"]["server_address"], "Vanilla Extended",
                 kitconfig.live_pack_file(REPO), str(mod.REPO / "dist"), "Minecraft name"):
        assert want in text, want
    body = text[text.index("----- copy from here -----"):]
    assert "{" not in body and "FOR YOU" not in body  # every token filled; the person's notes stay outside it


def test_check_passes_on_the_file_it_wrote_and_fails_once_anything_changed(setup, capsys, tmp_path):
    mod, out = setup
    assert run(mod, out, "--check") == 1 and "missing" in capsys.readouterr().out
    assert run(mod, out) == 0
    assert run(mod, out, "--check") == 0
    out.chmod(0o644)
    assert run(mod, out, "--check") == 1 and "want 600" in capsys.readouterr().out
    out.chmod(0o600)
    (tmp_path / "discord.env").write_text(f"TAILSCALE_SHARE_LINK={LINK}x\nTAILSCALE_SHARE_LINK_DATE=2026-10-20\n")
    assert run(mod, out, "--check") == 1  # a fresh link (R1): the message on disk is stale
    shown = capsys.readouterr()
    assert "stale" in shown.out and "FakeShare" not in shown.out + shown.err
    assert run(mod, out) == 0 and run(mod, out, "--check") == 0  # written again: current


def test_it_refuses_with_discord(setup, monkeypatch, capsys):
    mod, out = setup
    monkeypatch.setenv(kitconfig.PATH_ENV, str(EXAMPLE))
    assert run(mod, out) == 2 and not out.exists()
    assert "#setup" in capsys.readouterr().err


@pytest.mark.parametrize("env,says", [("", "missing or empty"),
                                      ("TAILSCALE_SHARE_LINK=https://example.invalid/x\n", "not a Tailscale share")])
def test_it_refuses_without_a_share_link(setup, tmp_path, capsys, env, says):
    mod, out = setup
    (tmp_path / "discord.env").write_text(env)
    assert run(mod, out) == 2 and not out.exists()
    assert says in capsys.readouterr().err


def test_it_refuses_an_unbuilt_pack(setup):
    mod, out = setup
    for f in (mod.REPO / "dist").iterdir():
        f.unlink()
    assert run(mod, out) == 2 and not out.exists()


def test_it_never_writes_through_a_link(setup, tmp_path):
    mod, out = setup
    target = tmp_path / "elsewhere"
    target.write_text("someone else's file\n")
    out.parent.mkdir()
    out.symlink_to(target)
    assert run(mod, out) == 2
    assert target.read_text() == "someone else's file\n" and out.is_symlink()


def test_it_never_replaces_a_file_someone_else_owns(setup, monkeypatch, capsys):
    """A file at the path that another user owns (say root left one there) is refused, not replaced. Owning it is
    simulated: the file is the runner's, and geteuid answers another (non-root) uid."""
    mod, out = setup
    out.parent.mkdir()
    out.write_text("someone else's file\n")
    other = out.stat().st_uid + 1 or 1
    monkeypatch.setattr(mod.os, "geteuid", lambda: other)
    assert run(mod, out) == 2
    assert out.read_text() == "someone else's file\n" and "belongs to uid" in capsys.readouterr().err


def test_root_is_refused(setup, monkeypatch):
    mod, out = setup
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    assert run(mod, out) == 2 and not out.exists()


def test_the_file_is_never_readable_by_others_even_before_its_chmod(setup, monkeypatch):
    """The link is written into a file created at 600, so no umask leaves it readable before os.chmod runs."""
    mod, out = setup
    monkeypatch.setattr(mod.os, "chmod", lambda *a: None)
    old = mod.os.umask(0)
    try:
        assert run(mod, out) == 0
    finally:
        mod.os.umask(old)
    assert out.stat().st_mode & 0o777 == 0o600


# ---- each guard catches its mutant ---------------------------------------------------------------------------------

def load(text, tmp_path):
    path = tmp_path / "friends_mut.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location("friends_mut", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GUARDS = {
    # name: (the guard as written, its mutant, the test body that must now fail)
    "discord_refusal": ("if kitconfig.discord_enabled(cfg):", "if False:", "discord"),
    "mode_600": ("fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
                 "fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)", "born"),
    "chmod_600": ("os.chmod(tmp, 0o600)", "os.chmod(tmp, 0o644)", "write"),
    "path_only": ('print(f"wrote {out} (mode 600;', 'print(f"wrote {out} {text} (mode 600;', "write"),
    "stale_check": ('if path.read_bytes() != text.encode("utf-8"):', "if False:", "check"),
    "link_refusal": ("if path.is_symlink() or (path.exists() and not path.is_file()):", "if False:", "link"),
    "root_refusal": ("if os.geteuid() == 0:", "if False:", "root"),
    "owner_refusal": ("if path.exists() and path.stat().st_uid != os.geteuid():", "if False:", "owner"),
}


@pytest.mark.parametrize("name", sorted(GUARDS))
def test_each_guard_catches_its_mutant(setup, tmp_path, monkeypatch, capsys, name):
    guard, mutant, body = GUARDS[name]
    assert SOURCE.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    mod = load(SOURCE.replace(guard, mutant), tmp_path)
    monkeypatch.setattr(mod, "REPO", friends.REPO)  # the stand-in repo
    monkeypatch.setattr(mod, "TEMPLATE", friends.TEMPLATE)  # the mutant lives in tmp_path, the template in kit/
    _, out = setup
    tests = {
        "discord": lambda: test_it_refuses_with_discord((mod, out), monkeypatch, capsys),
        "write": lambda: test_it_writes_the_message_at_mode_600_and_prints_only_the_path((mod, out), capsys),
        "check": lambda: test_check_passes_on_the_file_it_wrote_and_fails_once_anything_changed((mod, out), capsys,
                                                                                                  tmp_path),
        "link": lambda: test_it_never_writes_through_a_link((mod, out), tmp_path),
        "root": lambda: test_root_is_refused((mod, out), monkeypatch),
        "owner": lambda: test_it_never_replaces_a_file_someone_else_owns((mod, out), monkeypatch, capsys),
        "born": lambda: test_the_file_is_never_readable_by_others_even_before_its_chmod((mod, out), monkeypatch),
    }
    with pytest.raises(AssertionError):
        tests[body]()


def test_the_template_names_only_what_values_fills(setup):
    mod, _ = setup
    vals, _ = mod.values(kitconfig.load(), {"TAILSCALE_SHARE_LINK": LINK}, TODAY, repo=mod.REPO)
    assert set(friends.TOKEN.findall(friends.TEMPLATE.read_text() + friends.HEADER)) == set(vals)
