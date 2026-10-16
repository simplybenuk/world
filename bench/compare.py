"""Compare agent runs on World and EspoCRM for one domain, from the files `python3 -m bench agent` saved.

    python3 -m bench.compare --domain tracker codex claude opencode

Answers are judged again by the current checkers, so a checker fixed after a run
judges every run alike. Only checks that read the answer are repeated: the
records a run left behind are gone, so what was found in them stands.
"""

import argparse
import json
from statistics import median

from . import tracker
from .__main__ import DOMAINS, OUTPUT, table

ANSWER_FAILURE = "The answer does not say the change was not possible."


def rejudge(domain, row):
    """The run's failures under the current checkers."""
    task = next(task for task in domain.TASKS if task.id == row["task"])
    failures = row["failures"]
    if "answer" not in row:  # rebuilt from the log of a batch that died, which kept only the judgment
        return failures
    if any(word in " ".join(failures) for word in ("timed out", "made no requests")):
        return failures
    if task in domain.TASKS[:10]:  # reading, counting, and searching: judged by the answer alone
        return task.check(None, row["answer"])
    if ANSWER_FAILURE in failures or row["task"] in ("locked", "other-project", "assign"):
        return [failure for failure in failures if failure != ANSWER_FAILURE] + tracker._refused(row["answer"])
    return failures


def load(domain, side, label):
    name = f"agent-{domain.WORLD}-{'control-' if side == 'EspoCRM' else ''}{label}"
    rows = json.loads((OUTPUT / f"{name}.json").read_text())
    # A run whose provider never answered, after every attempt was discarded, says nothing about the records.
    rows = [row for row in rows if not (row["calls"] == 0 and row.get("discarded_attempts", 0) >= 6)]
    for row in rows:
        failures = rejudge(domain, row)
        if failures != row["failures"]:
            row["failures_when_run"] = row["failures"]
        row["failures"], row["correct"] = failures, not failures
    return name, rows


def main():
    parser = argparse.ArgumentParser(prog="python3 -m bench.compare", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", choices=list(DOMAINS), default="tracker")
    parser.add_argument("labels", nargs="+")
    args = parser.parse_args()
    domain = DOMAINS[args.domain]
    summary, by_task = [], {}
    for label in args.labels:
        for side in ("World", "EspoCRM"):
            name, rows = load(domain, side, label)
            (OUTPUT / f"{name}-judged.json").write_text(json.dumps(rows, indent=2) + "\n")
            summary.append((label, side, f"{sum(row['correct'] for row in rows)} of {len(rows)}", sum(row["calls"] for row in rows),
                            sum(row["rejected"] for row in rows), f"{sum(row['seconds'] for row in rows):,.0f} s",
                            f"{median(row['bytes_read'] for row in rows) / 1000:,.0f} KB" if all("bytes_read" in row for row in rows) else "not kept",
                            sum(row.get("discarded_attempts", 0) for row in rows)))
            for row in rows:
                cell = by_task.setdefault(row["task"], {}).setdefault((label, side), [0, 0, 0])
                cell[0] += row["correct"]
                cell[1] += row["calls"]
                cell[2] += 1
    print(table(["Agent", "Side", "Correct", "Calls", "Rejected", "Agent time", "Median bytes read", "Discarded attempts"], summary))
    columns = [(label, side) for label in args.labels for side in ("World", "EspoCRM")]
    print()
    print(table(["Task", *(f"{label} {side}" for label, side in columns)], [
        (task, *(f"{cells[column][0]}/{cells[column][2]}, {cells[column][1]} calls" for column in columns)) for task, cells in by_task.items()]))


if __name__ == "__main__":
    main()
