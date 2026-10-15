package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

class CooldownsTest {
	@Test
	void tenSecondsPerKey() {
		AtomicLong now = new AtomicLong(0);
		Cooldowns c = new Cooldowns(10_000, now::get);
		assertEquals(0, c.tryAcquire("discovery:a"));
		now.set(3_000_000_000L);
		assertEquals(7, c.tryAcquire("discovery:a"));
		assertEquals(0, c.tryAcquire("coords:a"), "separate per command");
		assertEquals(0, c.tryAcquire("discovery:b"), "separate per player");
		now.set(9_999_000_000L);
		assertEquals(1, c.tryAcquire("discovery:a"));
		now.set(10_000_000_000L);
		assertEquals(0, c.tryAcquire("discovery:a"));
	}
}
