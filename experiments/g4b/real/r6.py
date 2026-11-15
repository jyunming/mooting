# ===== mooting/telegram.py =====
# ...

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.S)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

#: Terminal colour codes, which mean nothing in a chat.
ANSI = re.compile(r"\[[0-9;]*m")


def _inline(text: str) -> str:
    """Inline markdown to Telegram HTML, on already-escaped text."""
    # `code` first: whatever is inside it must not be read as emphasis.
    holes: list[str] = []

    def stash(m):
        holes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(holes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![A-Za-z0-9])__(.+?)__(?![A-Za-z0-9])", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?![_\w])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], text)


def blocks(md: str) -> list[str]:
    """Markdown into Telegram-HTML blocks, each independently sendable.

    Blocks rather than one string because splitting happens between them: a
    chunk that ends inside `<pre>` is a message Telegram refuses, and finding
    that out in production is expensive.
    """
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(md):
        out += _prose(md[pos:m.start()])
        lang = f' class="language-{m.group(1)}"' if m.group(1) else ""
        out.append(f"<pre><code{lang}>{html.escape(m.group(2))}</code></pre>")
        pos = m.end()
    out += _prose(md[pos:])
    return [b for b in out if b.strip()]


def _prose(md: str) -> list[str]:
    """Everything that is not a fenced block, paragraph by paragraph."""
    out: list[str] = []
    table: list[str] = []

    def flush_table():
        if table:
            # There are no tables in Telegram. Monospace keeps the columns
            # aligned, which is the part that carried the meaning.
            out.append("<pre>" + html.escape("\n".join(table)) + "</pre>")
            table.clear()

    for para in re.split(r"\n\s*\n", md):
        if not para.strip():
            continue
        lines = para.splitlines()
        if all(_TABLE_ROW.match(ln) for ln in lines if ln.strip()):
            table.extend(lines)
            flush_table()
            continue
        flush_table()

        rendered = []
        for ln in lines:
            esc = html.escape(ln)
            heading = re.match(r"^\s*(#{1,6})\s+(.*)$", esc)
            if heading:
                rendered.append(f"<b>{_inline(heading.group(2))}</b>")
                continue
            quote = re.match(r"^\s*&gt;\s?(.*)$", esc)
            if quote:
                rendered.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
                continue
            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", esc)
            if bullet:
                rendered.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
                continue
            rendered.append(_inline(esc))
        out.append("\n".join(rendered))
    flush_table()
# ...
    async def say(chat_id, markdown: str) -> None:
        t = throttles.setdefault(str(chat_id), Throttle())
        for piece in chunks(markdown):
            await t.wait()
            try:
                await bot.send_message(chat_id, piece, parse_mode=ParseMode.HTML)
            except Exception as exc:
                # A reply that cannot be formatted must still arrive. Losing a
                # seat's argument to one stray character is the worst outcome.
                await bot.send_message(chat_id, plain(piece))
                log.warning("fell back to plain text: %s", exc)

# ...
        async def drive():
            try:
                if t["status"] == "paused":
                    store.set_topic_status(tid, "open", seat, "resumed from chat")
                reason = await sup.run_topic(tid)
                await say(msg.chat.id, f"_council stopped: {reason}_")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("council on %s failed", slug)
                await say(msg.chat.id, f"council failed: {exc}")

        running[tid] = asyncio.create_task(drive())
# ...
# ===== mooting/supervisor.py =====
# ...
        # A question waits for its answer. If a human was asked, nobody else
        # speaks until they reply -- the alternative is a room that talks over the
        # person it just asked, and by the time they answer the debate has moved
        # on without the fact only they had.
        for m in self.store.open_mentions(topic_id):
            if self.store.is_human(m["target"]):
                return f"{m['asker']} is waiting on you: {m['question'].strip()[:200]}"
        if not self._eligible(topic_id):
            return self._human_ask_reason(topic_id)
        return None

    def _human_ask_reason(self, topic_id: int) -> str | None:
        """Why the council stopped, when what it is missing is *you*.

        Once nobody else can proceed, an unanswered question addressed to a human
        IS the reason the topic stopped. Reporting "rounds exhausted" instead
        would bury the one thing that needs a person, which is the failure this
        whole feature exists to prevent.
        """
        for m in self.store.open_mentions(topic_id):
            if self.store.is_human(m["target"]):
                return f"{m['asker']} is waiting on you: {m['question'].strip()[:200]}"
        return None

# ...
