package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Consumer;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** mod/contract-examples/ is shared with the bridge's tests; the mod must produce and accept exactly these. */
class ContractExamplesTest {
	static final Path EXAMPLES = Path.of(System.getProperty("vecommands.contractExamples", "../contract-examples"));

	@TempDir
	Path root;

	static List<String> lines(String name) throws Exception {
		String s = Files.readString(EXAMPLES.resolve(name), StandardCharsets.UTF_8);
		assertTrue(s.endsWith("\n"), name + " ends with a newline");
		return List.of(s.substring(0, s.length() - 1).split("\n", -1));
	}

	/** Every example event is rebuilt through Events and must come out byte for byte. */
	@Test
	void everyEventTypeIsProducedExactlyAsTheExample() throws Exception {
		Set<String> types = new TreeSet<>();
		for (String line : lines("events.jsonl")) {
			JsonObject o = JsonParser.parseString(line).getAsJsonObject();
			String type = o.get("type").getAsString();
			types.add(type);
			Consumer<JsonObject> fields = switch (type) {
				case "server" -> Events.server(o.get("state").getAsString());
				case "chat" -> Events.chat(o.get("player").getAsString(), o.get("text").getAsString());
				case "game" -> Events.game(o.get("text").getAsString());
				case "discovery", "coords" -> {
					String field = type.equals("discovery") ? "text" : "name";
					yield Events.place(o.get("player").getAsString(), o.get("dim").getAsString(), o.get("x").getAsInt(),
							o.get("y").getAsInt(), o.get("z").getAsInt(), o.get("biome").getAsString(), field,
							o.get(field).getAsString());
				}
				case "status" -> {
					List<String> online = new ArrayList<>();
					for (JsonElement e : o.getAsJsonArray("online")) online.add(e.getAsString());
					yield Events.status(online, o.get("max").getAsInt());
				}
				case "whitelist_result" -> Events.whitelistResult(o.get("id"), o.get("name").getAsString(),
						o.get("ok").getAsBoolean(), o.get("detail").getAsString());
				default -> throw new AssertionError("unknown type in example: " + type);
			};
			assertEquals(line + "\n", EventLog.line(type, Instant.parse(o.get("ts").getAsString()), fields));
		}
		assertEquals(Set.of("server", "chat", "game", "discovery", "coords", "status", "whitelist_result"), types);
	}

	/** The example inbox (edge cases included) yields exactly the expected actions, and the partial line waits. */
	@Test
	void exampleInboxYieldsExactlyTheExpectedActions() throws Exception {
		Path in = Files.createDirectory(root.resolve("in"));
		Path out = Files.createDirectory(root.resolve("out"));
		List<JsonObject> got = new ArrayList<>();
		java.util.concurrent.atomic.AtomicLong now = new java.util.concurrent.atomic.AtomicLong();
		Inbox inbox = new Inbox(in, out, new Inbox.Handler() {
			@Override
			public void say(JsonElement id, String author, String text) {
				JsonObject o = new JsonObject();
				o.addProperty("verb", "say");
				o.add("id", id);
				o.addProperty("author", author);
				o.addProperty("text", text);
				got.add(o);
			}

			@Override
			public void whitelistAdd(JsonElement id, String name, String requestedBy) {
				JsonObject o = new JsonObject();
				o.addProperty("verb", "whitelist_add");
				o.add("id", id);
				o.addProperty("name", name);
				o.addProperty("requested_by", requestedBy);
				o.addProperty("name_valid", Text.PLAYER_NAME.matcher(name).matches());
				got.add(o);
			}

			@Override
			public void whitelistRateLimited(JsonElement id, String name) {
				fail("the examples stay under the limits: " + name);
			}
		}, () -> now.addAndGet(3_600_000_000_000L)); // an hour per clock read: the limits never bind here
		byte[] example = Files.readAllBytes(EXAMPLES.resolve("inbox.jsonl"));
		Files.write(in.resolve("inbox.jsonl"), example);
		inbox.poll();

		List<JsonElement> expected = new ArrayList<>();
		for (String line : lines("inbox.expected.jsonl")) expected.add(JsonParser.parseString(line));
		assertEquals(expected, new ArrayList<JsonElement>(got));

		int lastNewline = new String(example, StandardCharsets.ISO_8859_1).lastIndexOf('\n');
		assertTrue(lastNewline < example.length - 1, "the example ends with a partial line");
		assertEquals(Long.toString(lastNewline + 1), Files.readString(out.resolve("inbox.offset")),
				"the offset stops before the partial line");
	}

	@Test
	void examplesAreValidUtf8JsonLines() throws Exception {
		for (String line : lines("events.jsonl")) {
			JsonObject o = JsonParser.parseString(line).getAsJsonObject();
			assertEquals(1, o.get("v").getAsInt());
			assertTrue(o.get("ts").getAsString().matches("\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z"), line);
		}
	}
}
