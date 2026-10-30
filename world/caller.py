"""Trusted request-local caller context. Remote callers cannot set these values in record data."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Caller:
    agent: str | None = None
    identity: str | None = None

    def field(self, name):
        if name not in {"agent", "identity"}:
            raise ValueError(f"caller has no field {name}.")
        return getattr(self, name)


_current = ContextVar("world_caller", default=Caller())
_work = ContextVar("world_rule_work", default=None)


def current():
    return _current.get()


def identity(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value)):
        raise ValueError("identity is up to 64 letters, digits, dots, hyphens, or underscores.")
    return value


def bind(*, agent=None, identity=None):
    _current.set(Caller(agent=agent, identity=identity))


@contextmanager
def as_caller(*, agent=None, identity=None, rule_steps=None):
    token = _current.set(Caller(agent=agent, identity=identity))
    work_token = _work.set([rule_steps] if rule_steps is not None else None)
    try:
        yield _current.get()
    finally:
        _work.reset(work_token)
        _current.reset(token)


def spend():
    remaining = _work.get()
    if remaining is not None:
        remaining[0] -= 1
        if remaining[0] < 0:
            from .schema import WorldError
            raise WorldError(429, "work_limit", "This request exceeded its rule evaluation budget. Use a narrower query or fewer changes.")
