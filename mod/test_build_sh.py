"""Tests for mod/build.sh, the ve-commands build wrapper (kit plan gap #13), against a stub docker and id.

No real build runs here: the stub `docker run` logs its arguments and writes the bytes a test chose where Gradle
would. Each guard has a scenario that must refuse, and a mutation test that disables the guard in a copy of the
script and shows that the same scenario then goes wrong. A guard test only counts once it catches its mutant, and a
guard text that moved fails as a stale pattern, never a silent skip.
Run: uv run --with pytest pytest mod/
"""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

MOD = Path(__file__).resolve().parent
SCRIPT = MOD / "build.sh"
VERSION = "9.9.9"
JAR = f"ve-commands-{VERSION}.jar"
OLD_JAR = "ve-commands-9.9.8.jar"
GOOD = b"PK-the-released-bytes"
BAD = b"PK-some-other-bytes!!"  # the same size as GOOD, as a same-size rebuild would be
OLD = b"PK-an-older-release"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()

STUB_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
if [ "$1" = run ]; then
  if [ -n "${STUB_JAR:-}" ]; then mkdir -p "$(dirname "$STUB_OUT")"; cp "$STUB_JAR" "$STUB_OUT"; fi
  if [ -n "${STUB_PLANT:-}" ]; then mkdir -p "$(dirname "$STUB_PLANT")"; printf 'planted' > "$STUB_PLANT"; fi
  if [ -n "${STUB_SWAP_EXTRAS:-}" ]; then mkdir -p "$STUB_SWAP_EXTRAS"; ln -s "$STUB_SWAP_EXTRAS" "$STUB_EXTRAS"; fi
  exit "${STUB_RUN_RC:-0}"
