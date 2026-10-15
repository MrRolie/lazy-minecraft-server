package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonElement;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * rv3 M1: a hostile inbox cannot amplify itself into the server log. Skipped lines, repeated shrinks, refused
 * whitelist_adds, backlog skips, dropped says, read failures and a path flipping between a link and a file are
 * reported throttled, and everything bridge-supplied in the log is escaped (pinned here, at the
 * Inbox call sites, not only in Text.forLog).
 */
class LogFloodTest {
	@TempDir
	Path root;
	Path in;
	Path out;
	final AtomicLong now = new AtomicLong(1_000_000_000L);
	final List<String> calls = new ArrayList<>();
	final Inbox.Handler handler = new Inbox.Handler() {
		@Override
		public void say(JsonElement id, String author, String text) {
			calls.add("say " + id);
		}

		@Override
		public void whitelistAdd(JsonElement id, String name, String requestedBy) {
			calls.add("wl " + id);
		}

		@Override
		public void whitelistRateLimited(JsonElement id, String name) {
			calls.add("limited " + id);
		}
	};

	@BeforeEach
	void dirs() throws Exception {
		in = Files.createDirectory(root.resolve("in"));
		out = Files.createDirectory(root.resolve("out"));
	}

	Path inbox() {
		return in.resolve("inbox.jsonl");
	}

	void append(String s) throws Exception {
		Files.writeString(inbox(), s, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND);
	}

	/** The reviewer's line: 326 control bytes (the 200-line, 65400-byte budget per poll). */
	static String controlLines(int n) {
		return ("\u0001".repeat(326) + "\n").repeat(n);
	}

	@Test
	void skippedLinesAreReportedOncePerMinuteNotPerLine() throws Exception {
		try (LogCapture log = new LogCapture()) {
			Inbox inbox = new Inbox(in, out, handler, now::get);
			for (int i = 0; i < 5; i++) { // the reviewer's logflood: 5 x 65400 B, a poll after each
				append(controlLines(200));
				inbox.poll();
			}
			List<String> skips = log.matching("malformed");
			assertEquals(1, skips.size(), "one report within the minute, not 1000 lines: " + log.lines);
			assertTrue(skips.get(0).contains("skipped 200 "), skips.get(0));
			assertTrue(log.bytes() < 4096, "log bytes for 327,000 inbox bytes: " + log.bytes());
			now.addAndGet(60_000_000_000L);
			inbox.poll();
			assertEquals(2, log.matching("malformed").size());
			assertTrue(log.matching("malformed").get(1).contains("skipped 800 "), log.lines.toString());
		}
	}

	/** n whitelist_add lines with an invalid name (dispatched, but they spend no token and read no clock). */
	static String adds(int round, int n) {
		StringBuilder sb = new StringBuilder();
		for (int i = 0; i < n; i++) sb.append("{\"type\":\"whitelist_add\",\"id\":\"r").append(round).append("-").append(i).append("\",\"name\":\"bad name!\"}\n");
		return sb.toString();
	}

	@Test
	void shrinkAndRewriteResetsToZeroOncePerMinuteThenSkipsToTheEnd() throws Exception {
		try (LogCapture log = new LogCapture()) {
			Inbox inbox = new Inbox(in, out, handler, now::get); // the clock stands still: one reset token, no refill
			append(adds(9, 3));
			inbox.poll();
			for (int r = 0; r < 10; r++) { // the reviewer's replay: rewrite in place, alternating smaller and larger
				Files.writeString(inbox(), adds(r, r % 2 == 0 ? 2 : 3), StandardCharsets.UTF_8,
						StandardOpenOption.TRUNCATE_EXISTING);
				inbox.poll();
			}
			// Unlimited, every shrink re-reads from 0: 3 + 5 x (2 + 1) = 18 dispatches. Limited, only the first does:
			// 3 + 2 + 1 for rounds 0 and 1, then each later shrink skips to the end and only the growth is read (1 each).
			assertEquals(3 + 2 + 1 + 4, calls.size(), calls.toString());
			assertEquals(1, log.matching("shrank").size(), "shrink reports are throttled: " + log.matching("shrank"));
			assertTrue(log.bytes() < 4096, "log bytes after 10 rewrites: " + log.bytes());
		}
	}

	@Test
	void skipReportsCarryNoRawControlsAtTheCallSite() throws Exception {
		try (LogCapture log = new LogCapture()) {
			Inbox inbox = new Inbox(in, out, handler, now::get);
			append("{\"type\":\"x\u001b[2K\r\n\"}\n".replace("\r\n", "\\r\\n") + "garbage \u001b[31m \u007f \r [00:00:00] fake\n");
			inbox.poll();
			List<String> skips = log.matching("malformed");
			assertEquals(1, skips.size(), log.lines.toString());
			String line = skips.get(0);
			assertTrue(line.chars().noneMatch(c -> c < 0x20 || c == 0x7f || (c >= 0x80 && c <= 0x9f)), line);
			assertTrue(line.contains("\\u001b"), line);
		}
	}

