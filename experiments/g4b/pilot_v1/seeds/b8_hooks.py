import logging

log = logging.getLogger("hooks")


class Bus:
    """Synchronous publish/subscribe for plugin events."""

    def __init__(self) -> None:
        self._subs: dict[str, list] = {}

    def subscribe(self, event: str, fn) -> None:
        self._subs.setdefault(event, []).append(fn)

    def publish(self, event: str, payload: dict) -> int:
        """Call every subscriber; one that raises is logged and skipped."""
        called = 0
        for fn in list(self._subs.get(event, [])):
            try:
                fn(payload)
                called += 1
            except Exception:
                log.exception("subscriber failed on %s", event)
        return called


def register_plugins(bus: Bus, plugins: list) -> None:
    """Subscribe each plugin's `handle` to each event it declares, with its config."""
    for plugin in plugins:
        for event in plugin.events:
            bus.subscribe(event, lambda payload: plugin.handle(event, payload, plugin.config))
