"""kit/INSTANCE-PATHS, the one home of what a copy of this repo drops; kit/instance.py, the one predicate for "does
this tree hold the record"; and kit/instance-check.sh, which proves the rest works without it. These run in every
tree: the live one (the record is here) and a copy (it is not).
Run: uv run --with pytest pytest kit/"""
import os
import re
import subprocess
from pathlib import Path

import pytest

import instance

KIT = Path(__file__).resolve().parent
REPO = KIT.parent
MANIFEST = instance.MANIFEST
VALUES = KIT / "fixtures" / "asus-node.values"
CHECK = KIT / "instance-check.sh"
NO_RECORD = instance.NO_RECORD
RECORD = instance.record()  # part of a record raises: a damaged live tree fails the run, never skips its pins
entries = instance.entries


def tracked(root=REPO):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def covered(path, lines):
    return any(path == e or (e.endswith("/") and path.startswith(e)) for e in lines)


def problems(lines, files):
    """-> (present, missing, malformed) manifest lines, against the tracked files."""
    bad = [e for e in lines if e.startswith("/") or ".." in e.split("/") or " " in e or "\\" in e]
    present = [e for e in lines if e not in bad and any(covered(f, [e]) for f in files)]
    missing = [e for e in lines if e not in bad and e not in present]
    return present, missing, bad


def test_the_record_is_the_manifests_first_lines():
    """kit/instance.py RECORD restates the manifest's record section; renamed on one side only, the predicate would
    look for a record the manifest does not drop (or the reverse)."""
    assert instance.RECORD == tuple(entries()[:len(instance.RECORD)])
    assert "kit/fixtures/asus-node.values" in instance.RECORD and "kit/fixtures/asus-node.toml" in instance.RECORD


def test_the_manifest_names_its_paths_all_or_none():
    """With the record here (the live tree), every manifest line is tracked AND on disk: a stale line (a rename) or an
    unstaged delete fails here. In a copy the record is untracked as well as absent; the other lines are not checked
    there, because a fork may grow a docs/direction/ or a discord/posts/ of its own."""
    present, missing, bad = problems(entries(), tracked())
    assert not bad, f"not plain repo-relative paths: {bad}"
    assert entries(), "kit/INSTANCE-PATHS lists nothing"
    if RECORD:
        assert not missing, f"instance paths not tracked in the live tree: {missing}"
        assert instance.split(entries())[1] == [], "instance paths tracked but not on disk"
    else:
        assert not [e for e in present if e in instance.RECORD], f"a copy tracks part of the record: {present}"


@pytest.mark.parametrize("text,files,want", [
    ("a.md\nd/\n", ["a.md", "d/x", "k.py"], (["a.md", "d/"], [], [])),
    ("# c\n\na.md\nd/\n", ["k.py"], ([], ["a.md", "d/"], [])),
    ("a.md\nd/\n", ["a.md", "k.py"], (["a.md"], ["d/"], [])),               # a mix
    ("/etc/x\n../y\nd/../z\n", [], ([], [], ["/etc/x", "../y", "d/../z"])),
    ("d\n", ["d/x"], ([], ["d"], [])),                                        # a directory line must end in /
])
def test_the_manifest_rule_catches_a_mix_and_a_bad_line(text, files, want):
    assert problems(entries(text), files) == want


def standin(root, drop=()):
    for rel in instance.RECORD:
        if rel in drop:
            continue
        p = root / rel
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n")
    return root


def test_the_predicate_reads_all_none_or_refuses_a_part(tmp_path):
    assert instance.record(standin(tmp_path / "live")) is True
    (tmp_path / "copy").mkdir()
    assert instance.record(tmp_path / "copy") is False
    fork = tmp_path / "fork"  # a fork's own discord/posts/ and docs/direction/ do not make it the live tree
    (fork / "discord" / "posts").mkdir(parents=True)
    (fork / "docs" / "direction").mkdir(parents=True)
    assert instance.record(fork) is False
    for rel in instance.RECORD:
        part = standin(tmp_path / f"part-{rel.replace('/', '_')}", drop=(rel,))
        with pytest.raises(instance.InstanceError, match=f"record is missing: {re.escape(rel)}"):
            instance.record(part)
    broken = standin(tmp_path / "broken-link", drop=("kit/fixtures/asus-node.toml",))
    os.symlink(tmp_path / "nowhere", broken / "kit/fixtures/asus-node.toml")
    assert instance.record(broken) is True  # there; reading it then refuses (discord/test_sync.py)


def test_directory_lines_end_in_a_slash_and_file_lines_do_not():
    if not RECORD:
        pytest.skip(NO_RECORD)
    for e in entries():
        assert (REPO / e).is_dir() == e.endswith("/"), e