fi
echo "stub docker: unexpected $*" >&2; exit 99
"""
STUB_ID = r"""#!/usr/bin/env bash
case "$1" in -u) echo "${STUB_UID:-1000}" ;; -g) echo 1000 ;; *) /usr/bin/id "$@" ;; esac
"""


def _umask_002():
    os.umask(0o002)  # Ubuntu's usergroups default for a desktop user: a plain mkdir gives 775


class Kit:
    def __init__(self, tmp, script_text=None):
        self.tmp = tmp
        self.repo = tmp / "repo"
        mod = self.repo / "mod"
        (mod / "ve-commands").mkdir(parents=True)
        (mod / "build.sh").write_text(script_text if script_text is not None else SCRIPT.read_text())
        (mod / "ve-commands" / "gradle.properties").write_text(f"org.gradle.jvmargs=-Xmx1G\nversion={VERSION}\n")
        self.pins(f"{GOOD_SHA}  {JAR}")
        self.mods(JAR)
        self.built = mod / "ve-commands" / "build" / "libs" / JAR
        self.extras = self.repo / "extras"
        self.target = self.extras / JAR
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, body in (("docker", STUB_DOCKER), ("id", STUB_ID)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.jar_src = tmp / "jar"
        self.jar_src.write_bytes(GOOD)
        self.gradle_home = tmp / "home" / ".cache" / "ve-commands-gradle"
        self.log = tmp / "docker.log"
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(tmp / "home"),
                    "STUB_LOG": str(self.log), "STUB_OUT": str(self.built), "STUB_JAR": str(self.jar_src),
                    "STUB_EXTRAS": str(self.extras)}

    def pins(self, *lines):
        (self.repo / "mod" / "RELEASE.sha256").write_text("".join(f"{x}\n" for x in lines))

    def mods(self, name):
        (self.repo / "docker-compose.yml").write_text(
            f'services:\n  minecraft:\n    environment:\n      MODS: "/extras/{name}"\n')

    def built_ran(self):
        return self.log.exists() and any(line.startswith("run ") for line in self.log.read_text().splitlines())

    def run(self, *args, **env):
        return subprocess.run(["bash", str(self.repo / "mod" / "build.sh"), *args], cwd="/",
                              env={**self.env, **env}, capture_output=True, text=True, timeout=60,
                              preexec_fn=_umask_002)


@pytest.fixture
def kit(tmp_path):
    return Kit(tmp_path)


def test_happy_path_installs_644_in_a_755_extras_then_is_unchanged_without_a_rebuild(kit):
    r = kit.run()
    assert r.returncode == 0, r.stderr
    assert kit.target.read_bytes() == GOOD and oct(kit.target.stat().st_mode & 0o777) == "0o644"
    assert oct(kit.extras.stat().st_mode & 0o777) == "0o755"  # check.sh wants 755; umask 002 alone gives 775
    assert "installed" in r.stdout and "PASS built" in r.stdout
    log = kit.log.read_text()
    assert f"-v {kit.repo / 'mod'}:/src -w /src/ve-commands" in log  # the whole mod/: the tests read contract-examples
    assert "eclipse-temurin:25-jdk ./gradlew --no-daemon clean build" in log
    assert kit.gradle_home.is_dir()  # made before docker, which would make it root-owned
    assert [p.name for p in kit.extras.iterdir()] == [JAR]  # no temp file left behind
    kit.log.unlink()
    r2 = kit.run()
    assert r2.returncode == 0 and f"unchanged {kit.target}" in r2.stdout
    assert not kit.built_ran()  # an installed, matching jar is not rebuilt


def test_older_jars_in_extras_stay_byte_for_byte(kit):
    kit.extras.mkdir()
    (kit.extras / OLD_JAR).write_bytes(OLD)
    r = kit.run()
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in kit.extras.iterdir()) == sorted([OLD_JAR, JAR])
    assert (kit.extras / OLD_JAR).read_bytes() == OLD


def test_check_builds_nothing_and_writes_nothing(kit):
    r = kit.run("--check")
    assert r.returncode == 0, r.stderr
    assert "PASS preflight" in r.stdout and "is not there yet" in r.stdout
    assert not kit.built_ran() and not kit.extras.exists() and not kit.gradle_home.exists()


def test_check_passes_an_installed_matching_jar(kit):
    kit.extras.mkdir()
    kit.target.write_bytes(GOOD)
    r = kit.run("--check")
    assert r.returncode == 0 and "PASS" in r.stdout and not kit.built_ran()


def test_an_unknown_argument_is_refused(kit):
    r = kit.run("--force")
    assert r.returncode == 1 and "unknown argument" in r.stderr and not kit.built_ran()


def test_a_failed_docker_build_installs_nothing(kit):
    r = kit.run(STUB_RUN_RC="3")
    assert r.returncode == 1 and "Gradle build failed" in r.stderr and not kit.extras.exists()


def test_the_real_repo_passes_the_preflight():
    """gradle.properties, mod/RELEASE.sha256 and docker-compose.yml MODS agree in this checkout. docker is a stub
    that fails, so --check provably calls no build."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        stub = Path(d) / "docker"
        stub.write_text("#!/bin/sh\necho 'docker must not run in --check' >&2\nexit 99\n")
        stub.chmod(0o755)
        r = subprocess.run(["bash", str(SCRIPT), "--check"], env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}"},
                           capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "PASS" in r.stdout


# ---- guards ---------------------------------------------------------------------------------------------------
# name -> (setup(kit) -> env, args, the refusal text, guard text in build.sh, its mutant, went_wrong(kit, result))

def _two_pins(kit):
    kit.pins(f"{GOOD_SHA}  {JAR}", f"{'0' * 64}  {JAR}")


def _stale(kit):
    kit.built.parent.mkdir(parents=True)
    kit.built.write_bytes(GOOD)  # a matching jar from an earlier build
    return {"STUB_JAR": ""}  # this build writes nothing


def _symlinked_extras(kit):
    (kit.tmp / "elsewhere").mkdir()
    kit.extras.symlink_to(kit.tmp / "elsewhere")


def _extras_is_a_file(kit):
    kit.extras.write_bytes(b"not a dir")


