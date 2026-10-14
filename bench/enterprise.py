"""The lab's tasks, run in the company-sized world among a million other records.

Build the database first with `python3 -m bench.scale build`. Each task works on its own copy.
"""

from . import lab
from .scale import HOME

WORLD = "enterprise"
SOURCE = HOME / "world.sqlite3"
SEEDED = lab.SEEDED  # the lab's checkers read the seeded IDs from the lab module
FAULTS = []
TASKS = lab.TASKS
