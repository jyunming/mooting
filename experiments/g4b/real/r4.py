# ===== mooting/console.py =====
# ...
    def _rounds(self, rest: str) -> None:
        """`/rounds 7` means seven rounds. `/rounds +7` means seven more.

        It used to add in both cases, so asking for 7 on a 3-round topic gave 10
        rounds and 13 turns -- two numbers nobody asked for and no way to tell
        where they came from.
        """
        if not self._require_topic():
            return
        arg = rest.strip()
        add = arg.startswith("+")
        digits = arg.lstrip("+")
        if not digits.isdigit():
            t = self.store.topic(self.topic_id)
            self.emit(f"  round {BOLD}{t['round'] + 1}{RESET} of {BOLD}{t['max_rounds']}{RESET}"
                      f"   {DIM}/rounds <n> sets the total · /rounds +<n> adds more{RESET}")
            return

        n = int(digits)
        try:
            if add:
                self.store.grant_rounds(self.topic_id, n, self.me)
            else:
                self.store.set_rounds(self.topic_id, n, self.me)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return

        t = self.store.topic(self.topic_id)
        seats = self.store.seats(self.topic_id)
        turns = min((s["max_turns"] for s in seats), default=t["max_rounds"])
        self.emit(f"{DIM}round {t['round'] + 1} of {t['max_rounds']} — "
                  f"each seat may speak up to {turns} time(s){RESET}")
# ...
# ===== mooting/telegram.py =====
# ...
    async def send_choices(chat_id, which: str, slug: str | None) -> None:
        """The answers to a bare command, as buttons.

        Typing on a phone is the cost this whole surface exists to avoid, and a
        command whose answers are a short fixed list should never ask for them.
        """
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows, head = [], ""
        if which == "effort":
            head = "How long should they think?"
            rows = [[InlineKeyboardButton(text=e, callback_data=set_callback("effort", e))
                     for e in ("low", "medium", "high")]]
        elif which == "rounds":
            head = "How many more rounds?"
            rows = [[InlineKeyboardButton(text=f"+{n}",
                                          callback_data=set_callback("rounds", str(n)))
                     for n in (1, 3, 5)]]
        elif which in {"nudge", "chair"}:
            if not slug:
                return await say(chat_id, "No topic here yet.")
            seats = store.seats(int(store.topic(slug)["id"]))
            want_people = which == "chair"
            names = [r["agent"] for r in seats
                     if (r["kind"] in HUMAN_KINDS) == want_people]
            if not names:
                return await say(chat_id, "Nobody here to choose from.")
            head = ("Who chairs this meeting?" if want_people
                    else "Which seat should wake?")
            rows = [[InlineKeyboardButton(text=n, callback_data=set_callback(which, n))]
                    for n in names]
        try:
            await bot.send_message(chat_id, head,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception as exc:
# ...

        chosen = parse_set(call.data or "")
        if chosen is not None:
            what, value = chosen
            if not await allowed(chat_id):
                return await call.answer("not this chat", show_alert=True)
            seat = store.seat_for_chat(chat_id, call.from_user.id)
            if not seat:
                return await call.answer("You are not paired here.", show_alert=True)
            slug = topic_here(chat_id)
            line = {"effort": f"/effort {value}", "rounds": f"/rounds {value}",
                    "chair": f"/topic chair {value}", "wake": f"/nudge {value}"}[what]
            board = ChatBoard(db, slug, seat, room=("telegram", str(chat_id)))
            try:
                out = board.handle(line)
            finally:
                board.close()
            await call.answer(value)
            return await say(chat_id, out or f"{what} → {value}")

        picked = parse_pick(call.data or "")
# ...
