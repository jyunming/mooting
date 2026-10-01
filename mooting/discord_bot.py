"""A council in a Discord server or direct message.

A transport over `chat.ChatHost`, like `telegram.py`, so everything a person
can do from a Telegram chat -- pairing, the sign-off buttons, the topic picker,
/run -- works here the day the transport does.

Four facts decide the shape, all of them differences from Telegram:

**2000 characters a message**, half of Telegram's. Splitting is on block
boundaries, and a fenced block that is cut is re-fenced on both sides.

**Five buttons a row, five rows a message.** The topic picker and the team
toggles are one button a row in Telegram; here they are packed five across.

**A direct message is a channel with its own id.** The board files a private
room under the person's account id, as Telegram does, so the transport maps
the account id back to its DM channel when it sends.

**The gateway is a connection the bot opens.** Nothing listens on a port and
nothing needs a public address, which is the reason this was a candidate at
all. The price is the Message Content intent: a bot in a server cannot read
what people type without it, and it is a switch in the developer portal that
`run` names when it is off.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sys

from .telegram import _FENCE, _TABLE_ROW, MENU

log = logging.getLogger("mooting.discord")

#: Discord's ceiling for one message.
LIMIT = 2000
#: Per message: rows of buttons, and buttons in a row.
MAX_ROWS, MAX_PER_ROW = 5, 5
#: A button label is cut here by Discord; cut it first, at a word.
LABEL = 80


# -------------------------------------------------------------- rendering

def md_blocks(md: str) -> list[str]:
    """Split markdown into blocks that must not be cut in half.

    Discord renders markdown, so the text mostly goes as it is. Tables are the
    exception: Discord has none, so a table goes in a code block, where the
    columns stay aligned.
    """
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(md):
        out += _prose_blocks(md[pos:m.start()])
        out.append(m.group(0).strip())
        pos = m.end()
    out += _prose_blocks(md[pos:])
    return [b for b in out if b.strip()]


def _prose_blocks(md: str) -> list[str]:
    out = []
    for para in re.split(r"\n\s*\n", md):
        if not para.strip():
            continue
        lines = [ln for ln in para.splitlines() if ln.strip()]
        if lines and all(_TABLE_ROW.match(ln) for ln in lines):
            out.append("```\n" + "\n".join(lines) + "\n```")
        else:
            out.append(para.rstrip())
    return out


def chunks(md: str, limit: int = LIMIT) -> list[str]:
    """Messages within Discord's ceiling, split between blocks."""
    out: list[str] = []
    current = ""
    for block in md_blocks(md):
        if len(block) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_cut(block, limit))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            out.append(current)
            current = block
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _cut(block: str, limit: int) -> list[str]:
    """One oversized block, cut on lines. A fenced block is closed at every cut
    and opened again after it, because half a fence turns the rest of the
    message into code."""
    fence = re.match(r"^```([A-Za-z0-9_+-]*)\n", block)
    if fence:
        head, body = fence.group(0), block[fence.end():].removesuffix("```").rstrip("\n")
        wrap = lambda s: f"{head}{s.rstrip(chr(10))}\n```"   # noqa: E731
        room = limit - len(head) - 4
    else:
        body, wrap, room = block, (lambda s: s.rstrip("\n")), limit
    out, current = [], ""
    for line in body.splitlines(keepends=True):
        while len(line) > room:
            cut = line.rfind(" ", 0, room - len(current))
            cut = cut if cut > 0 else room - len(current)
            out.append(wrap(current + line[:cut]))
            line, current = line[cut:].lstrip(" "), ""
        if len(current) + len(line) > room:
            out.append(wrap(current))
            current = ""
        current += line
    if current:
        out.append(wrap(current))
    return out


def with_mentions(store, text: str) -> str:
    """`@name` as a real Discord mention for anybody whose account is bound,
    so a person asked a question is pinged rather than written about."""
    def swap(m):
        row = store.q1("SELECT user_id FROM identities WHERE channel = 'discord' "
                       "AND seat = ?", (m.group(1),))
        return f"<@{row['user_id']}>" if row else m.group(0)
    return re.sub(r"(?<![<\w])@([A-Za-z0-9_-]{2,32})", swap, text)


def label(text: str) -> str:
    return text if len(text) <= LABEL else text[:LABEL - 1].rsplit(" ", 1)[0] + "…"


