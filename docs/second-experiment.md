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

Rules are written twice. Each type carries its rules as text for a client to
read, and the definition enforces them in Python. Nothing checks that the two
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

## Open questions

- Creating a record that names a missing organization returns 403, not a
  reference error, because the permission check runs first. Reporting the
  missing reference would also let an actor test whether a hidden record exists.
- A rule's refusal says which rule applied but not which part of it failed.
- Change reasons are still optional over HTTP. An agent that omits one gets a
  generic default in the history.

## Next

1. Run the same five tasks, with the same agent, against an existing open-source
   CRM through its own API. Record its installed size alone and with everything
   it needs to run. That is the control this experiment lacks.
2. Reduce the description. Candidates are sending only the types a request
   names, shortening field definitions, and replacing rule text with rules the
   engine evaluates.
3. Repeat each agent task several times, and with a second agent, so the
   success rate means something.
