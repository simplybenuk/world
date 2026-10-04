# Third experiment: a headless fact World

This prototype asks whether an agent can use a CRM with fewer logical service
calls and less transferred data when the system exposes bounded relationship
queries, accepted facts with history, and atomic governed transactions.

The reviewers are the people developing the World method. The evidence is a
repeatable comparison against the existing record API, a real model interacting
through structured operations, and scenarios that exercise persistence and
invalid changes. Read the [observed findings](third-experiment-findings.md) for
results and limits.

The source lives in `experiments/agent_world/` in the isolated experiment
checkout. It is a prototype, separate from the existing `world.server` and
browser client. It uses fictional data and disposable SQLite files. It does not
replace either running browser experiment or mutate their records.

## Run the comparison

From the repository root, use Python 3.11 or newer:

```sh
python3 -m experiments.agent_world.benchmark --output artifacts/agent-world/benchmark.json
```

The scripted clients perform the same tasks through the existing `WorldStore`
and the candidate `FactWorldStore`. They compare normalized answers and record
logical service calls, serialized JSON bytes, operation time and database size.
These are scripted client measurements, not model token measurements. They
exclude HTTP, MCP and network overhead.

Both adapters expose the existing `list`, `query`, `get` and `history` reads.
The candidate also exposes `select` and `facts`. A bulk `list` retrieves the
whole authorized current graph in one read. The scripted report includes that
control alongside targeted queries and derived history for both adapters.
Each report freezes its operation catalog as `available-reads-v2`.

An optional larger fixture adds unrelated fictional accounts:

```sh
python3 -m experiments.agent_world.benchmark --distractors 100 --output artifacts/agent-world/scale.json
```

For the actual headless agent, install and authenticate the Codex CLI, then run:

```sh
python3 -m experiments.agent_world.benchmark --live --trials 1 --output artifacts/agent-world/live.json
```

To test the pipeline question with the larger fixture:

```sh
python3 -m experiments.agent_world.benchmark --live --trials 1 --scenario pipeline --distractors 100 --output artifacts/agent-world/live-scale.json
```

The agent receives discovery information, chooses a structured operation, reads
its result, and continues until it answers or proposes a change. The driver
does not execute generated code. Each model invocation has a timeout and the
conversation has a turn limit. Its operations use separate temporary databases;
it cannot edit the installed demos. Actual model usage, where supplied by the
provider, is distinct from JSON byte counts. Provider failures remain evidence.

Discovery includes authorized current record counts. The agent instructions
suggest bulk reads at 50 visible records or fewer, and targeted reads above
that size. Both modes receive this same experimental heuristic. It is not an
optimal query planner. Each model step starts a fresh ephemeral process and
replays discovery and prior results, so the measured tokens depend on this
driver's context handling.

## Run a persistent headless World

```sh
python3 -m experiments.agent_world.server --data-dir .world/agent-world --port 8789
```

In another terminal:

```sh
python3 -m experiments.agent_world.server access --data-dir .world/agent-world --port 8789 --actor ben
```

The access command prints the service URL and a private `Authorization` value.
The credentials and database stay outside Git. Use that value in an agent's
HTTP client. The service root returns a short JSON description. There are no
CRM pages or conversational UI on this service.

`GET /api/world` returns the actor's discovery description. Supplemental field
meanings describe money and dates; the definition and engine enforce actual
types, references, policy and transitions. An agent must convert a human's
question into the structured request contract. The server does not interpret
free-form prose or treat it as a transaction.

The earlier current-record routes remain available. `GET /api/records` returns
all authorized current records, `POST /api/query` filters one declared type,
and `GET /api/records/Type/id` reads one record. Current references to unreadable
records are redacted. `GET /api/history/Type/id` returns ledger-derived events
with versioned before/after records and permitted provenance.

The included HTTP client reads the private credential from the local data
directory without putting it in the command line or a model prompt:

