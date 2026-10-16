"""World definitions. Each module describes one domain for the same engine."""

from importlib import import_module

AVAILABLE = ("team", "crm", "lab", "enterprise", "tracker")


def load(name: str):
    """A world by its module name, or from a JSON file when the name ends in .json."""
    if name.endswith(".json"):
        from world.definition import load_json
        return load_json(name)
    if name not in AVAILABLE:
        raise ValueError(f"Unknown world {name!r}. Choose one of: {', '.join(AVAILABLE)}.")
    return import_module(f"worlds.{name}").DEFINITION
