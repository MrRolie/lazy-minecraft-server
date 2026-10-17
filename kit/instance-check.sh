#!/usr/bin/env bash
# kit/instance-check.sh [REV]: does a copy of this repo without the instance paths still work, and is it clean?
#
# A copy for anyone else (a public snapshot, a friend's fork) drops every path in kit/INSTANCE-PATHS. This script
# makes that copy from REV (default HEAD; committed content only, never the working tree), then:
#   1. reads the instance's values (kit/fixtures/asus-node.values, grep -E patterns) BEFORE dropping anything, since
#      the values file is itself an instance path;
#   2. drops every manifest path, and refuses a line that names nothing in REV (a stale manifest drops nothing);
#   3. runs the full test suite in the copy: any failure or error fails the check, and every skip prints its reason;
#   4. greps the copy for the values, binaries included: any hit fails the check, and so does a grep error.
# Exit 0 only when all four pass. The copy is made under the repo (a gitignored test-compose-* dir) because snap
# Docker cannot read /tmp and the compose tests need Docker; it is removed on exit (KEEP=1 keeps it).
set -euo pipefail

# read_values FILE: its patterns, one per line. A CR or a trailing blank would stay in a pattern and silently stop it
# matching, so both are stripped; comments and blank lines are dropped.
read_values() {
    tr -d '\r' < "$1" | sed -E 's/[[:space:]]+$//' | { grep -vE '^[[:space:]]*(#|$)' || true; }
}

# value_scan PATTERNS TREE: 0 when no file under TREE (binaries too: -a) matches a pattern; otherwise it prints what
# it found and returns 1. Only grep's exit 1 is "no value found": 0 is a hit, and 2 is an error (an unreadable file, a
# bad pattern) that may hide one, so both fail.
value_scan() {
    local values=$1 tree=$2 hits rc=0 err links
    # grep -r skips a symlink it meets, so a link (and whatever it points at) would never be scanned: refuse any.
    links=$(find "$tree" -path "$tree/.git" -prune -o -type l -print)
    if [ -n "$links" ]; then
        printf '  %s\n' "$links"
        echo "FAIL: the copy holds symlinks, which the value grep cannot vouch for"
        return 1
    fi
    err=$(mktemp)
    hits=$(grep -rlaE -f "$values" --exclude-dir=.git "$tree" 2> "$err") || rc=$?
    if [ "$rc" -eq 0 ]; then
        while IFS= read -r f; do
            echo "  ${f#"$tree"/}"
            grep -naE -f "$values" "$f" | cut -c1-200 | sed 's/^/    /' || true
        done <<< "$hits"
        echo "FAIL: $(printf '%s\n' "$hits" | wc -l) file(s) in the copy still carry an instance value"
    elif [ "$rc" -ne 1 ]; then
        cat "$err"
        echo "FAIL: the value grep could not read the copy (grep exit $rc); an error is never a clean result"
    else
        echo "  0 hits"
    fi
    rm -f "$err"
    [ "$rc" -eq 1 ]
}

# Sourced (kit/test_instance_paths.py tests the two functions above): define them and run nothing.
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

rev=${1:-HEAD}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
sha=$(git -C "$repo" rev-parse --verify "$rev^{commit}")
work=$(mktemp -d "$repo/test-compose-instance-check-XXXXXX")
[ "${KEEP:-0}" = 1 ] || trap 'rm -rf "$work"' EXIT
tree=$work/tree
mkdir "$tree"
git -C "$repo" archive "$sha" | tar -x -C "$tree"

manifest=$tree/kit/INSTANCE-PATHS
values=$tree/kit/fixtures/asus-node.values
[ -f "$manifest" ] || { echo "FAIL: $sha has no kit/INSTANCE-PATHS"; exit 1; }
[ -f "$values" ] || { echo "FAIL: $sha has no kit/fixtures/asus-node.values"; exit 1; }
read_values "$values" > "$work/values"
[ -s "$work/values" ] || { echo "FAIL: kit/fixtures/asus-node.values lists no value"; exit 1; }

echo "== $sha: dropping the instance paths (kit/INSTANCE-PATHS)"
mapfile -t paths < <(grep -vE '^[[:space:]]*(#|$)' "$manifest")
[ "${#paths[@]}" -gt 0 ] || { echo "FAIL: kit/INSTANCE-PATHS lists no path"; exit 1; }
for p in "${paths[@]}"; do
    case "$p" in
        /*|*..*|*' '*) echo "FAIL: manifest line '$p' is not a plain repo-relative path"; exit 1 ;;
    esac
    [ -e "$tree/$p" ] || { echo "FAIL: manifest line '$p' names nothing in $sha"; exit 1; }
    rm -rf "${tree:?}/$p"
    echo "  dropped $p"
done

# The tests read `git ls-files`, so the copy is a repository of its own (one commit, no history).
git -C "$tree" init -q
git -C "$tree" add -A
git -C "$tree" -c user.name=instance-check -c user.email=instance-check@example.invalid commit -qm copy

echo "== the full suite in the copy"
status=0
(cd "$tree" && PYTHONPYCACHEPREFIX="$work/pycache" uv run --with pytest --with discord.py==2.7.1 \
    pytest host/ pack/ discord/ bridge/ mod/ kit/ -q -rs -p no:cacheprovider) || status=$?
if [ "$status" -ne 0 ]; then
    echo "FAIL: the suite failed in the copy (pytest exit $status)"
    exit 1
fi

echo "== the instance's values in the copy ($(wc -l < "$work/values") patterns)"
value_scan "$work/values" "$tree" || exit 1
echo "PASS: $sha without kit/INSTANCE-PATHS passes the suite and carries no instance value"
