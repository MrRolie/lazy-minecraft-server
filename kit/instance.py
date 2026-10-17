"""Does this tree hold the asus-node instance record, or is it a copy without it? The one predicate that code
(discord/sync.py's P6 stop) and the tests (every "asus-node record absent" skip) ask. Stdlib only.

kit/INSTANCE-PATHS lists every instance path. The record is its first four lines (RECORD): asus-node's runbook
part, its kit.toml, the files it reproduces byte for byte, and its values. Their names carry "asus-node", so no copy
makes them by accident, while a copy may well grow a docs/direction/ or a discord/posts/ of its own. So:
- every RECORD path on disk: the live tree (record() is True);
- none on disk: a copy (False);
- some: a damaged tree or a half-made copy. That refuses (InstanceError), because the P6 stop cannot tell which
  guild it protects, and a test that skipped there would stay green with nothing checked.
On disk means os.path.lexists: a broken symlink counts as there, and reading it then refuses."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "kit" / "INSTANCE-PATHS"
# A second statement of the manifest's first lines; kit/test_instance_paths.py fails when they disagree.
RECORD = ("docs/instance/asus-node.md", "kit/fixtures/asus-node.toml", "kit/fixtures/asus-node/",
          "kit/fixtures/asus-node.values")
NO_RECORD = "the asus-node instance record is absent (a copy without kit/INSTANCE-PATHS)"


class InstanceError(ValueError):
    pass


def entries(text=None):
    """kit/INSTANCE-PATHS: its path lines (comments and blank lines dropped)."""
    if text is None:
        try:
            text = MANIFEST.read_text()
        except OSError as e:
            raise InstanceError(f"cannot read kit/INSTANCE-PATHS ({e}); it is kept in every copy") from None
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def split(paths, repo=REPO):
    """-> (on disk, not on disk)."""
    there = [p for p in paths if os.path.lexists(repo / p.rstrip("/"))]
    return there, [p for p in paths if p not in there]


def record(repo=REPO):
    """True: the live tree (the whole record is here). False: a copy (none of it is). A part refuses."""
    there, gone = split(RECORD, repo)
    if there and gone:
        raise InstanceError(f"part of the asus-node instance record is missing: {', '.join(gone)} (still here: "
                            f"{', '.join(there)}). The tree is damaged, or a copy kept part of kit/INSTANCE-PATHS; "
                            "restore it from git (live tree) or drop every manifest path (copy). Refusing")
    return bool(there)