def pack(buttons) -> list[list]:
    """Rows of buttons within Discord's five-by-five.

    Rows that already fit are kept as they were drawn. A list drawn one button
    a row -- the topic picker, the team toggles -- is packed five across, and
    past twenty-five the rest are dropped with a warning rather than the whole
    message being refused.
    """
    rows = [list(r)[:MAX_PER_ROW] for r in buttons if r]
    if len(rows) <= MAX_ROWS:
        return rows
    flat = [b for r in rows for b in r]
    if len(flat) > MAX_ROWS * MAX_PER_ROW:
        log.warning("dropping %d buttons past Discord's 25", len(flat) - 25)
        flat = flat[:MAX_ROWS * MAX_PER_ROW]
    return [flat[i:i + MAX_PER_ROW] for i in range(0, len(flat), MAX_PER_ROW)]


# -------------------------------------------------------------- transport

class DiscordTransport:
    """`chat.Transport` for Discord, over discord.py."""

    channel = "discord"
    pin_hint = "--channel"

    def __init__(self, client, store) -> None:
        self.client, self.store = client, store
        self._dms: dict[str, object] = {}

    async def _where(self, chat_id):
        """The channel to send to. A private room is filed under the person's
        id, so an id that is not a channel is a person to DM."""
        cid = int(chat_id)
        found = self.client.get_channel(cid) or self._dms.get(str(chat_id))
        if found is not None:
            return found
        import discord
        try:
            found = await self.client.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden):
            found = await (await self.client.fetch_user(cid)).create_dm()
            self._dms[str(chat_id)] = found
        return found

    def _view(self, buttons):
        import discord
        view = discord.ui.View(timeout=None)
        for r, row in enumerate(pack(buttons)):
            for b in row:
                style = (discord.ButtonStyle.success if b.text.startswith("✓")
                         else discord.ButtonStyle.danger if b.text.startswith("✗")
                         else discord.ButtonStyle.secondary)
                view.add_item(discord.ui.Button(label=label(b.text), custom_id=b.data,
                                                style=style, row=r))
        return view

    def _mentions(self):
        import discord
        # Never @everyone or a role, whatever a seat wrote.
        return discord.AllowedMentions(everyone=False, roles=False, users=True)

    async def send(self, chat_id, markdown, *, buttons=None, desk=False):
        where = await self._where(chat_id)
        pieces = chunks(with_mentions(self.store, markdown)) or ["​"]
        sent = None
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            view = self._view(buttons) if (buttons and last) else None
            kw = {"view": view} if view is not None else {}
            sent = await where.send(piece, allowed_mentions=self._mentions(), **kw)
            if view is not None:
                # Taps arrive through `on_interaction` whatever the view does;
                # keeping every view in discord.py's store only grows it.
                view.stop()
        return getattr(sent, "id", None)

    async def edit(self, chat_id, message, markdown, *, buttons=None):
        where = await self._where(chat_id)
        await where.get_partial_message(int(message)).edit(
            content=chunks(markdown)[0],
            view=self._view(buttons) if buttons else None)

    async def delete(self, chat_id, message):
        where = await self._where(chat_id)
        await where.get_partial_message(int(message)).delete()

    async def answer(self, tap, text="", alert=False):
        response = tap.handle.response
        if response.is_done():
            return
        if text:
            # Seen only by whoever pressed, which is what an alert is for.
            await response.send_message(text, ephemeral=True)
        else:
            await response.defer()

    async def ask_reply(self, chat_id, text):
        where = await self._where(chat_id)
        sent = await where.send(text.replace("Reply to this with why.",
                                             "Reply to this message with why."))
        return sent.id

    async def typing(self, chat_id):
        await (await self._where(chat_id)).typing()

    async def send_file(self, chat_id, name, data, caption):
        import discord
        where = await self._where(chat_id)
        await where.send(chunks(caption)[0][:LIMIT],
                         file=discord.File(io.BytesIO(data), filename=name),
                         allowed_mentions=self._mentions())

    async def owns_group(self, chat_id, user_id):
        where = self.client.get_channel(int(chat_id))
        guild = getattr(where, "guild", None)
        return bool(guild) and str(guild.owner_id) == str(user_id)

    def addressed(self, text):
        """Text with a leading mention of this bot removed. Discord has no
        `/cmd@bot` form; in a server you address a bot by mentioning it."""
        body = (text or "").strip()
        me = getattr(self.client, "user", None)
        if me is not None:
            body = re.sub(rf"^<@!?{me.id}>\s*", "", body)
        return body


