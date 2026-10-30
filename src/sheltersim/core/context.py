"""A context manager for one ContextVar, cheap enough to enter several times per tick."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


class ContextScope:
    """Set the variable on entry and restore it on exit.

    A generator-based context manager costs about ten times as much, which shows
    when each tick phase enters several scopes.
    """

    __slots__ = ("_token", "_value", "_var")

    def __init__(self, var: ContextVar[Any], value: Any) -> None:
        self._var = var
        self._value = value
        self._token: Token[Any] | None = None

    def __enter__(self) -> None:
        self._token = self._var.set(self._value)

    def __exit__(self, *_exc: object) -> None:
        self._var.reset(self._token)
