package net.vanillaextended.vecommands;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * mcve/out/events.jsonl writer (contract: mod/CONTRACT.md). The JSON line is built on the caller's thread (so "ts"
 * is the event time); the append runs on one daemon thread, so no tick ever waits on the disk. out/ is mod-only, and
 * every path in it is still checked and opened without following links (SafeFiles).
 */
final class EventLog {
	static final long DEFAULT_ROTATE_BYTES = 5L * 1024 * 1024;
	private static final Logger LOG = LoggerFactory.getLogger("ve-commands");
	private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
	/** Fixed width, always with milliseconds (Instant.toString drops ".000"). */
	private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'")
			.withZone(ZoneOffset.UTC);

	private final Path dir;
	private final Path file;
	private final Path rotated;
	private final long rotateBytes;
	private final ExecutorService writer = Executors.newSingleThreadExecutor(r -> {
		Thread t = new Thread(r, "ve-commands-events");
		t.setDaemon(true);
		return t;
	});

	EventLog(Path outDir, long rotateBytes) {
		this.dir = outDir;
		this.file = outDir.resolve("events.jsonl");
		this.rotated = outDir.resolve("events.jsonl.1");
		this.rotateBytes = rotateBytes;
	}

	static String line(String type, Instant ts, Consumer<JsonObject> fields) {
		JsonObject o = new JsonObject();
		o.addProperty("v", 1);
		o.addProperty("ts", TS.format(ts));
		o.addProperty("type", type);
		fields.accept(o);
		return GSON.toJson(o) + "\n";
	}

	void emit(String type, Consumer<JsonObject> fields) {
		String line = line(type, Instant.now(), fields);
		try {
			writer.execute(() -> append(line));
		} catch (RejectedExecutionException e) {
			LOG.warn("event after shutdown, dropped: {}", line.strip());
		}
	}

	/** One write per line; rotates to events.jsonl.1 once the file passes the threshold. A refused path drops it. */
	void append(String line) {
		try {
			if (!SafeFiles.isRealDir(dir) || !SafeFiles.absentOrRegular(file)) return;
			SafeFiles.raceHook.accept(file);
			Files.write(file, line.getBytes(StandardCharsets.UTF_8), StandardOpenOption.CREATE, StandardOpenOption.APPEND,
					LinkOption.NOFOLLOW_LINKS);
			BasicFileAttributes a = SafeFiles.lstat(file);
			if (a != null && a.size() > rotateBytes && SafeFiles.absentOrRegular(rotated)) {
				Files.move(file, rotated, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
			}
		} catch (IOException e) {
			LOG.error("could not write {}: {}", file, e.toString());
		}
	}

	/** Drains queued events (bounded) and stops the writer thread. */
	void close() {
		writer.shutdown();
		try {
			if (!writer.awaitTermination(5, TimeUnit.SECONDS)) LOG.warn("event writer did not drain in 5 s");
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
		}
	}
}