	@Test
	void refusedWhitelistAddsAreReportedThrottled() throws Exception {
		try (LogCapture log = new LogCapture()) {
			Inbox inbox = new Inbox(in, out, handler, now::get);
			StringBuilder sb = new StringBuilder();
			for (int i = 0; i < 60; i++) sb.append("{\"type\":\"whitelist_add\",\"id\":").append(i).append(",\"name\":\"Player").append(i).append("\"}\n");
			append(sb.toString());
			for (int p = 0; p < 5; p++) { // 60 lines per poll, five polls within the minute: 290 refusals
				inbox.poll();
				if (p < 4) append(sb.toString().replace("\"id\":", "\"id\":" + (p + 1) + "0"));
			}
			assertEquals(290, calls.stream().filter(c -> c.startsWith("limited")).count());
			assertEquals(1, log.matching("over the 10/hour cap").size(), "one report a minute: " + log.lines);
			now.addAndGet(60_000_000_000L);
			inbox.poll();
			assertEquals(2, log.matching("over the 10/hour cap").size());
			assertTrue(log.matching("over the 10/hour cap").get(1).contains("refused 240 "), log.lines.toString());
		}
	}

	@Test
	void aPathFlippingBetweenLinkAndFileIsReportedThrottled() throws Exception {
		Path target = Files.writeString(root.resolve("world.dat"), "world bytes\n");
		try (LogCapture log = new LogCapture()) {
			Inbox inbox = new Inbox(in, out, handler, now::get);
			for (int i = 0; i < 50; i++) { // the bridge flips in/inbox.jsonl every poll
				Files.deleteIfExists(inbox());
				if (i % 2 == 0) Files.createSymbolicLink(inbox(), target);
				else append("");
				inbox.poll();
			}
			assertEquals(1, log.matching("refusing " + inbox()).size(), "25 refusals, one report: " + log.lines);
			assertEquals(1, log.matching(inbox() + " is usable again").size(), log.lines.toString());
			assertTrue(log.bytes() < 4096, "log bytes after 50 flips: " + log.bytes());
		}
	}

	/** Polls {@code rounds} times within the minute (after {@code each}), then once more a minute later. */
	List<String> twoWindows(Inbox inbox, String needle, int rounds, Runnable each) throws Exception {
		try (LogCapture log = new LogCapture()) {
			for (int r = 0; r < rounds; r++) {
				each.run();
				inbox.poll();
			}
			assertEquals(1, log.matching(needle).size(), rounds + " occurrences, one report a minute: " + log.lines);
			now.addAndGet(60_000_000_000L);
			each.run();
			inbox.poll();
			assertEquals(2, log.matching(needle).size(), "the next minute reports again: " + log.lines);
			return log.matching(needle);
		}
	}

	void unchecked(java.util.concurrent.Callable<?> c) {
		try {
			c.call();
		} catch (Exception e) {
			throw new RuntimeException(e);
		}
	}

	@Test
	void backlogSkipsAreReportedThrottled() throws Exception {
		Inbox inbox = new Inbox(in, out, handler, now::get);
		List<String> lines = twoWindows(inbox, "is over 1048576", 5,
				() -> unchecked(() -> { append(controlLines(3300)); return null; })); // 1.08 MB each: over the backlog cap
		assertTrue(lines.get(1).contains("; 4 more skips since the last report"), lines.toString());
		assertTrue(calls.isEmpty(), "a skipped backlog is never acted on: " + calls);
	}

	@Test
	void droppedSaysAreReportedThrottled() throws Exception {
		Inbox inbox = new Inbox(in, out, handler, now::get); // the clock stands still within the minute: 5 tokens
		StringBuilder sb = new StringBuilder();
		for (int i = 0; i < 20; i++) sb.append("{\"type\":\"say\",\"id\":").append(i).append(",\"author\":\"a\",\"text\":\"t\"}\n");
		List<String> lines = twoWindows(inbox, "over the 5/s limit", 5,
				() -> unchecked(() -> { append(sb.toString()); return null; }));
		assertTrue(lines.get(0).contains("dropped 15 "), lines.toString());
		assertTrue(lines.get(1).contains("dropped 95 "), "4 x 20 within the minute, then 15 past the refill: " + lines);
	}

	@Test
	void readFailuresAreReportedThrottled() throws Exception {
		org.junit.jupiter.api.Assumptions.assumeFalse("root".equals(System.getProperty("user.name")), "root reads mode 000");
		append("{\"type\":\"say\",\"id\":1,\"author\":\"a\",\"text\":\"t\"}\n");
		Inbox inbox = new Inbox(in, out, handler, now::get);
		append("{\"type\":\"say\",\"id\":2,\"author\":\"a\",\"text\":\"t\"}\n");
		Files.setPosixFilePermissions(inbox(), java.nio.file.attribute.PosixFilePermissions.fromString("---------"));
		List<String> lines = twoWindows(inbox, "inbox read failed", 5, () -> {});
		assertTrue(lines.get(1).contains("(4 more since the last report)"), lines.toString());
	}

	@Test
	void theAuditLineEscapesRequestedBy() {
		String line = InboxActions.whitelistedLine("Notch", "069a79f4", "Mike\r\n[00:00:00] Made Mike a server operator\u001b\u007f");
		assertTrue(line.chars().noneMatch(c -> c < 0x20 || c == 0x7f), line);
		assertTrue(line.contains("\\u000d\\u000a") && line.contains("\\u007f"), line);
	}
}