def _symlinked_target(kit):
    (kit.tmp / "outside.jar").write_bytes(GOOD)
    kit.extras.mkdir()
    kit.target.symlink_to(kit.tmp / "outside.jar")


def _other_bytes(kit):
    kit.extras.mkdir()
    kit.target.write_bytes(BAD)


def _planted_during_build(kit):
    return {"STUB_PLANT": str(kit.target)}  # the name appears while the (minutes-long) build runs


def _linked_during_build(kit):
    return {"STUB_SWAP_EXTRAS": str(kit.tmp / "elsewhere")}  # extras/ becomes a link while the build runs


def _write(path, data):
    path.write_bytes(data)
    return path


def installed(kit):
    return kit.target.is_file() and not kit.target.is_symlink()


GUARDS = {
    "root": (lambda k: {"STUB_UID": "0"}, (), "not root",
             '[ "$(id -u)" -ne 0 ] || die', 'true || die',
             lambda k, r: r.returncode == 0 and installed(k)),
    "pin_count": (_two_pins, (), "has 2 pin(s)",
                  '[ "${#pin_lines[@]}" -eq 1 ] \\', 'true \\',
                  lambda k, r: r.returncode == 0 and installed(k)),
    # no pin at all: the same guard as pin_count (whose mutant is caught); without it, set -u still stops the run
    "no_pin": (lambda k: k.pins(f"{GOOD_SHA}  ve-commands-1.0.0.jar"), (), "has 0 pin(s)", None, None, None),
    # the only line names a jar that merely CONTAINS the jar's name: it is not a pin for it
    "pin_name_exact_suffix": (lambda k: k.pins(f"{GOOD_SHA}  {JAR}.bak"), (), "has 0 pin(s)",
                              'NF == 2 && $2 == j', 'NF == 2 && index($2, j)',
                              lambda k, r: r.returncode == 0 and installed(k)),
    "pin_name_exact_prefix": (lambda k: k.pins(f"{GOOD_SHA}  x{JAR}"), (), "has 0 pin(s)",
                              'NF == 2 && $2 == j', 'NF == 2 && index($2, j)',
                              lambda k, r: r.returncode == 0 and installed(k)),
    "pin_shape": (lambda k: k.pins(f"{GOOD_SHA.upper()}  {JAR}"), (), "is not a lowercase sha256",
                  '[[ "$want" =~ ^[0-9a-f]{64}$ ]] || die', 'true || die',
                  lambda k, r: k.built_ran()),  # a bad pin must refuse before the build, not after it
    "two_versions": (lambda k: (k.repo / "mod" / "ve-commands" / "gradle.properties").write_text(
                         f"version={VERSION}\nversion=9.9.10\n") and None, (), "no single usable version=",
                     '[[ "$version" =~ ^[0-9A-Za-z.+-]+$ ]] || die', 'true || die',
                     lambda k, r: "has 0 pin(s)" in r.stderr or k.built_ran()),  # it no longer says why
    "mods_mismatch": (lambda k: k.mods("ve-commands-1.0.4.jar"), (), "not ve-commands-9.9.9.jar",
                      '[ "$mods" = "$jar" ] || die', 'true || die',
                      lambda k, r: r.returncode == 0 and installed(k)),
    "symlinked_extras": (_symlinked_extras, (), "is a symlink",
                         '[ ! -L "$extras" ] || die "${extras} is a symlink"\n[ ! -e',
                         'true || die "${extras} is a symlink"\n[ ! -e',
                         lambda k, r: k.built_ran()),  # the later re-check still refuses, but only after the build
    "extras_linked_during_build": (_linked_during_build, (), "is a symlink",
                                   '[ ! -L "$extras" ] || die "${extras} is a symlink"\ntmp=',
                                   'true || die "${extras} is a symlink"\ntmp=',
                                   lambda k, r: (k.tmp / "elsewhere" / JAR).exists()),
    "extras_not_dir": (_extras_is_a_file, (), "is not a directory",
                       '[ ! -e "$extras" ] || [ -d "$extras" ] || die', 'true || die',
                       lambda k, r: k.built_ran()),
    "symlinked_target": (_symlinked_target, (), "is not a regular file",
                         'if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then', 'if false; then',
                         lambda k, r: r.returncode == 0),  # the link's target was taken as the installed jar
    "other_bytes": (_other_bytes, (), "exists with other bytes",
                    '[ "$have" = "$want" ] \\\n    || die "${target} exists', 'true \\\n    || die "${target} exists',
                    lambda k, r: r.returncode == 0 and k.target.read_bytes() == BAD),
    "stale_jar": (_stale, (), "left no",
                  'rm -f -- "$built"  # a build that writes nothing', ':  # a build that writes nothing',
                  lambda k, r: r.returncode == 0 and installed(k)),
    "sha_mismatch": (lambda k: {"STUB_JAR": str(_write(k.tmp / "bad.jar", BAD))}, (),
                     "differs from mod/RELEASE.sha256",
                     '[ "$have" = "$want" ] || die "built', 'true || die "built',
                     # the post-install verify still fails the run, but the wrong jar is already in extras/
                     lambda k, r: installed(k) and k.target.read_bytes() == BAD),
    "no_clobber": (_planted_during_build, (), "appeared during the build",
                   'ln -- "$tmp" "$target" ||', 'ln -f -- "$tmp" "$target" ||',
                   lambda k, r: k.target.read_bytes() == GOOD),  # the planted file was replaced
    "check_builds_nothing": (lambda k: None, ("--check",), None,
                             'if [ "$CHECK" = 1 ]; then', 'if false; then',
                             lambda k, r: k.built_ran()),
    "older_jars_kept": (lambda k: _write(_mkdir(k.extras) / OLD_JAR, OLD) and None, (), None,
                        'install -d -m 755 "$extras"\n',
                        'install -d -m 755 "$extras"\nrm -f -- "$extras"/ve-commands-*.jar\n',
                        lambda k, r: not (k.extras / OLD_JAR).exists()),
    "extras_mode": (lambda k: None, (), None,
                    'install -d -m 755 "$extras"', 'mkdir -p "$extras"',
                    lambda k, r: oct(k.extras.stat().st_mode & 0o777) != "0o755"),
}


