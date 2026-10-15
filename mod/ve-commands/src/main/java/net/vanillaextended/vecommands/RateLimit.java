package net.vanillaextended.vecommands;

import java.util.function.LongSupplier;

/** Token bucket: up to {@code burst} at once, refilled at {@code perSecond}. Single-threaded use (the inbox thread). */
final class RateLimit {
	private final double burst;
	private final double perNano;
	private final LongSupplier clock;
	private double tokens;
	private long last;

	RateLimit(double burst, double perSecond, LongSupplier nanoClock) {
		this.burst = burst;
		this.perNano = perSecond / 1e9;
		this.clock = nanoClock;
		this.tokens = burst;
		this.last = nanoClock.getAsLong();
	}

	boolean tryAcquire() {
		long now = clock.getAsLong();
		tokens = Math.min(burst, tokens + (now - last) * perNano);
		last = now;
		if (tokens < 1) return false;
		tokens -= 1;
		return true;
	}
}
