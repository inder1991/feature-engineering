from __future__ import annotations

from collections.abc import Callable

from featuregen.contracts import Command, CommandResult, DbConn

CommandHandler = Callable[[DbConn, Command], CommandResult]
_REGISTRY: dict[str, CommandHandler] = {}


def register_command(action: str, handler: CommandHandler) -> None:
    if action in _REGISTRY:
        raise ValueError(f"command already registered: {action}")
    _REGISTRY[action] = handler


def get_command(action: str) -> CommandHandler:
    try:
        return _REGISTRY[action]
    except KeyError:
        raise KeyError(f"no handler registered for action: {action}") from None


def clear_registry() -> None:
    _REGISTRY.clear()
