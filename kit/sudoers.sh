#!/usr/bin/env bash
# kit/sudoers.sh: the kit user's NOPASSWD sudo drop-in (HUMAN-STEPS step 7), from kit/sudoers.in.
#
#   sudo bash kit/sudoers.sh install     the human runs this once (one password prompt); afterwards Claude needs none
#   sudo bash kit/sudoers.sh check       is it installed, ours, and valid?
#   sudo bash kit/sudoers.sh remove      take it out (then every privileged step needs a password again)
#
# The user is $SUDO_USER: whoever ran sudo. It runs before kit.toml exists (step 7 precedes the render).
# install: renders the template, validates the candidate with `visudo -cf` BEFORE it is put in place, installs it
# root:root 0440 with a rename, then validates the whole sudoers config with `visudo -c`; if that fails it puts back
# what was there. A file at the destination that this script did not write is never overwritten or removed.
# The file name has no '.' or '~': sudo's @includedir silently skips such names, and the grant would do nothing.
# Tests: SUDOERS_DIR and SUDOERS_MAIN replace /etc/sudoers.d and /etc/sudoers; commands come from PATH.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
MAIN="${SUDOERS_MAIN:-/etc/sudoers}"
NAME=90-mc-maintainer   # = asus-node's drop-in name (granted 2026-09-29)
DEST="${DIR}/${NAME}"

die() { echo "sudoers: refusing: $*" >&2; exit 1; }
case "$NAME" in *.* | *~*) die "drop-in name '${NAME}' has a '.' or '~': sudo would skip it" ;; esac

cmd="${1:-}"
case "$cmd" in install | check | remove) ;; *) echo "usage: sudo bash kit/sudoers.sh install | check | remove" >&2; exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || die "run it with sudo (sudo bash kit/sudoers.sh ${cmd})"
user="${SUDO_USER:-}"
[[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] && [ "$user" != root ] \
  || die "SUDO_USER is '${user}': run it with sudo from the kit user's own account"

rendered() { sed "s/@MC_USER@/${user}/g" "${KIT_DIR}/sudoers.in"; }
# ours: DEST carries the template's first line, so this script wrote it (a hand-made grant is never touched).
ours() { [ -f "$DEST" ] && [ "$(head -1 "$DEST")" = "$(head -1 "${KIT_DIR}/sudoers.in")" ]; }

case "$cmd" in
  check)
    if [ ! -e "$DEST" ]; then echo "sudoers: ${DEST} not installed"; exit 1; fi
    ours || { echo "sudoers: ${DEST} exists but was not written by the kit"; exit 1; }
    [ "$(rendered)" = "$(cat "$DEST")" ] || { echo "sudoers: ${DEST} differs from kit/sudoers.in for ${user}"; exit 1; }
    [ "$(stat -c '%a' "$DEST")" = 440 ] || { echo "sudoers: ${DEST} mode $(stat -c %a "$DEST"), want 440"; exit 1; }
    visudo -cf "$DEST" >/dev/null || { echo "sudoers: ${DEST} does not parse"; exit 1; }
    echo "sudoers: ${DEST} installed for ${user}, valid"
    ;;
  install)
    if [ -e "$DEST" ] && ! ours; then
      die "${DEST} exists and was not written by the kit; nothing changed (read it, then move it aside yourself)"
    fi
    if ours && [ "$(rendered)" = "$(cat "$DEST")" ] && [ "$(stat -c '%a' "$DEST")" = 440 ]; then
      echo "sudoers: unchanged ${DEST}"
      exit 0
    fi
    tmp="${DEST}.kit-tmp"   # has a '.': sudo's includedir ignores it while it is being checked
    prev="${DEST}.kit-prev"
    rendered >"$tmp"
    chmod 440 "$tmp"
    if ! visudo -cf "$tmp" >/dev/null; then
      rm -f "$tmp"
      die "the rendered drop-in does not parse (visudo -cf); nothing installed"
    fi
    rm -f "$prev"
    [ -e "$DEST" ] && cp -p "$DEST" "$prev"
    mv -f "$tmp" "$DEST"
    if ! visudo -cf "$MAIN" >/dev/null; then
      if [ -e "$prev" ]; then mv -f "$prev" "$DEST"; else rm -f "$DEST"; fi
      die "the sudoers config failed visudo -c with the drop-in in place; it was taken back out"
    fi
    rm -f "$prev"
    echo "sudoers: installed ${DEST} (${user} ALL=(ALL:ALL) NOPASSWD: ALL); visudo -c OK"
    ;;
  remove)
    if [ ! -e "$DEST" ]; then echo "sudoers: nothing to remove (${DEST} absent)"; exit 0; fi
    ours || die "${DEST} was not written by the kit; nothing removed"
    rm -f "$DEST"
    echo "sudoers: removed ${DEST}; privileged steps need a password again"
    ;;
esac