def incoming(message):
    """A discord.py message, as the host sees one."""
    import discord

    from .chat import Incoming

    author = message.author
    private = isinstance(message.channel, discord.DMChannel)
    ref = getattr(message, "reference", None)
    file = None
    if message.attachments:
        att = message.attachments[0]
        file = (att.filename, att.read)
    return Incoming(
        # A private room is filed under the person, as Telegram's is.
        chat_id=str(author.id if private else message.channel.id),
        user_id=str(author.id),
        name=getattr(author, "display_name", None) or author.name,
        text=message.content if file is None else "",
        caption=message.content if file is not None else "",
        private=private,
        reply_to=ref.message_id if ref is not None else None,
        file=file)


def explain_start_failure(exc) -> list[str] | None:
    """The two ways a first run fails, said in terms of what to do."""
    import discord
    if isinstance(exc, discord.LoginFailure):
        return ["Discord refused that token.",
                "Copy it again from https://discord.com/developers/applications",
                "→ your app → Bot → Reset Token, and run "
                "`mooting discord --token <it>`."]
    if isinstance(exc, discord.PrivilegedIntentsRequired):
        return ["The bot cannot read what people type in a server.",
                "Turn on Message Content Intent: "
                "https://discord.com/developers/applications",
                "→ your app → Bot → Privileged Gateway Intents."]
    return None


def run(db, *, bot_token: str, chats, human: str, topic=None,
        remember: bool = False) -> int:        # pragma: no cover - needs a token
    """Connect to the Discord gateway and drive a council from a channel."""
    import discord
    from discord import app_commands

    from .chat import ChatHost, Tap
    from .store import connect

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    store = connect(db)
    transport = DiscordTransport(client, store)
    host = ChatHost(db, store, transport, human=human, chats=chats, topic=topic)
    started = {"pump": False}

    def slash(name: str, description: str):
        """`/name` in Discord's own command menu, routed as the typed form."""
        @tree.command(name=name, description=description[:100])
        @app_commands.describe(args="anything after the command")
        async def command(interaction: discord.Interaction, args: str = ""):
            line = f"/{name} {args}".strip()
            # Discord shows who used a command only once it is answered. A
            # claim code is between the person and the bot, so that echo is not
            # shown to the room.
            await interaction.response.send_message(f"`{line}`",
                                                    ephemeral=name == "pair")
            from .chat import Incoming
            private = isinstance(interaction.channel, discord.DMChannel)
            await host.route(Incoming(
                chat_id=str(interaction.user.id if private else interaction.channel_id),
                user_id=str(interaction.user.id),
                name=interaction.user.display_name, text=line, private=private))
        return command

    for name, description in MENU:
        slash(name, description)

    @client.event
    async def on_ready():
        print(f"  signed  in as {client.user}")
        try:
            for guild in client.guilds:
                # Per server: a global sync takes up to an hour to appear.
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
            print(f"  menu    {len(MENU)} commands registered in "
                  f"{len(client.guilds)} server(s)")
        except Exception as exc:
            print(f"  menu    could NOT register commands: {exc}", file=sys.stderr)
        if remember:
            store.set_setting("discord.token", bot_token)
            print(f"  token   saved to {store.path} — you will not be asked again")
        if not client.guilds:
            perms = discord.Permissions(view_channel=True, send_messages=True,
                                        read_message_history=True, attach_files=True,
                                        use_application_commands=True)
            print("  invite  " + discord.utils.oauth_url(
                client.user.id, permissions=perms,
                scopes=("bot", "applications.commands")))
        if not started["pump"]:
            started["pump"] = True
            asyncio.create_task(host.pump())

    @client.event
    async def on_guild_join(guild):
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        await host.route(incoming(message))

    @client.event
    async def on_interaction(interaction):
        if interaction.type != discord.InteractionType.component:
            return
        private = isinstance(interaction.channel, discord.DMChannel)
        await host.on_tap(Tap(
            chat_id=str(interaction.user.id if private else interaction.channel_id),
            user_id=str(interaction.user.id),
            data=(interaction.data or {}).get("custom_id", ""),
            message=getattr(interaction.message, "id", None), handle=interaction))

    print(f"  board   {store.path}")
    if host.claim:
        print(f"  pair    send  /pair {host.claim}  to the bot to claim the "
              f"first seat")
    else:
        print("  pair    the host approves in the chat; `mooting claim` prints a "
              "code for a new room")
    allowed = sorted(host.chats)
    print(f"  chans   {', '.join(allowed) if allowed else 'ANY (use --channel)'}")
    print("  connecting; Ctrl-C to stop")

    try:
        client.run(bot_token, log_handler=None)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        lines = explain_start_failure(exc)
        if lines is None:
            raise
        print("", *[f"  {ln}" for ln in lines], sep="\n", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0
