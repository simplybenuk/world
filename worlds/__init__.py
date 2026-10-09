"""World definitions. Each module describes one domain for the same engine."""

from importlib import import_module

AVAILABLE = ("team", "crm")


def load(name: str):
    if name not in AVAILABLE:
        raise ValueError(f"Unknown world {name!r}. Choose one of: {', '.join(AVAILABLE)}.")
    return import_module(f"worlds.{name}").DEFINITION
