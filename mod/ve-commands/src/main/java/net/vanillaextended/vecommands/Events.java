package net.vanillaextended.vecommands;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.List;
import java.util.function.Consumer;

/**
 * The fields of each events.jsonl type, in contract order (mod/CONTRACT.md). The mod emits only through these, and
 * the tests rebuild every line of mod/contract-examples/events.jsonl through them byte for byte.
 */
final class Events {
	private Events() {}

	static Consumer<JsonObject> server(String state) {
		return o -> o.addProperty("state", state);
	}

	static Consumer<JsonObject> chat(String player, String text) {
		return o -> {
			o.addProperty("player", player);
			o.addProperty("text", text);
		};
	}

	static Consumer<JsonObject> game(String text) {
		return o -> o.addProperty("text", text);
	}

	/** discovery ({@code field} "text") and coords ({@code field} "name"). */
	static Consumer<JsonObject> place(String player, String dim, int x, int y, int z, String biome, String field,
			String value) {
		return o -> {
			o.addProperty("player", player);
			o.addProperty("dim", dim);
			o.addProperty("x", x);
			o.addProperty("y", y);
			o.addProperty("z", z);
			o.addProperty("biome", biome);
			o.addProperty(field, value);
		};
	}

	static Consumer<JsonObject> status(List<String> online, int max) {
		JsonArray names = new JsonArray();
		online.forEach(names::add);
		return o -> {
			o.add("online", names);
			o.addProperty("max", max);
		};
	}

	static Consumer<JsonObject> whitelistResult(JsonElement id, String name, boolean ok, String detail) {
		return o -> {
			o.add("id", id);
			o.addProperty("name", name);
			o.addProperty("ok", ok);
			o.addProperty("detail", detail);
		};
	}
}
