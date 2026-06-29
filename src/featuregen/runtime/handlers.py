from __future__ import annotations

from featuregen.contracts import Handler


class HandlerRegistry:
    """Name -> step Handler. Re-registering a name is a load-time error (§10)."""

    def __init__(self) -> None:
        self._by_name: dict[str, Handler] = {}

    def register(self, handler: Handler) -> None:
        name = handler.name
        if name in self._by_name:
            raise ValueError(f"handler {name!r} already registered")
        self._by_name[name] = handler

    def get(self, name: str) -> Handler:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no handler registered: {name!r}") from None
