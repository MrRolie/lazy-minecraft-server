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
import java.util.function.LongSupplier;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class InboxTest {
	@TempDir
	Path root;
	Path in;
	Path out;

	final List<String> calls = new ArrayList<>();
	final Inbox.Handler handler = new Inbox.Handler() {
		@Override
		public void say(JsonElement id, String author, String text) {
			calls.add("say " + id + " " + author + ": " + text);
		}

		@Override
		public void whitelistAdd(JsonElement id, String name, String requestedBy) {
			calls.add("wl " + id + " " + name + " by " + requestedBy);
		}

		@Override
		public void whitelistRateLimited(JsonElement id, String name) {
			calls.add("limited " + id + " " + name);
		}
	};
	/** Each clock read is an hour later, so the rate limits always refill; LimitsTest exercises them. */
	final AtomicLong now = new AtomicLong();
	final LongSupplier hourPerRead = () -> now.addAndGet(3_600_000_000_000L);

	@BeforeEach
	void dirs() throws Exception {
		in = Files.createDirectory(root.resolve("in"));
		out = Files.createDirectory(root.resolve("out"));
	}

	Inbox inbox() {
		return new Inbox(in, out, handler, hourPerRead);
	}

	/** Polls until the offset stops moving; returns how many polls that took. */
	int drain(Inbox inbox) throws Exception {
		int polls = 0;
		long before;
		do {
			before = inbox.offset();
			inbox.poll();
			polls++;
		} while (inbox.offset() != before);
		return polls;
	}

	void append(String s) throws Exception {
		Files.writeString(in.resolve("inbox.jsonl"), s, StandardCharsets.UTF_8, StandardOpenOption.CREATE,
				StandardOpenOption.APPEND);
	}

	long savedOffset() throws Exception {
		return Long.parseLong(Files.readString(out.resolve("inbox.offset")));
	}

	long inboxSize() throws Exception {
		return Files.size(in.resolve("inbox.jsonl"));
	}

	@Test
	void missingInboxIsQuietAndStartsAtZero() throws Exception {
		Inbox inbox = inbox();
		inbox.poll();
		assertTrue(calls.isEmpty());
		assertEquals(0, savedOffset(), "the start offset is recorded at once");
		append("{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"first ever\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"1\" A: first ever"), calls, "an inbox created later is read from its start");
	}

	@Test
	void noOffsetStartsAtTheEndAndNeverReplays() throws Exception {
		append("{\"type\":\"whitelist_add\",\"id\":\"old\",\"name\":\"Removed\",\"requested_by\":\"x\"}\n");
		Inbox inbox = inbox();
		assertEquals(inboxSize(), savedOffset(), "persisted at once, so a restart before any new line keeps it");
		inbox.poll();
		assertTrue(calls.isEmpty());
		append("{\"type\":\"say\",\"id\":\"new\",\"author\":\"A\",\"text\":\"after start\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"new\" A: after start"), calls);
	}

	@Test
	void unreadableOrNegativeOffsetStartsAtTheEnd() throws Exception {
		append("{\"type\":\"say\",\"id\":\"old\",\"author\":\"A\",\"text\":\"old\"}\n");
		for (String bad : List.of("garbage", "-5", "")) {
			Files.writeString(out.resolve("inbox.offset"), bad);
			Inbox inbox = inbox();
			inbox.poll();
			assertTrue(calls.isEmpty(), bad);
			assertEquals(inboxSize(), savedOffset(), bad);
		}
	}

	@Test
	void dispatchesBothVerbsAndPersistsOffset() throws Exception {
		Inbox inbox = inbox();
		append("{\"type\":\"say\",\"id\":\"a1\",\"author\":\"Mike\",\"text\":\"hi\"}\n");
		append("{\"type\":\"whitelist_add\",\"id\":7,\"name\":\"Notch\",\"requested_by\":\"Mike\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"a1\" Mike: hi", "wl 7 Notch by Mike"), calls);
		assertEquals(inboxSize(), savedOffset());
		inbox.poll();
		assertEquals(2, calls.size(), "nothing re-read");
		inbox().poll();
		assertEquals(2, calls.size(), "offset survives a restart");
		assertFalse(Files.exists(out.resolve("inbox.offset.tmp")));
		assertEquals(List.of("inbox.jsonl"), names(in), "the mod writes nothing into in/");
	}

	@Test
	void partialLineWaitsForItsNewline() throws Exception {
		Inbox inbox = inbox();
		append("{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"one\"}\n{\"type\":\"say\",\"id\":\"2\",");
		inbox.poll();
		assertEquals(List.of("say \"1\" A: one"), calls);
		append("\"author\":\"B\",\"text\":\"two\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"1\" A: one", "say \"2\" B: two"), calls);
	}

	@Test
	void sanitizesAndClampsSay() throws Exception {
		Inbox inbox = inbox();
		String longText = "x".repeat(300);
		String longAuthor = "y".repeat(40);
		append("{\"type\":\"say\",\"id\":\"1\",\"author\":\"§c" + longAuthor + "\",\"text\":\"/op me\\n§k" + longText + "\"}\n");
		inbox.poll();
		String expected = "say \"1\" " + "y".repeat(32) + ": " + ("/op me " + longText).substring(0, 256);
		assertEquals(List.of(expected), calls);
	}

	@Test
	void shrunkInboxIsReadFromZero() throws Exception {
		Inbox inbox = inbox();
		append("{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"first file, long enough\"}\n");
		inbox.poll();
		Files.delete(in.resolve("inbox.jsonl"));
		append("{\"type\":\"say\",\"id\":\"2\",\"author\":\"A\",\"text\":\"new\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"1\" A: first file, long enough", "say \"2\" A: new"), calls);
	}

	@Test
	void overlongLineIsSkippedNotBufferedForever() throws Exception {
		Inbox inbox = inbox();
		append("{\"type\":\"say\",\"text\":\"" + "z".repeat(Inbox.MAX_LINE + 10) + "\"}\n");
		append("{\"type\":\"say\",\"id\":\"ok\",\"author\":\"A\",\"text\":\"after\"}\n");
		assertTrue(drain(inbox) <= 4, "the over-long line costs one poll, its tail another");
		assertEquals(List.of("say \"ok\" A: after"), calls);
	}

	@Test
	void eachPollIsBoundedAndSavesItsOffset() throws Exception {
		Inbox inbox = inbox();
		StringBuilder sb = new StringBuilder();
		for (int i = 0; i < 2000; i++) {
			sb.append("{\"type\":\"say\",\"id\":\"").append(i).append("\",\"author\":\"A\",\"text\":\"msg ").append(i).append("\"}\n");
		}
		append(sb.toString()); // about 100 KiB: over one poll's byte budget, under the backlog cap
		inbox.poll();
		assertEquals(Inbox.MAX_LINES_PER_POLL, calls.size(), "one poll handles at most MAX_LINES_PER_POLL lines");
		assertEquals(inbox.offset(), savedOffset(), "and saves the offset before returning");
		new Inbox(in, out, handler, hourPerRead).poll(); // a restart resumes after the saved chunk, no replay
		assertEquals("say \"200\" A: msg 200", calls.get(Inbox.MAX_LINES_PER_POLL));
		drain(inbox());
		assertEquals(2000, calls.size());
		assertEquals("say \"1999\" A: msg 1999", calls.get(1999));
	}

	@Test
	void eachPollReadsAtMostTheByteBudget() throws Exception {
		Inbox inbox = inbox();
		String line = "{\"type\":\"say\",\"id\":\"x\",\"author\":\"A\",\"text\":\"" + "w".repeat(1000) + "\"}\n";
		append(line.repeat(150)); // 150 lines of ~1 KiB: the byte budget, not the line budget, binds
		inbox.poll();
		assertTrue(inbox.offset() <= Inbox.MAX_BYTES_PER_POLL, "offset " + inbox.offset());
		assertEquals(inbox.offset() / line.length(), calls.size());
		assertEquals(0, inbox.offset() % line.length(), "stops on a line boundary");
	}

	@Test
	void aBacklogOverTheCapIsSkippedToTheLastNewline() throws Exception {
		Inbox inbox = inbox();
		String flood = "{\"type\":\"say\",\"id\":\"f\",\"author\":\"A\",\"text\":\"flood\"}\n";
		append(flood.repeat((int) (Inbox.MAX_BACKLOG / flood.length()) + 10));
		append("{\"type\":\"say\",\"id\":\"p\",\"au"); // a partial line at the end stays partial
		long lastNewline = inboxSize() - "{\"type\":\"say\",\"id\":\"p\",\"au".length();
		inbox.poll();
		assertTrue(calls.isEmpty(), "nothing in the flood is acted on");
		assertEquals(lastNewline, savedOffset());
		append("thor\":\"A\",\"text\":\"probe\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"p\" A: probe"), calls, "the inbox keeps working after the skip");
	}

	static List<String> names(Path dir) throws Exception {
		try (var s = Files.list(dir)) {
			return s.map(p -> p.getFileName().toString()).sorted().toList();
		}
	}
}
