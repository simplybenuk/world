# The parallel line: `experiment/crm-world`

On 3 and 4 October, another agent session took the first experiment in a
different direction on the branch `experiment/crm-world`. It ran its own second
and third experiments, alongside the ones on `main`. It was not merged, because
it rewrote the same engine files in a different way. This page keeps what it
found and the ideas worth taking from it. The branch stays on GitHub as a
reference and its worktree is at `/home/ben/projects/world-crm-experiment`.

Its own write-ups are on the branch:
`docs/second-experiment-findings.md`, `docs/third-experiment-findings.md`, and
`docs/world-format-prototype.md`.

## What it built

- **Definitions as JSON files.** `definitions/team.world.json` and
  `definitions/crm.world.json` describe fields, access policy, constraints,
  stage transitions, and seed records. One engine and one browser client run
  both, with no CRM-specific Python.
- **Declared transitions.** An enum field lists the values each value may move
  to. A direct `lead` to `won` edit was refused with 422. A new opportunity may
  start at any stage; the transitions govern later edits.
- **A headless agent service** (`experiments/agent_world/`), with no pages at
  all. It adds three operations to list, query, get, and history:
  - `select` returns requested fields, fields reached through references, a
    bounded set of related records, and the stages a record may move to next,
    from one snapshot.
  - `facts` returns accepted field changes over time.
  - A transaction applies up to ten versioned updates together or not at all.
- **History as a ledger of facts.** Every accepted field change is stored with
  its commit. Current records are rebuilt from the ledger when the service
  starts.
- **Record counts in the description,** so an agent can choose between reading
  everything at once and targeted queries.
- **Field meanings.** Descriptions say, for example, that an amount is the total
  contract value in minor currency units, and that an interaction's date is
  when it happened, not when it was recorded.
- **Historical privacy.** Reading history needs access to the record now and at
  the time. References and shared provenance that would expose a private record
  are redacted.

## What it found

It compared its World against a conventional create-read-update-delete service
over the same records, with the same agent. It did not compare against an
outside product.

| Fixture | Conventional calls | World calls | Conventional input tokens | World input tokens |
| --- | --- | --- | --- | --- |
| 14 records | 2 | 2 | 19,925 | 20,715 |
| 414 records | 6 | 3 | 59,960 | 30,045 |

These are single paired Codex runs, with provider-reported token counts.

- **Small worlds do not benefit.** At 14 records both agents read everything in
  one call.
- **Joined reads pay off as the world grows.** At 414 records World needed half
  the calls and half the input tokens, because one `select` replaced several
  lookups.
- **Atomic changes prevent partial updates.** When a second update in a group
  was stale or invalid, World changed nothing. The conventional service kept
  the first update.
- **The ledger cost more.** At 414 records its database was 590 KB against
  434 KB, and a task took 57 ms against 26 ms. It replays and scans the ledger
  in Python rather than using indexed queries.
- **Agents fail in ways unrelated to the records.** Two World attempts failed at
  the model provider after their first reads. A history answer gave a display
  name where an ID was expected, because the task did not say which it wanted.
  All failed attempts were kept in its evidence.

## How it compares with `main`

The two lines agree on the main results, reached separately:

- Reads that follow references cut an agent's calls and input by about half
  once a world is past a few dozen records. `main` found the same with `where`
  paths and `include`, against EspoCRM, up to a million records.
- Changes made together should succeed or fail together. `main` has this as
  `POST /api/changes`.
- One engine can run several domains from separate definitions.

They differ here:

| | `experiment/crm-world` | `main` |
| --- | --- | --- |
| Definition | JSON file | Python module holding data |
| Rules | Structured policy objects plus prose for clients | Expressions the engine evaluates and shows |
| History | Ledger of field facts, records rebuilt from it | Event per change with before and after, plus view lines |
| Control | A conventional service written in the branch | EspoCRM, an existing product |
| Largest test | 414 records | 1,000,000 records, 201 types |

## What `main` should take from it

1. **Definitions as data files.** Step 2 of [the plan](plan.md) needs a team to
   change its world while it runs. `main`'s definitions are already plain data
   inside Python modules, apart from seeds with relative dates. The branch shows
   a JSON layout that works for two domains.
2. **Declared transitions.** A board's columns are exactly this. A rule such as
   `not changed('stage') or record.stage in ...` can express it on `main` today,
   but a declared map is clearer to read and lets the description list each
   record's next stages, as `select` did.
3. **Record counts in the description.** Cheap to add, and it helps an agent
   decide between reading everything and searching.
4. **Field meanings and units.** `main`'s CRM gives the amount's unit. The
   branch went further on dates and money, which matters for real data in step 1.
5. **Historical privacy.** `main` lets anyone who can read a record now read its
   whole history. The branch's rule is stricter, and a public demo should decide
   which it wants.

Not taken: the fact ledger. It made history queryable over time but cost more
space and time, and `main`'s history and audit already meet the two aims.

## Services it left running

The branch runs two user services from its worktree, with their own data:

| Service | Port | Data |
| --- | --- | --- |
| `world-crm-experiment.service` | 8788 | `~/.local/share/world-crm-experiment` |
| `world-agent-experiment.service` | 8789 | `~/.local/share/world-agent-experiment` |

`world-experiment.service` on port 8787 runs `main`'s team world from
`/home/ben/projects/world`.
