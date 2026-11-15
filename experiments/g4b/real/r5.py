# ===== mooting/console.py =====
# ...
                landed = f"  {n} commit(s)" if n is not None else ""
                self.emit(f"       {DIM}{t['branch']}{landed}{RESET}")
            if t["result"]:
                self.emit(f"       {t['result'].strip()[:200]}")

    def _me(self, rest: str) -> None:
        """Rename yourself. The council addresses you by this."""
        new = rest.strip().lstrip("@")
        if not new:
            self.emit(f"  you are {BOLD}{self.me}{RESET}   {DIM}/me <name>{RESET}")
            return
        try:
            self.store.rename_agent(self.me, new)
        except StoreError as exc:
            self.emit(f"{RED}{exc}{RESET}")
            return
# ...
                                (int(ref), self.topic_id))
            if row is None:
                self.emit(f"{RED}no message #{ref} on this topic{RESET}")
                return
        else:
            row = self.store.q1(
                "SELECT * FROM messages WHERE topic_id = ? AND author = ? "
                "ORDER BY id DESC LIMIT 1", (self.topic_id, ref.lstrip("@")))
            if row is None:
                self.emit(f"{RED}{ref!r} has not said anything here{RESET}")
                self.emit(f"{DIM}/quote            the last thing anyone said{RESET}")
                self.emit(f"{DIM}/quote <seat>     that seat's latest{RESET}")
                self.emit(f"{DIM}/quote <id>       the dim #n beside a message{RESET}")
# ...
                  f"in its own git worktree{RESET}")

    def _manager(self, rest: str) -> None:
        if not self._require_topic():
            return
        if self.store.topic(self.topic_id)["mode"] != "work":
            self.emit(f"{DIM}no manager in a discussion — roles exist on work topics."
                      f" /topic mode work <agent> to switch.{RESET}")
            return
        if not self.store.seat(self.topic_id, rest):
            self.emit(f"{RED}{rest!r} holds no seat here{RESET}")
            return
        with self.store.tx() as c:
            c.execute("UPDATE seats SET role = 'participant' WHERE topic_id = ?",
                      (self.topic_id,))
            c.execute("UPDATE seats SET role = 'manager' WHERE topic_id = ? AND agent = ?",
                      (self.topic_id, rest))
        self.emit(f"{DIM}{rest} is now the manager{RESET}")
        self.on_topic_change()

    def on_topic_change(self) -> None:
        """Hook for a surface that has to repaint. The REPL has nothing to do."""

    def _nudge(self, agent: str) -> None:
        if not self._require_topic():
            return
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        def work() -> None:
            store = connect(self.db)
            try:
                r = asyncio.run(Supervisor(store, build_drivers(store, [agent]))
                                .wake_seat(self.topic_id, agent))
                if not r.ok:
                    self.emit(f"\n{RED}{agent}: {r.detail}{RESET}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True).start()
        self.emit(f"{DIM}waking {agent}...{RESET}")

    def _decide(self, cmd: str, rest: str) -> None:
# ...
