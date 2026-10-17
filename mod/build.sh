#!/usr/bin/env bash
# Build the ve-commands jar (mod/README.md "Build"), prove it is the released one, and put it in extras/.
#
#   bash mod/build.sh            as the kit user, never root, from anywhere (kit phase P5, docs/kit/KIT.md)
#   bash mod/build.sh --check    the preflight and the extras/ state only: no build, nothing written
#
# 1. Preflight, before any build: reads the version from mod/ve-commands/gradle.properties and refuses unless
#    mod/RELEASE.sha256 pins ve-commands-<version>.jar exactly once and docker-compose.yml's MODS line names that
#    same jar. Otherwise the jar built here is not the one compose deploys (a release in progress: mod/README.md
#    "Release" is a maintainer step, never a kit install's).
# 2. extras/ is append-only here. Nothing in it is ever deleted, moved or overwritten: older jars stay for rollback.
#    - extras/<jar> already there with the pinned sha256: "unchanged", and no build.
#    - extras/<jar> there with other bytes: refused, before the build. itzg's mcopy decides "up to date" by size
#      alone, so a same-name swap might never deploy; the person moves it aside, never this script.
#    - A symlinked extras/ or extras/<jar>, or an extras/<jar> that is not a regular file: refused.
# 3. Builds in Docker (eclipse-temurin:25-jdk, `./gradlew --no-daemon clean build`, which also runs the unit tests;
#    the whole mod/ is mounted because they read mod/contract-examples/). The host needs no JDK. The Gradle home
#    sits under $HOME: snap Docker cannot bind-mount /tmp.
# 4. The gate: the built jar's sha256 must equal the pin. The build is reproducible, so a mismatch means the source,
#    the toolchain or the pin is wrong, and nothing is installed.
# 5. Installs it as extras/<jar>, mode 644 (itzg reads it as uid 1000), with a hard link, which never replaces an
#    existing file, then checks the installed copy with sha256sum -c.
# host/check.sh's extras lines (mod/check_extras.py) then check the deployed side.
# Overrides, for tests and spikes only: VE_BUILD_IMAGE, VE_GRADLE_HOME, VE_BUILD_NAME (the build container's name).
set -euo pipefail

MOD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$MOD_DIR")"
IMAGE="${VE_BUILD_IMAGE:-eclipse-temurin:25-jdk}"
GRADLE_HOME_DIR="${VE_GRADLE_HOME:-${HOME}/.cache/ve-commands-gradle}"
NAME="${VE_BUILD_NAME:-ve-commands-build}"

die() {
  echo "refusing: $*" >&2
  exit 1
}

CHECK=0
case "${1:-}" in
  "") ;;
  --check) CHECK=1 ;;
  *) die "unknown argument '$1' (usage: bash mod/build.sh [--check])" ;;
esac

[ "$(id -u)" -ne 0 ] || die "run as the kit user, not root (extras/ and the Gradle home must be the kit user's)"

# ---- preflight --------------------------------------------------------------------------------------------------
version="$(sed -n 's/^version=//p' "${MOD_DIR}/ve-commands/gradle.properties")"
[[ "$version" =~ ^[0-9A-Za-z.+-]+$ ]] || die "no single usable version= in mod/ve-commands/gradle.properties ('${version}')"
jar="ve-commands-${version}.jar"

# The pin: exactly one "<sha256>  <jar>" line for this jar.
mapfile -t pin_lines < <(awk -v j="$jar" 'NF == 2 && $2 == j' "${MOD_DIR}/RELEASE.sha256")
[ "${#pin_lines[@]}" -eq 1 ] \
  || die "mod/RELEASE.sha256 has ${#pin_lines[@]} pin(s) for ${jar}, want exactly 1 (a release in progress? mod/README.md \"Release\")"
want="${pin_lines[0]%% *}"
[[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "the pin for ${jar} in mod/RELEASE.sha256 is not a lowercase sha256"

mods="$(sed -n 's|^ *MODS: *"/extras/\([^"]*\)" *$|\1|p' "${REPO}/docker-compose.yml")"
[ "$mods" = "$jar" ] || die "docker-compose.yml MODS names '${mods}', not ${jar}: the jar built here would not be the one deployed"

extras="${REPO}/extras"
[ ! -L "$extras" ] || die "${extras} is a symlink"
[ ! -e "$extras" ] || [ -d "$extras" ] || die "${extras} exists and is not a directory"
target="${extras}/${jar}"
if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
  die "${target} exists and is not a regular file"
fi
if [ -f "$target" ]; then
  have="$(sha256sum "$target" | cut -d' ' -f1)"
  [ "$have" = "$want" ] \
    || die "${target} exists with other bytes (sha256 ${have}, pin ${want}); it is never overwritten: move it aside yourself, then re-run"
  echo "PASS ${target} matches mod/RELEASE.sha256"
  echo "unchanged ${target} (nothing built)"
  exit 0
fi
echo "PASS preflight: ${jar} is pinned once in mod/RELEASE.sha256 and named by docker-compose.yml MODS"
if [ "$CHECK" = 1 ]; then
  echo "--check: ${target} is not there yet; 'bash mod/build.sh' builds and installs it"
  exit 0
fi

# ---- build ------------------------------------------------------------------------------------------------------
built="${MOD_DIR}/ve-commands/build/libs/${jar}"
rm -f -- "$built"  # a build that writes nothing must never pass on an earlier jar (this is the build dir, not extras/)
mkdir -p "$GRADLE_HOME_DIR"  # else Docker creates it root-owned and Gradle cannot write
echo "== building ${jar} in ${IMAGE} (Gradle home ${GRADLE_HOME_DIR}; a few minutes)"
docker run --rm --name "$NAME" -u "$(id -u):$(id -g)" \
  -e GRADLE_USER_HOME=/gradle-home -e HOME=/gradle-home \
  -v "${GRADLE_HOME_DIR}:/gradle-home" \
  -v "${MOD_DIR}:/src" -w /src/ve-commands \
  "$IMAGE" ./gradlew --no-daemon clean build \
  || die "the Gradle build failed (its output is above); nothing installed"

# ---- the gate: the built jar is the released one -----------------------------------------------------------------
[ -f "$built" ] || die "the build exited 0 but left no ${built}"
have="$(sha256sum "$built" | cut -d' ' -f1)"
[ "$have" = "$want" ] || die "built ${jar} sha256 ${have} differs from mod/RELEASE.sha256 ${want}; nothing installed"
echo "PASS built ${jar} sha256 matches mod/RELEASE.sha256"

# ---- install (never replaces: ln fails if the name exists) ------------------------------------------------------
install -d -m 755 "$extras"
[ ! -L "$extras" ] || die "${extras} is a symlink"
tmp="$(mktemp "${extras}/.${jar}.XXXXXX")"
trap 'rm -f -- "$tmp"' EXIT
cp -- "$built" "$tmp"
chmod 644 "$tmp"
ln -- "$tmp" "$target" || die "${target} appeared during the build; it was left as it is"
rm -f -- "$tmp"
trap - EXIT
(cd "$extras" && printf '%s  %s\n' "$want" "$jar" | sha256sum -c --quiet) || die "the installed ${target} does not verify"
echo "installed ${target} (sha256 verified against mod/RELEASE.sha256)"
