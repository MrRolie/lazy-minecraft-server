package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/** The status beat and the inbox poll run on Tickers: separate threads, and no throw (not even an Error) stops one. */
class TickerTest {
	@Test
	void aThrowingJobKeepsRunningEvenAfterAnError() throws Exception {
		AtomicInteger runs = new AtomicInteger();
		CountDownLatch five = new CountDownLatch(5);
		Ticker t = new Ticker("t-throws", 0, 10, TimeUnit.MILLISECONDS, false, () -> {
			int n = runs.incrementAndGet();
			five.countDown();
			if (n == 1) throw new OutOfMemoryError("simulated");
			if (n == 2) throw new IllegalStateException("simulated");
			if (n == 3) throw new StackOverflowError("simulated");
		});
		try {
			assertTrue(five.await(5, TimeUnit.SECONDS), "runs after the throws: " + runs.get());
		} finally {
			t.stop();
		}
	}

	@Test
	void aBlockedJobNeverDelaysAnotherTicker() throws Exception {
		CountDownLatch release = new CountDownLatch(1);
		Set<String> threads = ConcurrentHashMap.newKeySet();
		Ticker inbox = new Ticker("t-inbox", 0, 10, TimeUnit.MILLISECONDS, false, () -> {
			threads.add(Thread.currentThread().getName());
			try {
				release.await(); // a Mojang lookup that never returns
			} catch (InterruptedException e) {
				Thread.currentThread().interrupt();
			}
		});
		CountDownLatch beats = new CountDownLatch(3);
		Ticker status = new Ticker("t-status", 0, 20, TimeUnit.MILLISECONDS, true, () -> {
			threads.add(Thread.currentThread().getName());
			beats.countDown();
		});
		try {
			assertTrue(beats.await(5, TimeUnit.SECONDS), "the beat kept going while the inbox job was stuck");
			assertEquals(Set.of("t-inbox", "t-status"), threads, "one thread per Ticker");
		} finally {
			release.countDown();
			inbox.stop();
			status.stop();
		}
	}
}
