# The headless rehearsal (2026-11-14 to 15): steps 24–25, then P0 to P12 over SSH

Run `20261114T173035Z` was a rehearsal of the headless setup (HUMAN-STEPS "from your other computer"), on the kit as
of private commit `4b0c0c6`, in a public-shaped copy. The kit's code did not change before the run ended; only its
docs did. It ran on a 16 GB, 4-vCPU VM on the maintainer's machine,
installed unattended from the official Ubuntu Desktop 24.04.5.1 ISO (signature and checksum verified). QEMU user
networking forwarded the VM's SSH port to the host's loopback. The maintainer stood in for the person, and the host,
a Linux machine, stood in for the person's own computer. There was no Discord. On 2026-11-14 the run stopped at P4,
because nobody had accepted the Minecraft EULA for this server yet. After the maintainer accepted the EULA for this test server on 2026-11-15,
it went on from P4 to P12. It was sealed as complete with accepted failures. Its human-step count is only a floor,
because the Tailscale steps (17, 18 and 21) were never done.

## What ran
- **Step 24 at the VM's own screen.** The maintainer typed it through the QEMU monitor: logged in at the login
  screen, opened a terminal with Ctrl+Alt+T, and typed the step's command.
  - openssh-server came up socket-activated, tmux was installed, and sleep was masked.
  - It printed the user name and the addresses. The address was `10.0.2.15`, which fits the "10.x.x.x" wording. The
    connection itself went to the loopback port forward, not to that address.
- **Step 25 from a Linux computer:** `ssh-keygen`, `ssh-copy-id` (the password once), and then a connection with no
  password. `~/.ssh` came out 700 and `authorized_keys` 600. The Windows line was not run.
- **The key check before P7, tested both ways:** **0** after a password-only login, **1** after the key login. As a
  positive control, the same `journalctl -u ssh.service` query read the 2 password logins, so the 0 was not an
  unreadable log.
- **Steps 5 and 7 over SSH:** the curl, git and Claude Code install exited 0. `sudoers.sh install` ran from a second
  SSH session (visudo OK), after which `sudo -n` worked.
- **The phases:**
  - P0 passed (no RAM override was needed). P1, P6 and P11 were skipped (no Discord).
  - P2 passed.
  - P3's gate failed, since no tailnet credential may enter a test VM, and the failure was accepted as not tested.
  - P4 stopped on 2026-11-14 at `kitconfig`'s EULA refusal, as designed. It was re-run after the yes, and passed.
  - P5 passed its gates: the mod check, the pack file, and the pack's server list holding the address from
    `kit.toml`, a placeholder here (no Tailscale). The build's own log printed that ve-commands matched its release
    hash; the record keeps only the gates.
  - P7, the SSH branch: the key check read 25 key logins before `setup.sh`. Afterwards `sshd` had password logins
    off, a password-only login from the host was refused, and the key still worked. The second `setup.sh` changed
    nothing. `check.sh` (without `--no-ssh`) showed no SSH failure. Its only failures were the network watchdog's,
    because Tailscale was never logged in (P3); they were accepted.
  - P8: the first start reached `Done` in 21 s, and the owner was op.
  - P9: two backups, `world.sh init`, and pausing set to 60 s with a fresh backup and a restart. **The rings were not
    run**: they take hours of CPU, and the 2026-11-13 run already exercised them
    (`docs/kit/2026-11-13_no-discord-run-findings.md`). So the border and ring gates failed
    as expected, and the failure was accepted.
  - P10: a backup and `--stand-up`, then `sudo systemctl reboot` over SSH. The key login worked again 25 s later,
    and the server came back by itself (`Done`, the player list answered, the backup timer listed). `check.sh` failed
    only on Tailscale and the watchdog, as in P7, and that was accepted.
  - P12 (no Discord): the owner was whitelisted, and `kit/friends.py` wrote the friend message, with a placeholder
    share link because there was no Tailscale. Both of P12's headless `scp` lines, run from the Linux side, copied
    the message and the `.mrpack`, and each copy's hash matched the original. Nobody read the message.

## Findings
1. **Ubuntu offered an upgrade to 26.04.1 LTS** in a dialog during the first session. A person who clicks yes moves to
   a release the kit has not tested. **Fixed:** step 24 now says to click Don't Upgrade. That wording has not been
   tried live.
2. **`server.open_date` written in quotes is refused.** KIT.md P12 said only "set it to today". Written as
   `"2026-11-15"`, render refused it ("must be date, not str"). Unquoted it passed. **Fixed:** KIT.md P12 now shows the
   unquoted form.

## Not exercised
- Claude Code itself on the VM. No Claude login may enter a test VM, so the maintainer drove over SSH, and
  `claude --continue` was not run. Some of KIT.md's multi-line gate scripts were copied into the VM as files, because
  of SSH quoting. Each one ran the same code.
- An interactive tmux reattach. A `kit` session made in one SSH connection was still there from the next one, and
  `tmux new -A -s kit` ran after the reboot, but both were run without a terminal.
- The Windows `ssh-copy-id` replacement, and a connection over a real home-network address.
- Tailscale (P3) and everything that needs it: the watchdog's health, the real share link, and a friend joining.
- The pre-generation rings (P9), Discord, and a real person at the chair.