def _mkdir(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


REFUSALS = sorted(n for n in GUARDS if GUARDS[n][2])


@pytest.mark.parametrize("name", REFUSALS)
def test_guard_refuses(kit, name):
    setup, args, why, *_ = GUARDS[name]
    env = setup(kit) or {}
    before = kit.target.read_bytes() if kit.target.is_file() else None
    r = kit.run(*args, **env)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert why in r.stderr
    if name == "no_clobber":
        assert kit.target.read_bytes() == b"planted"  # left exactly as it appeared
        assert [p.name for p in kit.extras.iterdir()] == [JAR]  # and the temp copy is gone
    elif before is None:
        assert not kit.target.exists() or kit.target.is_symlink()
    else:
        assert kit.target.read_bytes() == before  # an existing jar is never overwritten
    if name not in ("stale_jar", "sha_mismatch", "no_clobber", "extras_linked_during_build"):
        assert not kit.built_ran()  # refused before the (minutes-long) build


@pytest.mark.parametrize("name", sorted(n for n in GUARDS if GUARDS[n][3]))
def test_each_guard_catches_its_mutant(tmp_path, name):
    setup, args, _, guard, mutant, went_wrong = GUARDS[name]
    text = SCRIPT.read_text()
    assert text.count(guard) == 1, f"stale or ambiguous mutation pattern for {name}"
    kit = Kit(tmp_path, text.replace(guard, mutant))
    r = kit.run(*args, **(setup(kit) or {}))
    assert went_wrong(kit, r), (name, r.returncode, r.stdout, r.stderr)
