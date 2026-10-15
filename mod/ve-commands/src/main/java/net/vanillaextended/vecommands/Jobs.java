package net.vanillaextended.vecommands;

import java.util.concurrent.TimeUnit;

/**
 * The mod's two periodic jobs, each on its own Ticker. The status beat must never wait behind the inbox poll, whose
 * Mojang lookup or 10 s whitelist check can stall; JobsTest pins that a blocked poll does not delay the beat.
 */
final class Jobs {
	final Ticker inbox;
	final Ticker status;

	private Jobs(Ticker inbox, Ticker status) {
		this.inbox = inbox;
		this.status = status;
	}

	/** {@code poll} every {@code pollMillis} after each run ends; {@code beat} at a fixed rate, the first at once. */
	static Jobs start(Runnable poll, long pollMillis, Runnable beat, long beatMillis) {
		Ticker inbox = new Ticker("ve-commands-inbox", pollMillis, pollMillis, TimeUnit.MILLISECONDS, false, poll);
		Ticker status = new Ticker("ve-commands-status", 0, beatMillis, TimeUnit.MILLISECONDS, true, beat);
		return new Jobs(inbox, status);
	}

	void stop() {
		inbox.stop();
		status.stop();
	}
}
