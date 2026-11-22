package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.mojang.brigadier.CommandDispatcher;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import net.minecraft.SharedConstants;
import net.minecraft.commands.CommandSource;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.server.Bootstrap;
import net.minecraft.server.permissions.LevelBasedPermissionSet;
import net.minecraft.resources.Identifier;
import net.minecraft.world.entity.ai.attributes.AttributeInstance;
import net.minecraft.world.entity.ai.attributes.AttributeModifier;
import net.minecraft.world.entity.ai.attributes.Attributes;
import net.minecraft.world.phys.Vec2;
import net.minecraft.world.phys.Vec3;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * LocatorBar.Waypoints on Minecraft's own AttributeInstance (MC 26.1.2, bootstrapped, no server): the values a
 * friend's client is sent are getValue(), and what reaches playerdata is pack(). The player's tags are a real set,
 * called the way {@code LocatorBar.of} calls the player's (LocatorWiringTest pins that one line). Plus the real
 * command tree: a friend without op can run /hide and /here.
 */
class LocatorBarTest {
	static final double VANILLA = Locator.MAX_RANGE; // Player.createAttributes: both waypoint ranges
	/** Vanilla's sneak modifier on transmit range (LivingEntity), the same shape as /hide's. */
	static final Identifier CROUCH = Identifier.withDefaultNamespace("waypoint_transmit_range_crouch");

	@BeforeAll
	static void bootstrap() {
		SharedConstants.tryDetectVersion();
		Bootstrap.bootStrap();
	}

	/** A player as the server holds it: the two attribute instances and the entity's tag set. */
	static final class Player {
		AttributeInstance receive = new AttributeInstance(Attributes.WAYPOINT_RECEIVE_RANGE, i -> {});
		AttributeInstance transmit = new AttributeInstance(Attributes.WAYPOINT_TRANSMIT_RANGE, i -> {});
		final Set<String> tags = new HashSet<>();

		Player() {
			receive.setBaseValue(VANILLA);
			transmit.setBaseValue(VANILLA);
		}

		Locator.Target target() {
			return new LocatorBar.Waypoints(receive, transmit, tags::contains, tags::add, tags::remove);
		}

		/** PlayerList.respawn: a new player with the base values (restoreFrom) and the tags; transient modifiers gone. */
		Player respawn() {
			Player n = new Player();
			n.receive.setBaseValue(receive.getBaseValue());
			n.transmit.setBaseValue(transmit.getBaseValue());
			n.tags.addAll(tags);
			return n;
		}
	}

	static void nothingSaved(Player p) {
		assertEquals(List.of(), p.receive.pack().modifiers(), "the cap never reaches playerdata");
		assertEquals(List.of(), p.transmit.pack().modifiers(), "/hide never reaches playerdata as a modifier");
		assertEquals(VANILLA, p.receive.pack().baseValue());
		assertEquals(VANILLA, p.transmit.pack().baseValue());
	}

	@Test
	void joinCapsWhatYouSeeToTheRangeAndNothingElse() {
		Player p = new Player();
		assertEquals(VANILLA, p.receive.getValue(), "uncapped before the join hook");
		Locator.apply(p.target(), 200);
		assertEquals(200.0, p.receive.getValue(), 0.0);
		assertTrue(p.receive.hasModifier(LocatorBar.RECEIVE_CAP));
		assertEquals(VANILLA, p.transmit.getValue(), "your own dot is untouched");
		assertFalse(p.transmit.hasModifier(LocatorBar.HIDE));
		nothingSaved(p);
		Locator.apply(p.target(), 200);
		assertEquals(200.0, p.receive.getValue(), 0.0, "a second join hook on the same player changes nothing");
		assertEquals(1, p.receive.getModifiers().size());
	}

	@Test
	void aLowerReceiveIsLeftAloneAndAStaleCapIsRemoved() {
		Player p = new Player();
		Locator.apply(p.target(), 200);
		p.receive.setBaseValue(120); // an admin's /attribute ... base set 120, then a rejoin
		Locator.apply(p.target(), 200);
		assertEquals(120.0, p.receive.getValue(), 0.0, "the cap never raises a lower range");
		assertFalse(p.receive.hasModifier(LocatorBar.RECEIVE_CAP), "capAmount 0 removes the old cap");
	}

