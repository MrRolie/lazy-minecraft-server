package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonElement;
import com.google.gson.JsonParser;
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

/** The mod-side limits: the bridge is untrusted, so its own rate limits do not count. */
class LimitsTest {
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

	Inbox inbox() {
		return new Inbox(in, out, handler, now::get);
	}

	void append(String s) throws Exception {
		Files.writeString(in.resolve("inbox.jsonl"), s, StandardCharsets.UTF_8, StandardOpenOption.CREATE,
				StandardOpenOption.APPEND);
	}

	String says(int from, int n) {
		StringBuilder sb = new StringBuilder();
		for (int i = from; i < from + n; i++) {
			sb.append("{\"type\":\"say\",\"id\":\"s").append(i).append("\",\"author\":\"A\",\"text\":\"t\"}\n");
		}
		return sb.toString();
	}

	@Test
	void sayIsLimitedToFivePerSecondAndTheExcessDropped() throws Exception {
		Inbox inbox = inbox();
		append(says(0, 50));
		inbox.poll();
		assertEquals(List.of("say \"s0\"", "say \"s1\"", "say \"s2\"", "say \"s3\"", "say \"s4\""), calls);
		assertEquals(Files.size(in.resolve("inbox.jsonl")), inbox.offset(), "the excess is consumed, not queued");
		now.addAndGet(1_000_000_000L);
		append(says(50, 50));
		inbox.poll();
		assertEquals(10, calls.size(), "one second later, five more");
		assertEquals("say \"s50\"", calls.get(5));
	}

	@Test
	void whitelistAddIsCappedAtTenPerHourAndTheExcessAnswered() throws Exception {
		Inbox inbox = inbox();
		StringBuilder sb = new StringBuilder();
		for (int i = 0; i < 12; i++) sb.append("{\"type\":\"whitelist_add\",\"id\":").append(i).append(",\"name\":\"Player").append(i).append("\"}\n");
		append(sb.toString());
		inbox.poll();
		assertEquals(12, calls.size());
		for (int i = 0; i < 10; i++) assertEquals("wl " + i, calls.get(i));
		assertEquals(List.of("limited 10", "limited 11"), calls.subList(10, 12));
		now.addAndGet(360_000_000_000L); // six minutes: one more token
		append("{\"type\":\"whitelist_add\",\"id\":12,\"name\":\"Player12\"}\n{\"type\":\"whitelist_add\",\"id\":13,\"name\":\"Player13\"}\n");
		inbox.poll();
		assertEquals(List.of("wl 12", "limited 13"), calls.subList(12, 14));
	}

	@Test
	void invalidNamesDoNotSpendWhitelistTokens() throws Exception {
		Inbox inbox = inbox();
		StringBuilder sb = new StringBuilder();
		for (int i = 0; i < 15; i++) sb.append("{\"type\":\"whitelist_add\",\"id\":\"bad").append(i).append("\",\"name\":\"bad name!\"}\n");
		for (int i = 0; i < 10; i++) sb.append("{\"type\":\"whitelist_add\",\"id\":").append(i).append(",\"name\":\"Player").append(i).append("\"}\n");
		append(sb.toString());
		inbox.poll();
		assertEquals(25, calls.size());
		assertTrue(calls.stream().noneMatch(c -> c.startsWith("limited")), "15 invalid names left all 10 tokens: " + calls);
		assertEquals("wl 9", calls.get(24));
	}

	@Test
	void idIsAStringOrAnInteger() {
		for (String ok : List.of("\"a\"", "\"\"", "42", "-7", "0")) {
			assertTrue(Inbox.validId(JsonParser.parseString(ok)), ok);
		}
		for (String bad : List.of("true", "false", "null", "1.5", "1e3", "[1]", "{\"a\":1}", "12345678901234567890")) {
			assertFalse(Inbox.validId(JsonParser.parseString(bad)), bad);
		}
		assertFalse(Inbox.validId(null));
	}

	@Test
	void linesMustBeStrictJson() throws Exception {
		Inbox inbox = inbox();
		append("{type:say,id:'7',author:a,text:lenient}\n"            // unquoted names, single quotes
				+ "{\"type\":\"say\",\"id\":\"c\",\"author\":\"A\",\"text\":\"t\"} // comment\n"
				+ "{\"type\":\"say\",\"id\":\"t\",\"author\":\"A\",\"text\":\"t\",}\n"   // trailing comma
				+ "{\"type\":\"say\",\"id\":\"g\",\"author\":\"A\",\"text\":\"t\"} trailing\n"
				+ "{\"type\":\"say\",\"id\":NaN,\"author\":\"A\",\"text\":\"t\"}\n"
				+ "{\"type\":\"say\",\"id\":\"ok\",\"author\":\"A\",\"text\":\"strict\"}\n");
		inbox.poll();
		assertEquals(List.of("say \"ok\""), calls, "only the strict line");
	}

	@Test
	void rateLimitRefillsAndCaps() {
		AtomicLong t = new AtomicLong();
		RateLimit r = new RateLimit(2, 1, t::get);
		assertTrue(r.tryAcquire());
		assertTrue(r.tryAcquire());
		assertFalse(r.tryAcquire());
		t.addAndGet(10_000_000_000L); // ten seconds refills to the burst (2), not to 10
		assertTrue(r.tryAcquire());
		assertTrue(r.tryAcquire());
		assertFalse(r.tryAcquire());
	}

	@Test
	void logThrottleSpeaksOncePerWindowAndCountsTheRest() {
		AtomicLong t = new AtomicLong();
		LogThrottle l = new LogThrottle(60_000, t::get);
		assertEquals(0, l.next());
		assertEquals(-1, l.next());
		assertEquals(-1, l.next());
		t.addAndGet(60_000_000_000L);
		assertEquals(2, l.next());
	}
}
