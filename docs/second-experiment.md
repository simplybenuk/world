# Second experiment: a CRM on the same engine, and what an agent pays to use it

The first experiment left two questions. Can the engine run a second domain
without being rewritten? And what does an agent with no documentation pay to use
a world? This experiment answers both with a small CRM and a set of
measurements.

## Run it

Serve the CRM in its own data directory, then print a sign-in link:

```sh
python3 -m world.server --world crm --data-dir /tmp/world-crm --port 8788
python3 -m world.server access --world crm --data-dir /tmp/world-crm --port 8788 --actor priya
```

The actors are `ben` (admin), `priya` and `tom` (reps), and `nina` (viewer). A
database belongs to one world. The server refuses to open a team database as a
CRM, or the reverse.

Run the measurements that need no model:

```sh
python3 -m bench
```

Run the same tasks with an agent. This uses the agent's model account. The
default agent is `codex exec`:

```sh
python3 -m bench agent
python3 -m bench agent --command 'your-agent --flags' --label your-agent
```

The agent receives a URL, a token, and one task in plain words. Each task gets a
freshly seeded world in a temporary directory. Results go in `artifacts/bench`.

## What the engine reuses

A world is now a definition in `worlds/`. The engine in `world/` holds no type
names, roles, or rules from either domain.

| Part | Lines | Shared |
| --- | --- | --- |
| Storage, versions, history, queries (`world/store.py`) | 566 | yes |
| Field validation (`world/schema.py`) | 125 | yes |
| HTTP protocol (`world/server.py`) | 325 | yes |
| Team definition (`worlds/team.py`) | 272 | no |
| CRM definition (`worlds/crm.py`) | 328 | no |

A definition supplies its types, its seed records, and a few rule functions:
who can read and update a record, who can create and delete, which fields an
actor may not change, and which states are valid.

The CRM needed three changes to shared code:

- An `integer` field type, for an opportunity's amount.
- A `writable: false` marker on fields the current actor cannot change. The
  browser client used to work this out by matching the text of a team rule.
- A plural rule in the client, which labelled the type "Opportunitys".

The browser client then showed the CRM with no other changes. A rep sees their
own pipeline and gets a disabled owner field in the edit form.

## What it shows about a World format

Rules were written twice. This is now fixed. See [Write a world](writing-a-world.md).
Each type carried its rules as text for a client to
read, and the definition enforced them in Python. Nothing checks that the two
agree. The CRM states 34 rules and constraints as text and has about 150
lines of code behind them.

Constraints that span two records are written from both sides. "A contact
belongs to the same organization as their opportunity" is checked when the
opportunity changes and again when the contact moves. A declared constraint
could be enforced from either side by the engine.

Both point at the same next step for the format: rules the engine can evaluate
and describe from one definition.

## Measurements

All figures are for the seeded CRM of 26 records.

### Size

| Part | Bytes |
| --- | --- |
| Engine and HTTP protocol | 54,267 |
| CRM definition | 19,941 |
| Seeded database with history | 57,344 |

The service needs Python 3.11 or newer and nothing else. The reference client
is 112 KB and is not part of World. There is no control figure yet. See
"Next" below.

### Discovery cost

`GET /api/world` returns about 7.2 KB for every actor, roughly 1,800 tokens at
four bytes per token. That one response is 84% to 94% of everything a scripted
task reads. It is the number to bring down.

### Round trips per task

The scripted column is the fewest calls a client that already knows the protocol
can make. The agent columns are one run each of `codex exec` with no
documentation, before and after the protocol change described below.

| Task | Scripted | Agent before | Rejected | Agent after | Rejected |
| --- | --- | --- | --- | --- | --- |
| Total my open pipeline | 2 | 5 | 3 | 2 | 0 |
| Mark a renewal won and note why | 4 | 13 | 9 | 4 | 0 |
| Mark a pilot lost | 3 | 11 | 8 | 3 | 0 |
| Add a contact and make them primary | 4 | 11 | 7 | 6 | 0 |
| Refuse a change to a closed opportunity | 2 | 1 | 0 | 2 | 0 |
| Total | 15 | 41 | 27 | 17 | 0 |

### Cold success rate

Before the change the agent got 4 of 5 tasks right in 281 seconds. On the fifth
it stopped after one call and gave no answer. After the change it got 5 of 5 in
143 seconds. These are single runs, so treat the success rate as an indication.
The call counts are the firmer result.

### Recovery from a rejected request

Ten plausible mistakes, each followed by a mechanical repair that uses the
error's details and falls back to extra lookups. Before the change 3 of 10 could
be corrected in one call. After it, 8 of 10. Of the other two, one is a
permission refusal that has no correct retry. The other is described below.

## The protocol change the measurements prompted

The first agent run showed where the calls went. The description listed every
type and rule but not how to reach them, so the agent guessed paths such as
`/api/records/Opportunity` and `/api/update`, and then guessed the shape of an
update body. It also left the reason for a change blank in three tasks, and the
server filled in a default.

Two changes followed:

- `GET /api/world` now includes an `operations` list with each path and body
  shape, including `reason`. It adds about 530 bytes.
- Rejections now carry what the next request needs: the current version, the
  allowed fields or types, the missing field's definition, the filter keys, or
  the rule that refused the change.

The second run made no rejected calls and gave its own reason for every change.

## The control: EspoCRM

The control is EspoCRM 10.0.9 with MariaDB, started from
`bench/control/compose.yaml`. It holds the same people, organizations, contacts,
opportunities, and notes as the CRM world, loaded from the same seed. Priya, Tom,
and Nina are API users with roles that match the world's rules. An
administrator's before-save script adds the one rule EspoCRM does not ship: only
an administrator can change a closed opportunity.