	@Test
	void hideTakesYourDotToZeroAndBackAndNeverTouchesWhatYouSee() {
		Player p = new Player();
		Locator.apply(p.target(), 200);
		assertTrue(Locator.toggleHide(p.target()));
		assertEquals(0.0, p.transmit.getValue(), 0.0, "hidden: transmit range 0, no friend gets your dot");
		assertTrue(p.tags.contains(Locator.HIDDEN_TAG));
		assertEquals(200.0, p.receive.getValue(), 0.0, "you still see friends within the cap");
		nothingSaved(p);

		assertFalse(Locator.toggleHide(p.target()), "the second /hide un-hides");
		assertEquals(VANILLA, p.transmit.getValue(), 0.0);
		assertFalse(p.transmit.hasModifier(LocatorBar.HIDE), "the hide modifier is gone, not just outweighed");
		assertFalse(p.tags.contains(Locator.HIDDEN_TAG), "and the tag, so a relog stays visible");
		assertEquals(200.0, p.receive.getValue(), 0.0);
		nothingSaved(p);
	}

	@Test
	void hideIsZeroWhateverTheBaseAndAlongsideSneaking() {
		Player p = new Player();
		p.transmit.setBaseValue(500);
		p.transmit.addTransientModifier(new AttributeModifier(CROUCH, -1.0, AttributeModifier.Operation.ADD_MULTIPLIED_TOTAL));
		Locator.toggleHide(p.target());
		assertEquals(0.0, p.transmit.getValue(), 0.0);
		p.transmit.removeModifier(CROUCH);
		assertEquals(0.0, p.transmit.getValue(), 0.0, "standing up does not un-hide");
	}

	@Test
	void aRespawnOrARelogGetsTheCapAndTheHideBack() {
		Player p = new Player();
		Locator.apply(p.target(), 200);
		Locator.toggleHide(p.target());
		Player after = p.respawn();
		assertEquals(VANILLA, after.receive.getValue(), "a new player starts uncapped: the hook must run");
		assertEquals(VANILLA, after.transmit.getValue());
		Locator.apply(after.target(), 200);
		assertEquals(200.0, after.receive.getValue(), 0.0);
		assertEquals(0.0, after.transmit.getValue(), 0.0, "the saved tag brings /hide back");

		Player visible = new Player();
		Locator.apply(visible.target(), 200);
		assertEquals(VANILLA, visible.transmit.getValue(), "no tag, no hide");
	}

	@Test
	void aPlayerWithoutTheAttributesIsLeftAlone() {
		Locator.Target none = new LocatorBar.Waypoints(null, null, t -> false, t -> true, t -> true);
		assertDoesNotThrow(() -> Locator.apply(none, 200));
		assertDoesNotThrow(() -> Locator.toggleHide(none));
	}

	/** The permission a friend without op has (MinecraftServer.getProfilePermissions for a non-op is ALL). */
	@Test
	void friendsWithoutOpCanRunEveryPlayerCommand() {
		CommandDispatcher<CommandSourceStack> dispatcher = new CommandDispatcher<>();
		PlayerCommands.register(dispatcher, () -> null);
		CommandSourceStack friend = new CommandSourceStack(CommandSource.NULL, Vec3.ZERO, Vec2.ZERO, null,
				LevelBasedPermissionSet.ALL, "Alex", Component.literal("Alex"), null, null);
		assertFalse(Commands.hasPermission(Commands.LEVEL_GAMEMASTERS).test(friend),
				"the stand-in really is a friend without op");
		for (String command : List.of("hide", "here", "coords", "discovery")) {
			var node = dispatcher.getRoot().getChild(command);
			assertNotNull(node, command);
			assertTrue(node.canUse(friend), "/" + command + " is for everyone, not ops");
		}
	}
}
