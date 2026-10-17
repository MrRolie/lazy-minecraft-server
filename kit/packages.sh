#!/usr/bin/env bash
# kit/packages.sh: KIT.md P2, the package phase. Root, idempotent.
#
#   sudo bash kit/packages.sh
#
# Installs what host/ assumes and never installs itself:
#   python3                  kit/render.py, pack/build.py, the host/bin scripts
#   Docker Engine            docker-ce + compose plugin from DOCKER'S apt repo (docs.docker.com/engine/install/ubuntu,
#                            2026-10-14), not the snap and not Ubuntu's docker.io: host/conf/apt-52mc-host blacklists
#                            the docker-ce packages, and snap Docker cannot bind outside $HOME
#   Tailscale                from Tailscale's apt repo (pkgs.tailscale.com/stable, 2026-10-14), which
#                            apt-52mc-host's Origins-Pattern (origin=Tailscale) matches for the 05:00 upgrades
# and adds the kit user ($SUDO_USER) to the docker group (host/check.sh and the kit user's compose calls need it).
#
# Each repo key is checked against its pinned fingerprint before apt trusts it; a mismatch refuses. Conflicting
# Docker packages are REFUSED, never removed. So is any apt source for download.docker.com or pkgs.tailscale.com
# the kit did not write byte for byte (a second source with another Signed-By makes apt refuse every source list,
# so every apt command fails, the 05:00 upgrades included); the refusal comes before anything is fetched or written.
# An installed package is left alone: never upgraded here (Docker upgrades are the manual stopped-server procedure,
# AGENTS.md). A second run prints "unchanged" for every step.
# The gate at the end runs `apt-get update` (every source reads and fetches, checked even when nothing was
# installed), `docker info`, `docker compose version` and `tailscale version`; any failure exits 1.
#
# Tests: PKG_ROOT prefixes every file path, commands come from PATH (kit/test_host_bootstrap.py).
set -euo pipefail

ROOT="${PKG_ROOT:-}"
KEYRINGS="${ROOT}/etc/apt/keyrings"
SOURCES="${ROOT}/etc/apt/sources.list.d"
DOCKER_KEY="${KEYRINGS}/docker.asc"
DOCKER_KEY_URL=https://download.docker.com/linux/ubuntu/gpg
# The primary key of "Docker Release (CE deb)", downloaded and read 2026-10-14.
DOCKER_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
DOCKER_PKGS=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
# docs.docker.com's "uninstall old versions" list: any of these installed alongside docker-ce breaks it.
DOCKER_CONFLICTS=(docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc)
TS_KEY="${ROOT}/usr/share/keyrings/tailscale-archive-keyring.gpg"
# "Tailscale Inc. (Package repository signing key)", downloaded and read 2026-10-14 (not published as a fingerprint
# by Tailscale: this pins that download, so a later key swap or a tampered download refuses).
TS_FPR=2596A99EAAB33821893C0A79458CA832957F5868

log() { printf '   %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { echo "refusing: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash kit/packages.sh"
KIT_USER="${SUDO_USER:-}"
[ -n "$KIT_USER" ] && [ "$KIT_USER" != root ] || die "run through sudo from the kit user's account (SUDO_USER is '${KIT_USER}')"
[ -r "${ROOT}/etc/os-release" ] || die "${ROOT}/etc/os-release is missing"
# shellcheck disable=SC1091
OS_ID="$(. "${ROOT}/etc/os-release" && echo "${ID:-}")"
# shellcheck disable=SC1091
CODENAME="$(. "${ROOT}/etc/os-release" && echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")"
[ "$OS_ID" = ubuntu ] || die "this is '${OS_ID}', not Ubuntu (the kit's repos are Ubuntu's)"
[[ "$CODENAME" =~ ^[a-z]+$ ]] || die "unreadable Ubuntu codename '${CODENAME}'"
ARCH="$(dpkg --print-architecture)"

