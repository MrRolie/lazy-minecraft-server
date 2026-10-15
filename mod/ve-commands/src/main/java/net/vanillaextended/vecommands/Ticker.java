package net.vanillaextended.vecommands;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * One periodic job on its own daemon thread. The job may throw anything, an Error included: it is caught and logged
 * (throttled), and the schedule goes on. A throw out of a ScheduledExecutorService task would cancel it for good,
 * silently. One Ticker per job, so a slow job (a Mojang lookup) can never delay another (the status beat).
 */
final class Ticker {
	private static final Logger LOG = LoggerFactory.getLogger("ve-commands");
	private final ScheduledExecutorService exec;
	private final LogThrottle failures = new LogThrottle(60_000, System::nanoTime);
	private final String name;

	/** {@code fixedRate}: every period from the start (the beat); else period after each run ends (the poll). */
	Ticker(String name, long initialDelay, long period, TimeUnit unit, boolean fixedRate, Runnable job) {
		this.name = name;
		this.exec = Executors.newSingleThreadScheduledExecutor(r -> {
			Thread t = new Thread(r, name);
			t.setDaemon(true);
			return t;
		});
		Runnable guarded = () -> {
			try {
				job.run();
			} catch (Throwable t) {
				report(t);
			}
		};
		if (fixedRate) exec.scheduleAtFixedRate(guarded, initialDelay, period, unit);
		else exec.scheduleWithFixedDelay(guarded, initialDelay, period, unit);
	}

	private void report(Throwable t) {
		try {
			long suppressed = failures.next();
			if (suppressed >= 0) LOG.error("{} failed ({} more since the last report); it keeps running", name, suppressed, t);
		} catch (Throwable ignored) {
			// logging itself can fail (out of memory); the schedule must survive that too
		}
	}

	void stop() {
		exec.shutdownNow();
	}
}
