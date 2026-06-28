from __future__ import annotations

from typing import Callable, Dict

from sp0.contracts import Command, CommandResult, DbConn

CommandHandler = Callable[[DbConn, Command], CommandResult]
_REGISTRY: Dict[str, CommandHandler] = {}


def register_command(action: str, handler: CommandHandler) -> None:
    if action in _REGISTRY:
        raise ValueError(f"command already registered: {action}")
    _REGISTRY[action] = handler


def get_command(action: str) -> CommandHandler:
    try:
        return _REGISTRY[action]
    except KeyError:
        raise KeyError(f"no handler registered for action: {action}")


def clear_registry() -> None:
    _REGISTRY.clear()
