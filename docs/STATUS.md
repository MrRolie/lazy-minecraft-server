# Proven so far, and not yet

Moved unchanged from the README. "The virtual machine above" is the run described under "How long it took, once" in the [README](../README.md#how-long-it-took-once).

**The original server.** It runs on an old ASUS laptop (i5-7200U, 2 cores / 4 threads, 15 GiB RAM) for a group of
friends, open since 2026-10-02. It runs the version the kit was generalized from, not the kit itself.

**The kit.** Its fresh setup is proven once, on the virtual machine above: every phase ran, Discord-to-game chat
and `/whitelist` worked, and the test restart came back on its own. What that run found is fixed (in code with tests, or in
the docs), except one open finding (below). Two later runs on virtual machines, both without Discord, ran on the
fixed kit; the Discord and Tailscale fixes have not been run yet.

**Not yet:**
- No kit run on a real old laptop, so the Wi-Fi fallback, a fixed address, the battery charge cap and the BIOS
  power-on setting are untested.
- The first run stopped world pre-generation early.
- The custom-pack path: a friend ran it on 2026-10-20 and reported it went smoothly; its run record is pending. The
  defect it found, `#welcome` showing Vanilla Extended's text, is fixed in this snapshot; no run has gone through
  that fix yet.
- The no-Discord path ran once, on a throwaway virtual machine (2026-11-13), not on a laptop: Tailscale was untested,
  world pre-generation was cut short, and every person's step was done by a stand-in, so nobody joined the game.
  Its 12 steps are counted from the step list, not measured. What it found: [docs/kit/2026-11-13_no-discord-run-findings.md](kit/2026-11-13_no-discord-run-findings.md).
- The headless setup over SSH was rehearsed once, on a virtual machine (2026-11-14 to 15), with no Discord: key-only
  SSH after setup, and the reconnect after a restart worked. Not yet run that way: Claude Code on the laptop itself (it
  was not run in the rehearsal), the Windows connection line, a real home-network address, Tailscale, world pre-generation, Discord, or a real person at the
  chair. The record: [docs/kit/2026-11-14_headless-rehearsal.md](kit/2026-11-14_headless-rehearsal.md).
- Game-to-Discord chat was not checked by eye in the first run (Discord-to-game was).
- Two open findings. The Nether and End borders reverted once after a cancel and a restart; a gate caught it, and
  it has not been reproduced. And after the no-Discord run's reboot, a health check failed on Tailscale's
  auto-update setting (in that run's findings).