```sh
python3 -m experiments.agent_world.client describe --data-dir .world/agent-world
python3 -m experiments.agent_world.client select --data-dir .world/agent-world
python3 -m experiments.agent_world.client agent --data-dir .world/agent-world --request 'Show Northstar open opportunities with owner names and latest interactions.' --output artifacts/agent-world/http-agent.json
```

The `agent` command uses the actual HTTP service. It can read and propose; it
does not automatically accept changes. An explicit `transact --request-file`
command sends a reviewed transaction to the service.

## Request related data

Send this JSON to `POST /api/select` with the bearer credential:

```json
{
  "type": "Opportunity",
  "filters": [
    {"field": "organization", "op": "eq", "value": "northstar"},
    {"field": "stage", "op": "in", "value": ["lead", "qualified", "proposal"]}
  ],
  "select": ["name", "stage", "amount_minor", "currency", "organization.name", "owner.name"],
  "include": [
    {
      "name": "latest_interaction",
      "type": "Interaction",
      "via": "opportunity",
      "select": ["body", "occurred", "author.name"],
      "order_by": "occurred",
      "descending": true,
      "limit": 1
    }
  ],
  "limit": 20
}
```

The result includes one snapshot number, selected values, bounded related
records and the declared next stage values. The fixture returns Northstar's
renewal and pilot. Atlas is a separate private organization. Sam can read
Northstar but cannot change it or inspect Atlas.

The saved request in `experiments/agent_world/queries.json` includes the contact
name as well. Reference traversal and included records must respect the actor's
permissions. The server supports root-field filters and bounded selections;
it does not expose a general graph language or server-side aggregation.
Selections return at most 100 roots and at most 100 records per included
relationship. This prototype has no pagination or truncation marker. Use
questions whose requested results fit those bounds.

## Propose and accept a change

Reading and generating a proposal leave accepted facts unchanged. To accept an
explicit proposal, send a transaction to `POST /api/transact`:

```json
{
  "operations": [
    {
      "type": "Opportunity",
      "id": "northstar-renewal",
      "changes": {"stage": "proposal"},
      "expected_version": 1
    }
  ],
  "reason": "Prepare the proposal requested in the discovery conversation",
  "evidence": ["Interaction#renewal-discovery"]
}
```

The service checks current authority, versions, field types, relationships and
stage transitions. A transaction can contain up to ten distinct record updates.
It accepts all operations together or rejects all of them. An invalid second
operation must leave the first record, its version and the accepted ledger
unchanged. Creates and deletes are not part of this multi-record grammar.

## Inspect history and rebuild

`GET /api/facts/Opportunity/northstar-renewal` returns accepted field assertions
and retractions with their transaction and provenance. `POST /api/select` can
include `as_of` with a saved snapshot number. After accepting the proposal,
the original snapshot must still show `qualified`, while the latest shows
`proposal`.

Accepted ledger entries are append-only. Current records are a projection that
the store reconstructs on reopen and can rebuild explicitly. This tests a
logical storage model on SQLite; it does not invent or benchmark a new physical
database engine.

Historical reads require present access and access at the historical snapshot.
Revoking access also revokes reads of previous snapshots. Gaining access does
not reveal private-era facts. Ledger time is the time of acceptance, distinct
from a customer interaction's `occurred` date. This prototype does not implement
a full valid-time or bitemporal model.

## Verify and reset

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -W error::ResourceWarning -m unittest discover -s experiments/agent_world -t . -p 'test_*.py' -v
```

The first command protects the earlier experiments. The second exercises the
third prototype's storage and HTTP contract. The benchmark checks its own
scripted answers and failure scenarios.

To start a fresh persistent example, choose a new directory such as
`.world/agent-world-2` in both server commands. Stop a server with Ctrl+C. The
benchmark cleans up only its own temporary databases and subprocesses. Local
JSON transcripts under `artifacts/` are ignored by Git; the findings preserve
the reviewed measurements.

Production deployment, automatic business decisions, a public service, general
query planning, CRM imports, schema migration and a cross-client protocol
standard remain outside this experiment.
