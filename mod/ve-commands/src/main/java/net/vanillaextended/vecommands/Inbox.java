package net.vanillaextended.vecommands;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.JsonPrimitive;
import com.google.gson.Strictness;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.IOException;
import java.io.InputStream;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.function.LongSupplier;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * mcve/in/inbox.jsonl reader (contract: mod/CONTRACT.md). Called about once a second off the server thread. Only
 * complete ('\n'-terminated) lines are consumed, so a line the bridge is still writing waits for the next poll. The
 * byte offset persists in mcve/out/inbox.offset (mod-only) after each poll's lines are dispatched, so a crash mid-poll
 * can replay at most that one poll's budget. With no usable offset the mod starts at the END of the inbox: a replay
 * could re-add names the operator has since removed. Lines must be strict JSON. Unknown types and malformed lines are
 * skipped (the offset moves past them) and reported throttled: at most one log line a minute, with the count and the
 * first offending line, so a hostile inbox cannot amplify itself into the server log. in/ is bridge-writable, so the
 * inbox is opened without following links and only if it is a regular file (SafeFiles). Nothing in here can run a
 * command: the only outlets are the Handler verbs.
 *
 * <p>The bridge is untrusted here, so the mod bounds what a flood can do (mod/CONTRACT.md "Security boundary"):
 * at most {@link #MAX_LINES_PER_POLL} lines or {@link #MAX_BYTES_PER_POLL} bytes per poll, with the offset saved after
 * each poll; a backlog over {@link #MAX_BACKLOG} is skipped (offset to the end); {@code say} is rate-limited and
 * {@code whitelist_add} capped, and the excess is dropped.
 */
final class Inbox {
	/** The inbox verbs. {@code id} is echoed back as sent (a string or an integer). */
	interface Handler {
		void say(JsonElement id, String author, String text);

		void whitelistAdd(JsonElement id, String name, String requestedBy);

		/** A whitelist_add over the cap: answered (ok=false, "rate limited"), never acted on. */
		void whitelistRateLimited(JsonElement id, String name);
	}

	static final int MAX_AUTHOR = 32;
	static final int MAX_TEXT = 256;
	/** A line longer than this with no '\n' is skipped as malformed rather than buffered forever. */
	static final int MAX_LINE = 64 * 1024;
	static final int MAX_LINES_PER_POLL = 200;
	static final int MAX_BYTES_PER_POLL = MAX_LINE;
	/** A backlog this large is a flood, not news: skip to the end. */
	static final long MAX_BACKLOG = 1024 * 1024;
	/** say: 5 per second sustained, bursts of 5. whitelist_add: 10 per hour, bursts of 10. */
	static final double SAY_PER_SECOND = 5;
	static final double WHITELIST_PER_HOUR = 10;
	private static final Pattern INTEGER = Pattern.compile("-?[0-9]{1,18}");
	private static final Logger LOG = LoggerFactory.getLogger("ve-commands");

	private final Path inDir;
	private final Path outDir;
	private final Path file;
	private final Path offsetFile;
	private final Handler handler;
	private final RateLimit says;
	private final RateLimit whitelists;
	private final LogThrottle droppedLog;
	private final LogThrottle skippedLog;
	private final LogThrottle badLineLog;
	private final LogThrottle shrinkLog;
	private final LogThrottle failureLog;
	/** One reset to offset 0 per minute; a bridge that keeps truncating and rewriting gets skipped to the end. */
	private final RateLimit shrinkResets;
	private long droppedSays;
	private long badLines;
	private long refusedAdds;
	private String firstRefusedAdd;
	private final LogThrottle refusedLog;
	private String firstBadLine;
	private long offset;

	Inbox(Path inDir, Path outDir, Handler handler) {
		this(inDir, outDir, handler, System::nanoTime);
	}

	Inbox(Path inDir, Path outDir, Handler handler, LongSupplier nanoClock) {
		this.inDir = inDir;
		this.outDir = outDir;
		this.file = inDir.resolve("inbox.jsonl");
		this.offsetFile = outDir.resolve("inbox.offset");
		this.handler = handler;
		this.says = new RateLimit(SAY_PER_SECOND, SAY_PER_SECOND, nanoClock);
		this.whitelists = new RateLimit(WHITELIST_PER_HOUR, WHITELIST_PER_HOUR / 3600, nanoClock);
		this.droppedLog = new LogThrottle(60_000, nanoClock);
		this.skippedLog = new LogThrottle(60_000, nanoClock);
		this.badLineLog = new LogThrottle(60_000, nanoClock);
		this.shrinkLog = new LogThrottle(60_000, nanoClock);
		this.failureLog = new LogThrottle(60_000, nanoClock);
		this.refusedLog = new LogThrottle(60_000, nanoClock);
		this.shrinkResets = new RateLimit(1, 1.0 / 60, nanoClock);
		this.offset = loadOffset();
	}

	long offset() {
		return offset;
	}

	private long loadOffset() {
		try {
			if (SafeFiles.absentOrRegular(offsetFile) && SafeFiles.lstat(offsetFile) != null) {
				SafeFiles.raceHook.accept(offsetFile);
				try (InputStream in = Files.newInputStream(offsetFile, LinkOption.NOFOLLOW_LINKS)) {
					long saved = Long.parseLong(new String(in.readAllBytes(), StandardCharsets.UTF_8).strip());
					if (saved < 0) throw new NumberFormatException("negative: " + saved);
					return saved;
				}
			}
		} catch (IOException | NumberFormatException e) {
			LOG.warn("unreadable {} ({})", offsetFile, e.toString());
		}
		long end = 0;
		try {
			BasicFileAttributes a = SafeFiles.lstat(file);
			if (a != null && a.isRegularFile()) end = a.size();
			offset = end;
			saveOffset();
		} catch (IOException e) {
			LOG.error("could not record the inbox start offset: {}", e.toString());
		}
		LOG.warn("no usable {}; starting at the end of the inbox (byte {}), nothing is replayed", offsetFile, end);
		return end;
	}

	private void saveOffset() throws IOException {
		if (!SafeFiles.isRealDir(outDir) || !SafeFiles.absentOrRegular(offsetFile)) return;
		Path tmp = offsetFile.resolveSibling("inbox.offset.tmp");
		Files.deleteIfExists(tmp); // removes a planted link itself, never its target
		SafeFiles.raceHook.accept(tmp);
		Files.write(tmp, Long.toString(offset).getBytes(StandardCharsets.UTF_8), StandardOpenOption.CREATE_NEW,
				StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);
		Files.move(tmp, offsetFile, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
	}

	/**
	 * Reads and dispatches complete lines past the offset, at most one budget's worth, and saves the offset. Called
	 * from a {@link Ticker}, which survives any throw.
	 */
	void poll() {
		try {
			if (!SafeFiles.isRealDir(inDir) || !SafeFiles.absentOrRegular(file) || SafeFiles.lstat(file) == null) return;
			SafeFiles.raceHook.accept(file);
			try (FileChannel ch = FileChannel.open(file, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)) {
				long size = ch.size();
				long start = offset;
				if (size < offset) {
					boolean reset = shrinkResets.tryAcquire();
					long to = reset ? 0 : afterLastNewline(ch, size);
					long s = shrinkLog.next();
					if (s >= 0) {
						LOG.warn("inbox.jsonl shrank ({} < offset {}); {} ({} more shrinks since the last report)", size,
								offset, reset ? "reading it from 0" : "a repeat within a minute: skipped to its end", s);
					}
					offset = to;
				}
				if (size - offset > MAX_BACKLOG) {
					long to = afterLastNewline(ch, size);
					long s = skippedLog.next();
					if (s >= 0) {
						LOG.warn("inbox backlog of {} bytes is over {}: skipped to byte {} unread (a flood, not news; "
								+ "{} more skips since the last report)", size - offset, MAX_BACKLOG, to, s);
					}
					offset = to;
				} else {
					readBudget(ch, size);
				}
				if (offset != start) saveOffset();
			}
		} catch (IOException e) {
			long s = failureLog.next();
			if (s >= 0) LOG.error("inbox read failed: {} ({} more since the last report)", e.toString(), s);
		}
		reportDrops();
		reportBadLines();
	}

	/** Dispatches up to the per-poll budget of complete lines and advances the offset past them. */
	private void readBudget(FileChannel ch, long size) throws IOException {
		if (offset >= size) return;
		ByteBuffer buf = ByteBuffer.allocate((int) Math.min(size - offset, MAX_BYTES_PER_POLL));
		while (buf.hasRemaining() && ch.read(buf, offset + buf.position()) > 0) {}
		int end = buf.position();
		byte[] bytes = buf.array();
		int lineStart = 0;
		int lines = 0;
		for (int i = 0; i < end && lines < MAX_LINES_PER_POLL; i++) {
			if (bytes[i] == '\n') {
				handle(new String(bytes, lineStart, i - lineStart, StandardCharsets.UTF_8), offset + lineStart);
				lineStart = i + 1;
				lines++;
			}
		}
		if (lineStart == 0 && end == MAX_BYTES_PER_POLL) {
			badLine(offset, "over " + MAX_LINE + " bytes with no newline", "");
			lineStart = end;
		}
		offset += lineStart; // a trailing partial line (the bridge mid-write) waits for the next poll
	}

	/** The offset just past the last '\n' in the file's final {@link #MAX_LINE} bytes, else the file size. */
	private static long afterLastNewline(FileChannel ch, long size) throws IOException {
		int n = (int) Math.min(size, MAX_LINE);
		ByteBuffer buf = ByteBuffer.allocate(n);
		while (buf.hasRemaining() && ch.read(buf, size - n + buf.position()) > 0) {}
		byte[] bytes = buf.array();
		for (int i = buf.position() - 1; i >= 0; i--) {
			if (bytes[i] == '\n') return size - n + i + 1;
		}
		return size;
	}

	private void badLine(long at, String why, String line) {
		badLines++;
		if (firstBadLine == null) {
			// Everything here came from the bridge: escaped, so it cannot forge or hide log lines.
			firstBadLine = "byte " + at + " (" + Text.forLog(why, 200) + "): " + Text.forLog(line, 200);
		}
	}

	private void reportBadLines() {
		if (badLines == 0) return;
		long s = badLineLog.next();
		if (s < 0) return;
		LOG.warn("skipped {} malformed or unknown inbox line(s); the first at {}", badLines, firstBadLine);
		badLines = 0;
		firstBadLine = null;
	}

	/** Strict JSON (RFC 8259): no unquoted names, single quotes, comments, NaN or trailing data. */
	static JsonElement parseStrict(String line) throws IOException {
		JsonReader r = new JsonReader(new StringReader(line));
		r.setStrictness(Strictness.STRICT);
		JsonElement e = JsonParser.parseReader(r);
		if (r.peek() != JsonToken.END_DOCUMENT) throw new IOException("trailing data after the JSON value");
		return e;
	}

	private void reportDrops() {
		if (refusedAdds > 0 && refusedLog.next() >= 0) {
			LOG.warn("refused {} whitelist_add over the {}/hour cap; the first: {}", refusedAdds, (int) WHITELIST_PER_HOUR,
					firstRefusedAdd);
			refusedAdds = 0;
			firstRefusedAdd = null;
		}
		if (droppedSays == 0) return;
		long s = droppedLog.next();
		if (s < 0) return;
		LOG.warn("dropped {} inbox say lines over the {}/s limit", droppedSays, (int) SAY_PER_SECOND);
		droppedSays = 0;
	}

	private void handle(String raw, long at) {
		String line = raw.strip();
		if (line.isEmpty()) return;
		try {
			JsonElement parsed = parseStrict(line);
			if (!parsed.isJsonObject()) throw new IllegalArgumentException("not a JSON object");
			JsonObject o = parsed.getAsJsonObject();
			String type = string(o, "type");
			JsonElement id = o.get("id");
			if (!validId(id)) throw new IllegalArgumentException("id missing, or not a string or an integer");
			switch (type) {
				case "say" -> {
					String author = Text.truncate(Text.clean(string(o, "author")), MAX_AUTHOR);
					String text = Text.truncate(Text.clean(string(o, "text")), MAX_TEXT);
					if (text.isEmpty()) throw new IllegalArgumentException("empty text");
					if (says.tryAcquire()) handler.say(id, author.isEmpty() ? "?" : author, text);
					else droppedSays++;
				}
				case "whitelist_add" -> {
					String name = string(o, "name");
					JsonElement by = o.get("requested_by");
					String requestedBy = by != null && by.isJsonPrimitive() ? by.getAsString() : "?";
					// Only a name that can be added costs a token; an invalid one is answered "invalid name" for free.
					if (!Text.PLAYER_NAME.matcher(name).matches() || whitelists.tryAcquire()) {
						handler.whitelistAdd(id, name, requestedBy);
					}
					else {
						if (refusedAdds++ == 0) firstRefusedAdd = Text.forLog(id.toString(), 40) + " for " + Text.forLog(name, 32);
						handler.whitelistRateLimited(id, name);
					}
				}
				default -> throw new IllegalArgumentException("unknown type " + Text.forLog(type, 40));
			}
		} catch (RuntimeException | IOException e) {
			badLine(at, String.valueOf(e.getMessage()), line);
		}
	}

	/** A JSON string, or a JSON integer (no bool, null, fraction, exponent, object or array). */
	static boolean validId(JsonElement id) {
		if (id == null || !id.isJsonPrimitive()) return false;
		JsonPrimitive p = id.getAsJsonPrimitive();
		return p.isString() || (p.isNumber() && INTEGER.matcher(p.getAsString()).matches());
	}

	private static String string(JsonObject o, String key) {
		JsonElement e = o.get(key);
		if (e == null || !e.isJsonPrimitive() || !e.getAsJsonPrimitive().isString()) {
			throw new IllegalArgumentException("missing string '" + key + "'");
		}
		return e.getAsString();
	}
}
