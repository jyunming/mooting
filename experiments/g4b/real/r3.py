# ===== mooting/telegram.py =====
# ...
        return getattr(member, "status", None) == "creator"

    @dp.message(lambda m: bool(getattr(m, "new_chat_members", None)))
    async def on_join(msg: Message):
        """Somebody was added to the group.

        Telegram says who added them, and that is the fact this needs: an invite
        from the host of the room is the host deciding, and anybody else adding
        somebody is not. Without this the bot never saw a person arrive at all,
        so joining did nothing and the newcomer had to know to type `/pair`.
        """
        if not await allowed(msg.chat.id, msg.from_user.id,
                             msg.chat.type == "private"):
            return
        room_id = store.ensure_room("telegram", str(msg.chat.id))
        host = store.room_host(room_id)
        added_by = store.seat_for_chat(msg.chat.id, msg.from_user.id)

        for member in msg.new_chat_members:
            if getattr(member, "is_bot", False):
                continue
            if store.seat_for_chat(msg.chat.id, member.id):
                continue                        # already one of us
            who = member.full_name or str(member.id)
            pid = store.pair_request(msg.chat.id, member.id, who)
            # Owning the group is only ever a confirmation about somebody this
            # board already knows. Taken on its own it is authority anybody can
            # mint: make a group, add this bot, and every person you add is let
            # onto a board that is not yours. `added_by` must be a seat here
            # first -- the Telegram fact then says which seat is the host.
            by_owner = bool(added_by) and await owns_this_group(
                msg.chat.id, msg.from_user.id)
            if added_by and (by_owner or (host and added_by == host)):
                if by_owner:
                    host = store.claim_room(room_id, added_by)
                seat = store.seat_name_for(who, fallback=f"guest{pid}")
                try:
                    row = store.pair_approve(pid, seat, added_by or host or seat)
                except (StoreError, NotAuthorised) as exc:
                    await say(msg.chat.id, str(exc))
                    continue
                await say(msg.chat.id,
                          f"{who} was added by {added_by or 'the group owner'} "
                          f"and speaks as **{row['seat']}**.")
                continue
            await say_join_request(
                msg.chat.id, pid,
                f"{who}" + (f", added by {added_by}" if added_by else ""))

    @dp.message(lambda m: m.document is not None)
    async def on_document(msg: Message):
        """A file sent to the chat becomes an attachment on the topic.

        `/attach <path>` names a file on the machine running the bot, which is
        not the machine you are holding. Sending the document *is* the gesture
        on a phone, so it is the one that works.
        """
        if not await allowed(msg.chat.id, msg.from_user.id,
                             msg.chat.type == "private"):
            return
        seat = store.seat_for_chat(msg.chat.id, msg.from_user.id)
        if not seat:
            return await say(msg.chat.id, "You are not paired here.")
        slug = topic_here(msg.chat.id)
        if not slug:
            return await say(msg.chat.id,
                             "No topic yet — `/topic new <your question>` first.")

        import tempfile

        doc = msg.document
        name = doc.file_name or f"attachment-{doc.file_unique_id}"
        try:
            buf = await bot.download(doc)
        except Exception as exc:
            # Bots can only fetch files up to 20 MB; a bigger one fails here.
            return await say(msg.chat.id, f"Could not fetch that file: {exc}")

        tmp = pathlib.Path(tempfile.mkdtemp()) / name
        tmp.write_bytes(buf.read())
        try:
            aid = store.attach(int(store.topic(slug)["id"]), tmp, seat,
                               note=(msg.caption or "").strip())
        except StoreError as exc:
            return await say(msg.chat.id, str(exc))
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

        row = store.q1("SELECT * FROM attachments WHERE id = ?", (aid,))
        how = ("its text goes into every seat's next prompt" if row["is_text"]
               else "binary — the seats get its name and path, not its contents")
        await say(msg.chat.id,
                  f"Attached **{row['name']}** ({row['bytes']:,} bytes) — {how}.")

    @dp.message(Command("run"))
    async def on_run(msg: Message):
        """Drive the council from the bot's own loop.

        Not through `Console._run`: that starts a daemon thread and returns, and
        `ChatBoard` closes its store the moment the message is handled -- so the
        thread lost the board underneath it and the council died in silence,
# ...
