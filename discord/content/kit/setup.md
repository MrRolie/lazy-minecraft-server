# How to join

**1. Get on the private network (Tailscale)**
The server isn't on the open internet. You reach it through **Tailscale**, a free app that makes a private link between your computer and the server.
• Install it: https://tailscale.com/download
• Sign in with any account (Google, Microsoft, GitHub…). It's free. Use your own account: a work or school Tailscale account can't accept the share.
• Open this link and accept the share: <{secret:share_link}>
• Your Tailscale app should now list **{config:host_name}**. That's the server, and it's the only thing the link gives you access to.
Tailscale starts with your computer; just leave it running (don't quit it).
---
**2. Install the modpack**
The modpack is the file attached to this message. Download it, then import it into a launcher. Either of these works:
• **Prism Launcher** (<https://prismlauncher.org>): **Add Instance** → **Import** → pick the file → **OK**.
• **Modrinth App** (<https://modrinth.com/app>): **Create new instance** → **Upload a modpack** → pick the file. It may warn that the pack isn't hosted on Modrinth ("unknown modpack"): that's expected for this pack, so go ahead.
The launcher downloads Minecraft, Fabric and all the mods for you. The pack already has what it needs, so don't add mods to it yourself. You sign in with your Microsoft account when you first launch the game.

**Give it 4–6 GB of RAM:**
• Prism: right-click the instance → **Edit** → **Settings** → **Java** tab → tick **Memory** → **Maximum memory allocation** 4096–6144 MB.
• Modrinth App: open the instance's settings and find **Custom memory allocation** (recent versions have it under **Sync overrides**) → 4096–6144 MB.

**When a new chapter comes out** (we'll say so in {#announcements}), this message gets the new file. Download it again and import it the same way, then play on the new instance. Your builds and items live on the server, so nothing is lost. You may have to redo your keybinds and video settings.
{attach:pack}
---
**3. Connect**
• Minecraft → Multiplayer → **{config:pack_name}** is already in your list. Double-click it.
• If it's missing, Add Server: `{config:server_address}`
• First time? Get whitelisted: type `/whitelist` and your Minecraft username anywhere in this Discord, e.g. `/whitelist Steve`. Only you see the answer. If it doesn't work, post your username in {#help}.
---
**Something not working?**
• **Can't connect / timed out:** check that Tailscale is on and shows {config:host_name}. Use the full address above, not just `{config:host_name}`.
• **"Incompatible mods" or version errors:** you're probably on an old version of the pack. Download the file from step 2 again and import it. Your game and the server have to match.
• **Still stuck:** ask in {#help} and include a screenshot.
