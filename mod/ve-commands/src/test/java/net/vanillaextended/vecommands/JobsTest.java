package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

/** rv3 M5: the status beat VeCommands schedules (through Jobs) keeps beating while the inbox poll is stuck. */
class JobsTest {
	@Test
	void aStalledInboxPollNeverDelaysTheStatusBeat() throws Exception {
		CountDownLatch pollStarted = new CountDownLatch(1);
		CountDownLatch release = new CountDownLatch(1);
		CountDownLatch beats = new CountDownLatch(5);
		Jobs jobs = Jobs.start(() -> {
			pollStarted.countDown();
			try {
				release.await(); // a Mojang lookup (or the 10 s whitelist check) that does not come back
			} catch (InterruptedException e) {
				Thread.currentThread().interrupt();
			}
		}, 10, beats::countDown, 20);
		try {
			assertTrue(pollStarted.await(5, TimeUnit.SECONDS), "the poll ran");
			long t0 = System.nanoTime();
			assertTrue(beats.await(5, TimeUnit.SECONDS), "the beat kept going while the poll was stuck");
			assertTrue(System.nanoTime() - t0 < TimeUnit.SECONDS.toNanos(5));
		} finally {
			release.countDown();
			jobs.stop();
		}
	}

	/**
	 * The VeCommands call site, pinned from its source (it needs a live MinecraftServer, so it cannot run here): the
	 * poll Ticker gets exactly {@code inbox::poll}, and the only status beat is the separate beat argument. Folding
	 * the beat into the poll job fails this, and the test above proves Jobs keeps the two apart.
	 */
	@Test
	void veCommandsSchedulesTheBeatOnItsOwnJob() throws Exception {
		String src = Files.readString(Path.of("src/main/java/net/vanillaextended/vecommands/VeCommands.java"));
		assertEquals(1, count(src, "Jobs.start("), "one scheduling call site");
		assertEquals(1, count(src, "status(server, null, false)"), "one status beat");
		List<String> args = args(src, src.indexOf("Jobs.start(") + "Jobs.start(".length());
		assertEquals(4, args.size(), args.toString());
		assertEquals("inbox::poll", args.get(0), "the poll job runs the inbox poll and nothing else");
		assertEquals("() -> server.execute(() -> status(server, null, false))", args.get(2), "the beat is its own job");
		assertEquals("60_000", args.get(3));
	}

	private static int count(String s, String what) {
		int n = 0;
		for (int i = s.indexOf(what); i >= 0; i = s.indexOf(what, i + 1)) n++;
		return n;
	}

	/** The top-level arguments of the call whose '(' ends just before {@code from}. */
	private static List<String> args(String s, int from) {
		List<String> out = new ArrayList<>();
		int depth = 0;
		StringBuilder cur = new StringBuilder();
		for (int i = from; ; i++) {
			char c = s.charAt(i);
			if (depth == 0 && (c == ',' || c == ')')) {
				out.add(cur.toString().strip().replaceAll("\\s+", " "));
				cur.setLength(0);
				if (c == ')') return out;
				continue;
			}
			if (c == '(' || c == '{') depth++;
			if (c == ')' || c == '}') depth--;
			cur.append(c);
		}
	}
}