```sh
docker compose -f bench/control/compose.yaml up -d
python3 -m bench control
```

Both sides get the same five prompts and the same agent:

```sh
python3 -m bench agent --repeat 3
python3 -m bench agent --control --repeat 3
```

### The agent comparison

Run on 2026-10-12 with `codex exec`, three runs of each task on each side.

| | World | EspoCRM |
| --- | --- | --- |
| Task runs correct | 15 of 15 | 15 of 15 |
| Calls | 51 | 50 |
| Rejected calls | 0 | 3 |
| Agent time, all 15 runs | 281 s | 282 s |
| Bytes read, median run | 8,058 | 3,289 |
| Bytes read, largest run | 8,699 | 747,729 |

On success, calls, and time the two are level. The agent's own thinking takes
12 to 30 seconds a task, so a server that answers in 5 ms and one that answers
in 50 ms look the same.

The agent already knew EspoCRM. It went straight to the right paths, filters,
and field names without reading any description, which is why its usual run
reads less than half of what a World run reads. World was new to it, and it
matched that from a 6.8 KB description. When the agent was unsure about
EspoCRM once, it fetched the 248 KB metadata three times in one task.

All three rejected calls on EspoCRM are the closed opportunity. The agent tried
the change and was refused. On World it read that the record could not be
updated and did not try.

This control is the easy case for the other side. A widely used product with a
public API is in the model's training. A company's own system is not. The next
comparison should give EspoCRM a custom entity and custom fields that the agent
cannot know, and give World the same.

### Size

| | World | EspoCRM |
| --- | --- | --- |
| The application itself | 81 KB of source | 42.6 MB release archive, 172 MB installed |
| With what it needs to run | 66 MB with Python and its standard library | 1.53 GB as two container images |
| Idle memory | 29 MB | 218 MB across the two containers |
| Seeded data on disk | 57 KB | 200 MB database directory |

The EspoCRM images include an operating system, and its database directory is
mostly MariaDB's own files at this size. World's figures are for this machine's
Python install with no container. Read the last two rows of each column as what
you would carry, not as a like-for-like count of application bytes.

### Self-description

World describes itself in 6.8 KB. EspoCRM's equivalent, `GET /api/v1/Metadata`,
is 248 KB for a rep, roughly 62,000 tokens. An agent that already knows
EspoCRM's API from training does not need it. An agent that does not has no
smaller place to look.

### Scripted tasks

A client that already knows each API, with no model involved:

| Task | World calls | World ms | EspoCRM calls | EspoCRM ms |
| --- | --- | --- | --- | --- |
| Total my open pipeline | 2 | 12 | 1 | 20 |
| Mark a renewal won and note why | 4 | 25 | 3 | 106 |
| Mark a pilot lost | 3 | 6 | 2 | 59 |
| Add a contact and make them primary | 4 | 25 | 3 | 122 |
| Refuse a change to a closed opportunity | 2 | 4 | 2 | 45 |

World makes one more call per task because its script reads the description
first. EspoCRM's script skips discovery. Its times include a local counting
proxy, which adds a few milliseconds.

### The same ten mistakes

World rejects all ten and gives enough to correct eight in one call. EspoCRM
rejects six. It names the field in its validation errors but not the allowed
values, and it puts the explanation in an `X-Status-Reason` header.

| Mistake | World | EspoCRM |
| --- | --- | --- |
| Update with an out-of-date version | 409, gives the current version | 409, gives the current values |
| Update without a version | 428, gives the current version | Saved. The last write wins |
| Stage written as closed-won | 422, lists the stages | 400, names the field |
| Won without an amount or close date | 422, names the fields | Saved as won with an amount of zero |
| Amount written as value | 422, lists the fields | 200, and the amount did not change |
| Interaction without its date | 422, gives the field's definition | 400, names the field |
| Organization given by a guessed ID | 403, no detail | 403, says the record does not exist |
| Opportunity called Deal | 400, lists the types | 404, says the type does not exist |
| Filter key written as operator | 400, lists the keys | Accepted. It reads the other key name too |
| Change to a closed opportunity | 403, gives the rule | 403, gives the rule's message |

Two of EspoCRM's four acceptances leave a wrong result with a success status.
The unknown field is the one an agent is least likely to notice. The won
opportunity at zero is a rule EspoCRM does not have, not a fault in it. EspoCRM
explains the guessed ID better than World does.

## Changes since the first measurements

- Responses are compact JSON. The description fell from 7.3 KB to 6.8 KB with
  no change in content. The agent figures above were measured before this.
- A person can issue a token to an agent with
  `python3 -m world.server access --actor priya --agent codex`. The audit then
  records that token's reads and changes as `priya via codex`. The agent runner
  uses such a token and saves each run's audit lines with its results.

## Open questions

- Creating a record that names a missing organization returns 403, not a
  reference error, because the permission check runs first. Reporting the
  missing reference would also let an actor test whether a hidden record exists.
- A rule's refusal says which rule applied but not which part of it failed.
- Change reasons are still optional over HTTP. An agent that omits one gets a
  generic default in the history.

## Next

1. Done. See [the third experiment](third-experiment.md), where the agent meets
   a domain it cannot know.
2. Reduce the description further. Rule text is 23% of it and field
   definitions 38%. Splitting it across calls would save bytes and cost a round
   trip, and a round trip costs an agent more. Any cut that changes content
   needs an agent run to show the success rate holds.
3. Repeat each agent task several times, and with a second agent, so the
   success rate means something.