def test_the_check_script_parses_and_reads_this_manifest():
    r = subprocess.run(["bash", "-n", str(CHECK)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = CHECK.read_text()
    assert "kit/INSTANCE-PATHS" in text and "kit/fixtures/asus-node.values" in text
    # the values are read before the drop: the values file is itself an instance path
    assert text.index('> "$work/values"') < text.index('rm -rf "${tree:?}/$p"')
    assert covered("kit/fixtures/asus-node.values", entries())


def check_fn(call, tmp_path):
    """Source kit/instance-check.sh (which then runs nothing) and run one of its functions."""
    return subprocess.run(["bash", "-c", f'source "{CHECK}"; {call}'], cwd=tmp_path, capture_output=True, text=True)


def test_the_check_scripts_value_scan_fails_on_a_hit_and_on_an_error(tmp_path):
    """Only grep's "no match" passes: a hit fails, and so does an error (an unreadable file beside a real hit, a bad
    pattern), which once read as "0 hits". A binary file is scanned too."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "clean.md").write_text("nothing here\n")
    values = tmp_path / "values"
    values.write_text("\\bzorble\\b\n")
    r = check_fn(f'value_scan "{values}" "{tree}"', tmp_path)
    assert r.returncode == 0 and "0 hits" in r.stdout, r.stdout + r.stderr
    (tree / "blob.bin").write_bytes(b"\0\x01zorble\0")
    r = check_fn(f'value_scan "{values}" "{tree}"', tmp_path)
    assert r.returncode == 1 and "blob.bin" in r.stdout and "FAIL" in r.stdout
    (tree / "blob.bin").unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("zorble\n")
    (tree / "link.md").symlink_to(outside)  # grep -r would skip it
    r = check_fn(f'value_scan "{values}" "{tree}"', tmp_path)
    assert r.returncode == 1 and "symlinks" in r.stdout, r.stdout
    (tree / "link.md").unlink()
    (tree / "hit.md").write_text("zorble was here\n")
    (tree / "sub").mkdir()
    (tree / "sub" / "locked.md").write_text("x\n")
    (tree / "sub" / "locked.md").chmod(0)
    if os.access(tree / "sub" / "locked.md", os.R_OK):
        pytest.skip("root reads a mode-000 file")
    r = check_fn(f'value_scan "{values}" "{tree}"', tmp_path)  # a hit AND an error: grep exits 2
    assert r.returncode == 1 and "FAIL" in r.stdout, r.stdout
    (tree / "hit.md").unlink()
    r = check_fn(f'value_scan "{values}" "{tree}"', tmp_path)  # the error alone
    assert r.returncode == 1 and "could not read the copy" in r.stdout, r.stdout
    (tree / "sub" / "locked.md").chmod(0o644)
    values.write_text("(unclosed\n")
    r = check_fn(f'value_scan "{values}" "{tree}"', tmp_path)  # a bad pattern
    assert r.returncode == 1 and "could not read the copy" in r.stdout, r.stdout


def test_the_check_script_strips_a_cr_and_a_trailing_blank_from_the_values(tmp_path):
    values = tmp_path / "values"
    values.write_bytes(b"# a comment\r\n\\bzorble\\b\r\n\\bBOX\\b  \n\n")
    r = check_fn(f'read_values "{values}"', tmp_path)
    assert r.returncode == 0 and r.stdout == "\\bzorble\\b\n\\bBOX\\b\n", repr(r.stdout)


def value_patterns():
    """The values file's patterns. A CR or trailing blank would silently stop a pattern matching in the check
    script's grep (it reads the file as it is), so either refuses here."""
    lines = VALUES.read_bytes().decode().split("\n")
    assert not [ln for ln in lines if ln != ln.rstrip()], "kit/fixtures/asus-node.values: a CR or trailing blank"
    return [ln for ln in lines if ln.strip() and not ln.startswith("#")]


def grep(patterns, paths, cwd=REPO):
    """grep -E, as kit/instance-check.sh runs it (binary files too: -a): -> the matching lines. An error (exit 2) is
    a failure, never "no match"."""
    if not paths:
        return []
    r = subprocess.run(["grep", "-naE", *sum((["-e", p] for p in patterns), []), "--", *paths],
                       cwd=cwd, capture_output=True, text=True, errors="replace")
    assert r.returncode in (0, 1), r.stderr
    return r.stdout.splitlines() or ([] if r.returncode else ["(a match grep did not print)"])


def test_the_value_grep_reads_binaries_and_fails_on_an_error(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0\x01PK " + b"zorble" + b"\0\xff")
    assert grep([r"\bzorble\b"], ["blob.bin"], cwd=tmp_path)
    with pytest.raises(AssertionError):
        grep([r"\bzorble\b"], ["absent.txt"], cwd=tmp_path)


def test_no_kept_file_carries_an_instance_value():
    """The check script's value grep, run here on the live tree so a value never reaches a shared file in the first
    place: every tracked file outside the manifest is free of every value."""
    if not RECORD:
        pytest.skip(NO_RECORD)
    kept = [f for f in tracked() if not covered(f, entries())]
    assert grep(value_patterns(), kept) == []


def test_every_value_pattern_finds_its_value_in_the_record():
    """A pattern that matches nothing (a typo, a bad escape) would let its value through unseen: each one must hit at
    least one file inside the instance paths."""
    if not RECORD:
        pytest.skip(NO_RECORD)
    inside = [f for f in tracked() if covered(f, entries()) and f != "kit/fixtures/asus-node.values"]
    dead = [p for p in value_patterns() if not grep([p], inside)]
    assert not dead, dead
    for p in value_patterns():
        re.compile(p)  # and Python reads it the same way (no grep-only syntax)
