package net.vanillaextended.vecommands;

import java.util.function.LongSupplier;

/** At most one log line per window; the occurrences in between are counted and reported with the next one. */
final class LogThrottle {
	private final long windowNanos;
	private final LongSupplier clock;
	private long lastLogged;
	private boolean logged;
	private long suppressed;

	LogThrottle(long windowMillis, LongSupplier nanoClock) {
		this.windowNanos = windowMillis * 1_000_000L;
		this.clock = nanoClock;
	}

	/** -1 to stay quiet, else how many occurrences were suppressed since the last line (log now). */
	synchronized long next() {
		long now = clock.getAsLong();
		if (logged && now - lastLogged < windowNanos) {
			suppressed++;
			return -1;
		}
		long s = suppressed;
		suppressed = 0;
		logged = true;
		lastLogged = now;
		return s;
	}
}