# The two apt sources the kit writes (step 3), exactly.
DOCKER_SOURCES="${SOURCES}/docker.sources"
DOCKER_SOURCES_TEXT="Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${CODENAME}
Components: stable
Architectures: ${ARCH}
Signed-By: /etc/apt/keyrings/docker.asc"
TS_LIST="${SOURCES}/tailscale.list"
# The one line pkgs.tailscale.com's ${CODENAME}.tailscale-keyring.list holds (read 2026-10-14), written here
# rather than downloaded, so no fetched text becomes an apt source.
TS_LIST_TEXT="# Tailscale packages for ubuntu ${CODENAME}
deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu ${CODENAME} main"

# kit_wrote FILE TEXT: FILE holds TEXT plus put_file's newline, byte for byte.
kit_wrote() { printf '%s\n' "$2" | cmp -s - "$1"; }

# foreign_vendor_sources: every file apt reads as a source list (sources.list, sources.list.d/*.list and *.sources)
# that names download.docker.com or pkgs.tailscale.com outside a comment and is not the kit's own file byte for byte.
# One printed per line; an unreadable one counts (it cannot be judged).
foreign_vendor_sources() {
  local f rc
  for f in "${ROOT}/etc/apt/sources.list" "${SOURCES}"/*.list "${SOURCES}"/*.sources; do
    [ -e "$f" ] || continue
    rc=0
    grep -qE '^[[:space:]]*[^#[:space:]].*(download\.docker\.com|pkgs\.tailscale\.com)' "$f" || rc=$?
    case "$rc" in
      0) ;;
      1) continue ;;
      *) printf '%s (unreadable)\n' "${f#"${ROOT}"}"; continue ;;
    esac
    if [ "$f" = "$DOCKER_SOURCES" ] && kit_wrote "$f" "$DOCKER_SOURCES_TEXT"; then continue; fi
    if [ "$f" = "$TS_LIST" ] && kit_wrote "$f" "$TS_LIST_TEXT"; then continue; fi
    printf '%s\n' "${f#"${ROOT}"}"
  done
}

installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }

APT_UPDATED=0
apt_update() {
  if [ "$APT_UPDATED" = 0 ]; then
    apt-get update -qq
    APT_UPDATED=1
  fi
}
# apt_install PKG...: install the ones not yet installed; never upgrade an installed one.
apt_install() {
  local missing=() p
  for p in "$@"; do installed "$p" || missing+=("$p"); done
  if [ ${#missing[@]} -eq 0 ]; then
    log "unchanged: $* installed (never upgraded here)"
    return 0
  fi
  apt_update
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
  log "installed ${missing[*]}"
}

# fingerprint FILE: the primary key's fingerprint (a throwaway GNUPGHOME: root's keyrings are never touched).
fingerprint() {
  local home fpr
  home="$(mktemp -d)"
  fpr="$(GNUPGHOME="$home" gpg --batch --show-keys --with-colons "$1" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }')"
  rm -rf "$home"
  printf '%s' "$fpr"
}

# fetch_key URL DEST FPR: download to a temp file, refuse unless its fingerprint is FPR, then install it 644.
fetch_key() {
  local url=$1 dest=$2 want=$3 tmp have
  if [ -f "$dest" ] && [ "$(fingerprint "$dest")" = "$want" ]; then
    log "unchanged ${dest} (fingerprint ${want})"
    return 0
  fi
  tmp="$(mktemp)"
  curl -fsSL --retry 3 "$url" -o "$tmp" || { rm -f "$tmp"; die "download failed: ${url}"; }
  have="$(fingerprint "$tmp")"
  if [ "$have" != "$want" ]; then
    rm -f "$tmp"
    die "${url} has key fingerprint '${have}', not the pinned ${want}; nothing installed"
  fi
  install -D -m 644 "$tmp" "$dest"  # root runs this: root-owned
  rm -f "$tmp"
  log "installed ${dest} (fingerprint ${want})"
  SOURCES_CHANGED=1
}

# put_file DEST CONTENT: write DEST (644) only if it differs.
put_file() {
  local dest=$1 content=$2
  if [ -f "$dest" ] && [ "$(cat "$dest")" = "$content" ]; then
    log "unchanged ${dest}"
    return 0
  fi
  install -d -m 755 "$(dirname "$dest")"
  printf '%s\n' "$content" >"${dest}.kit-tmp"
  chmod 644 "${dest}.kit-tmp"
  mv "${dest}.kit-tmp" "$dest"
  log "wrote ${dest}"
  SOURCES_CHANGED=1
}

SOURCES_CHANGED=0

step "1. refuse a conflicting Docker, or a Docker/Tailscale apt source the kit did not write"
conflicts=()
for p in "${DOCKER_CONFLICTS[@]}"; do installed "$p" && conflicts+=("$p"); done
if command -v snap >/dev/null && snap list docker >/dev/null 2>&1; then conflicts+=("snap:docker"); fi
if [ ${#conflicts[@]} -gt 0 ]; then
  die "conflicting Docker install(s): ${conflicts[*]}. Docker's docs say to remove them before docker-ce; that removes their containers' runtime, so it is yours to do (sudo apt-get remove ... / sudo snap remove docker), then re-run"
fi
log "no conflicting Docker packages"
foreign="$(foreign_vendor_sources)"
if [ -n "$foreign" ]; then
  die "apt source(s) for download.docker.com or pkgs.tailscale.com that the kit did not write: $(tr '\n' ' ' <<<"$foreign")
  The kit writes /etc/apt/sources.list.d/docker.sources and tailscale.list (kit/packages.sh); a second source for the
  same repo with another Signed-By makes apt refuse every source list, and then every apt command fails (the 05:00
  upgrades included). Compare, then move the other source out of /etc/apt yourself and re-run. Nothing changed"
fi
log "no conflicting Docker or Tailscale apt source (none, or only the kit's own)"

step "2. base packages (python3, curl, gnupg, ca-certificates)"
apt_install python3 ca-certificates curl gnupg

step "3. Docker's and Tailscale's apt repos (keys checked against pinned fingerprints)"
install -d -m 755 "$KEYRINGS"
fetch_key "$DOCKER_KEY_URL" "$DOCKER_KEY" "$DOCKER_FPR"
put_file "$DOCKER_SOURCES" "$DOCKER_SOURCES_TEXT"
fetch_key "https://pkgs.tailscale.com/stable/ubuntu/${CODENAME}.noarmor.gpg" "$TS_KEY" "$TS_FPR"
put_file "$TS_LIST" "$TS_LIST_TEXT"
if [ "$SOURCES_CHANGED" = 1 ]; then
  APT_UPDATED=0  # the new sources must be read before the installs below
fi

step "4. Docker Engine + Compose plugin, Tailscale"
apt_install "${DOCKER_PKGS[@]}"
apt_install tailscale

step "5. the kit user in the docker group"
if id -nG "$KIT_USER" | tr ' ' '\n' | grep -qx docker; then
  log "unchanged: ${KIT_USER} is in the docker group"
else
  usermod -aG docker "$KIT_USER"
  log "added ${KIT_USER} to the docker group (takes effect at the next login; until then: sg docker -c ...)"
fi

step "gate"
fails=0
gate() {
  local what=$1
  shift
  if out="$("$@" 2>&1)"; then
    printf 'PASS  %s: %s\n' "$what" "$(head -1 <<<"$out")"
  else
    printf 'FAIL  %s: %s\n' "$what" "$(head -3 <<<"$out" | tr '\n' ' ')"
    fails=$((fails + 1))
  fi
}
# apt_reads_its_sources: `apt-get update` exits 0 with no E: line. Run even when nothing was installed (an install
# would have run it; an all-installed host would otherwise never read the sources, and a broken list would pass).
apt_reads_its_sources() {
  local out rc=0
  out="$(apt-get update -qq 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ] || grep -q '^E:' <<<"$out"; then
    printf 'exit %s: %s\n' "$rc" "$(grep '^E:' <<<"$out" || printf '%s' "$out")"
    return 1
  fi
  echo "every source read and fetched"
}
gate "apt-get update (the source lists read; the 05:00 upgrades need them)" apt_reads_its_sources
gate "docker info (the daemon answers)" docker info --format '{{.ServerVersion}}'
gate "docker compose version" docker compose version
gate "tailscale version" tailscale version
if [ "$fails" -gt 0 ]; then
  echo "packages: ${fails} gate(s) failed; exit 1"
  exit 1
fi
echo "packages: every gate passed"
