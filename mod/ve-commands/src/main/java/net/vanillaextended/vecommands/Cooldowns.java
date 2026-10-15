package net.vanillaextended.vecommands;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.LongSupplier;

/** Per-key cooldown. A key is "<command>:<who>". */
final class Cooldowns {
	private final long cooldownNanos;
	private final LongSupplier clock;
	private final Map<String, Long> last = new ConcurrentHashMap<>();

	Cooldowns(long cooldownMillis, LongSupplier nanoClock) {
		this.cooldownNanos = cooldownMillis * 1_000_000L;
		this.clock = nanoClock;
	}

	/** 0 if {@code key} may run now (and starts its cooldown), else the whole seconds left (at least 1). */
	long tryAcquire(String key) {
		long now = clock.getAsLong();
		Long prev = last.get(key);
		if (prev != null && now - prev < cooldownNanos) {
			return Math.max(1, (cooldownNanos - (now - prev) + 999_999_999L) / 1_000_000_000L);
		}
		last.put(key, now);
		return 0;
	}
}
