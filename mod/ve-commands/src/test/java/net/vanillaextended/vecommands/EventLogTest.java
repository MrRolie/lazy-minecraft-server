package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class EventLogTest {
	@TempDir
	Path dir;

	@Test
	void lineCarriesEnvelopeAndIsOneLine() {
		String line = EventLog.line("chat", Instant.parse("2026-10-01T12:00:00.123456Z"), o -> {
			o.addProperty("player", "Mike");
			o.addProperty("text", "a\nb <c> \"d\"");
		});
		assertTrue(line.endsWith("\n"));
		assertEquals(1, line.chars().filter(ch -> ch == '\n').count());
		JsonObject o = JsonParser.parseString(line).getAsJsonObject();
		assertEquals(1, o.get("v").getAsInt());
		assertEquals("2026-10-01T12:00:00.123Z", o.get("ts").getAsString());
		assertEquals("chat", o.get("type").getAsString());
		assertEquals("a\nb <c> \"d\"", o.get("text").getAsString());
		assertTrue(line.startsWith("{\"v\":1,\"ts\":"), line);
	}

	@Test
	void appendsAndRotatesKeepingOneOldFile() throws Exception {
		EventLog log = new EventLog(dir, 100);
		String line = EventLog.line("server", Instant.EPOCH, o -> o.addProperty("state", "started")); // 61 bytes
		log.append(line);
		assertEquals(List.of(line.strip()), Files.readAllLines(dir.resolve("events.jsonl")));
		log.append(line); // 122 > 100: rotates
		assertFalse(Files.exists(dir.resolve("events.jsonl")));
		assertEquals(2, Files.readAllLines(dir.resolve("events.jsonl.1")).size());
		log.append(line);
		log.append(line); // rotates again, replacing the old .1
		assertEquals(2, Files.readAllLines(dir.resolve("events.jsonl.1")).size());
		log.append(line);
		assertEquals(1, Files.readAllLines(dir.resolve("events.jsonl")).size());
	}

	@Test
	void emitIsAsyncAndCloseDrains() throws Exception {
		EventLog log = new EventLog(dir, EventLog.DEFAULT_ROTATE_BYTES);
		for (int i = 0; i < 100; i++) {
			int n = i;
			log.emit("game", o -> o.addProperty("text", "line " + n));
		}
		log.close();
		List<String> lines = Files.readAllLines(dir.resolve("events.jsonl"), StandardCharsets.UTF_8);
		assertEquals(100, lines.size());
		assertEquals("line 99", JsonParser.parseString(lines.get(99)).getAsJsonObject().get("text").getAsString());
		log.emit("game", o -> o.addProperty("text", "late")); // dropped with a warning, never thrown
	}
}
