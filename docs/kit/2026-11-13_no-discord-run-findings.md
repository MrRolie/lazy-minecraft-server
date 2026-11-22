# The no-Discord run: findings (2026-11-13)

The first live run of KIT.md's "With no Discord" path. It ran on run `20261113T011533Z`, on a throwaway Ubuntu Desktop
24.04.5 VM (Hyper-V, 11 GB RAM, 4 vCPU, 80 GB disk, NetworkManager on a static wired link, internet only). P0 to P12
took 16 min 37 s, with the rings cut short. Its outcome was "complete, with accepted failures" (P3, P7, P9 and P10,
each accepted with its reason; why is under the findings below), and N was INCOMPLETE (at least 12), because the
Tailscale steps did not happen. The code changed during the run: finding 1's fix went in during P7, so `setup.sh` as
it stood at the start was not run end to end.

## What it was, and what it was not

- **Proven live with no Discord:** P1, P6 and P11 are skipped and the record shows them as skipped. The person is not
  asked for steps 9–16. P7's alert gate passes on "alerts are off". `check.sh` prints INFO lines for alerts off, no
  bridge and no `.env.bridge`. At boot, `mc-bridge-up` logs "no Discord … no bridge to start" and starts nothing.
  P12 needs no sync: the owner is whitelisted by rcon, `kit/friends.py` writes the message, `friends.py --check`
  passes and `finish` reads the Discord steps as not required.
- **Also live:** the probe's `--accept-unproven-ram` path, P2 to P5, the first start (`Done` in 24 s on a 4G heap),
  backups, `world.sh init`, pausing off and on through `kit.toml` and render, a ring started and cut, `world.sh done`
  failing on a cut ring as it should, `--stand-up`, and the server coming back by itself after a reboot.
- **Not tested:** P3. The VM could hold no tailnet credential, so the share link was a shape-valid placeholder,
  `kit.toml [tailscale]` held placeholders, and P3 was ended with `--accept`. Nobody joined the game, and no person
  sat at the chair: the maintainer stood in for every human step, and each stand-in is marked in the record.
- **RAM:** 11 GB is under the proven 16 GB class. `server.memory` was 4G (about RAM − 7 GiB). Nothing here says
  whether 4G holds friends.

## Findings, and what fixed each

1. **`setup.sh` step 8 refused key-only SSH with "PubkeyAuthentication off" while `sshd -T` said `yes`.**
   `setup.sh` runs under `set -o pipefail`, and `sshd -T | grep -qx …` lets grep exit at the first match. `sshd`
   then dies of SIGPIPE (rc 141), and pipefail turns the match into a false. This was reproduced in the VM. The
   same shape sat in setup's address check, `deploy/world.sh`'s running-task guard (where a false "no task" fails
   open) and `kit/packages.sh`. **Fixed:** each became `grep … >/dev/null`, which reads to the end. A test in
   `host/test_host.py` fails on any pipe into `grep -q` in a pipefail script; reintroducing the bug fails it.
2. **The dhcp abort said "nothing changed" after it had changed the wired connection's priorities.** Only the
   address is untouched. **Fixed:** the message now says that.
3. **Stopping a ring early takes two rcon commands.** `chunky cancel` only asks for `chunky confirm`, and with
   continue-on-restart an unconfirmed ring would come back at the next start. **Fixed:** KIT.md P9 says so.
4. **Open, not fixed:** after the reboot, `check.sh` FAILed on `tailscale AutoUpdate.Apply = 'None'`, though P7 had
   set it off. The likely cause is a logged-out `tailscaled` (P3 skipped) not keeping the preference, but that is
   not proven. A host that logs in at P3 has not shown it.
5. **Artifacts of the test VM, not kit defects:** the VM's wired link was static with no DHCP (run as human step 20,
   with its own values: `unchanged`). Its gateway does not answer ping, so with no tailnet the netwatch probe reads
   UNHEALTHY, `setup.sh` holds the timer back itself and `check.sh` FAILs those two lines.
