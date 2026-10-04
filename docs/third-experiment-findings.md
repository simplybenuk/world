# Third experiment findings

World now has a headless CRM whose primary client is an agent. The
[run guide](third-experiment.md) describes discovery, accepted facts,
relationship selections and governed transactions.

The useful result is conditional. Both real agents answered the tiny CRM
question with discovery and one bulk read. In a completed larger comparison,
World's joined read reduced calls and model input by about half. Atomic changes
also prevented partial updates. The fact-store implementation used more space
and processing time, so this experiment does not demonstrate a better physical
database.

Verified on 2026-10-04. The [portable evidence](evidence/third-experiment.json)
contains operation catalogs, measured counts, failed attempts and hashes of
local raw artifacts. The [observed answer](evidence/third-experiment-answer.json)
contains fictional records only.

## What runs

The service root returns a short JSON description. Agents use authenticated
operations; the person's interaction happens in their agent client. The service
has no CRM pages or generated layouts.

`FactWorldStore` keeps accepted field assertions and retractions with immutable
commit metadata. Current records are a projection rebuilt from that ledger on
startup. The store reuses the domain definition, typed fields, permissions,
references and transitions from experiment two. Fresh candidate databases have
no duplicate legacy audit-event table. SQLite remains the physical engine.

A selection returns requested fields, reference paths, bounded reverse
relationships and permitted next stages from one snapshot. A transaction
validates up to ten distinct versioned updates against the final graph, then
accepts all or none. Historical reads require current and historical access.
Unreadable references and shared provenance that could expose private records
are redacted.

Field descriptions explain the fictional monetary value and dates. Amounts are
total proposed contract value in minor currency units, paired with a currency.
An interaction's occurrence date differs from its acceptance time. These
meanings supplement the enforceable schema and rules.

## A fair read comparison

Both adapters expose the existing `list`, `query`, `get` and `history` reads.
World adds `select` and `facts`. The current reports freeze these contracts as
`available-reads-v2`. The bulk control reads the complete authorized graph in
one consistent transaction; targeted clients cache references and batch needed
IDs into filtered queries.

Discovery reports authorized current record counts. Both agents receive the
same experimental suggestion to consider bulk reads at 50 visible records or
fewer and targeted reads above that size. This is a heuristic, not an optimal
planner. The fixtures contain 14 and 414 records, of which Maya can see 10 and
410 respectively.

The scripted clients produced exact answers through both strategies. Calls and
response bytes include discovery. These measurements count logical in-process
operations and compact JSON, excluding HTTP and MCP overhead.

| Fixture and pipeline strategy | CRUD calls | World calls | CRUD response bytes | World response bytes |
| --- | ---: | ---: | ---: | ---: |
| 14 records, targeted | 6 | 3 | 13,733 | 12,955 |
| 14 records, bulk | 2 | 2 | 14,222 | 14,944 |
| 414 records, targeted | 6 | 3 | 13,742 | 12,964 |
| 414 records, bulk | 2 | 2 | 161,981 | 162,703 |

Targeted task responses alone were 2,719 versus 1,219 bytes. Bulk task responses
were 3,208 bytes in the small fixture and 150,958 in the larger one for both
adapters. Discovery accounted for most targeted response data and was slightly
larger for World. Bulk minimizes calls here; joined selection keeps irrelevant
records out of the answer context as the fixture grows.

The optimized simple history scenario used discovery and derived history in
both adapters, two calls each. The small-fixture responses were 12,579 versus
13,289 bytes. The candidate enforces a stricter historical privacy contract,
so equal answers do not imply identical authorization behavior.

Stale or invalid second updates left every candidate record, version and
accepted fact unchanged. Sequential CRUD retained its first successful update.
Those atomicity tasks are marked incorrect for the baseline, while its expected
partial-commit behavior passes the behavioral check. A conventional service
could add atomic batch updates too. A coordinated four-record customer move
also succeeded as one accepted commit; incomplete moves failed validation.

## Actual agent results

The bounded Codex agent received discovery, chose structured reads and returned
an answer or proposal. It had no shell, browser or generated-code execution.
The fixture oracle independently checked exact answers. Proposals required a
successful permitted current-state read, an observed version and update
authority.

The completed current-catalog comparisons were:

| Pipeline fixture | CRUD calls | World calls | CRUD input tokens | World input tokens |
| --- | ---: | ---: | ---: | ---: |
| 14 records, both exact answers correct | 2 | 2 | 19,925 | 20,715 |
| 414 records, completed repeat, both correct | 6 | 3 | 59,960 | 30,045 |

Both small-fixture agents chose `list`. In the larger completed comparison,
CRUD used targeted queries; World looked up the organization and selected its
opportunities with related fields. The larger World task responses were 1,439
bytes versus 2,719 for CRUD. Its larger discovery payload makes the reduction
in total response bytes smaller than the token reduction.

These are provider-reported tokens from one paired trial per artifact. Each
step starts a fresh ephemeral process and replays discovery plus prior results.
Call count therefore affects this driver's accumulated input cost. A persistent
agent client may have different context and caching behavior. Both completed
current-catalog comparisons reported zero cached input tokens. The CLI version
was 0.159.2 with the same default invocation in both modes. The backend model
identifier was not reported or independently verified.

The first larger attempt completed for CRUD with six calls and 60,025 input
tokens. World failed at provider generation after discovery and an organization
read. Its partial usage is not a completed task cost. A current-catalog HTTP
attempt also failed at generation immediately after discovery. Both failed
artifacts remain in the evidence; the completed comparisons above are separate
follow-up attempts.

The subsequent HTTP run returned the exact pipeline answer with discovery and
one bulk read, two calls and 20,719 input tokens. Its bearer credential stayed in
the HTTP client and was absent from the model payload and result artifacts.
It performed no accepted changes.

Earlier runs omitted bulk reads from both agent catalogs and derived history
from World's catalog. Those restricted experiments are retained separately in
the evidence, including accepted proposals on disposable fixtures. Their costs
do not establish the best available baseline strategy. The first history answer
also returned display name `Maya` where the oracle expected actor ID `maya`.
The task did not specify that representation clearly. After clarification, both
history answers passed in a separate run. That original failed result remains.

## Storage and execution costs

| Fixture, targeted pipeline | CRUD database bytes | World database bytes | CRUD time | World time |
| --- | ---: | ---: | ---: | ---: |
| 14 records | 45,056 | 49,152 | 4.7 ms | 8.0 ms |
| 414 records | 434,176 | 589,824 | 26.1 ms | 56.8 ms |

These are single-run operation timings and observed database plus extant WAL
and SHM sizes. Setup is excluded. The candidate replays and scans its ledger in
Python rather than compiling selections into indexed projection queries.
Larger-fixture history was also slower. These results concern this
implementation and do not compare optimized physical database engines.

## Verification and next decision

The earlier 87 Python tests and the prototype's 42 tests pass with resource
warnings treated as errors. Independent review covered catalog parity, HTTP
reads, temporal permissions, reference and provenance redaction, observed
proposals, atomic rollback, projection recovery and rejected-database startup
preflight. Rejected conventional or mismatched databases retain their original
schema and contents.

The private `world-agent-experiment.service` runs on port 8789. A final process
restart preserved the accepted ledger hash, 107 fact rows, one commit and the
renewal at `qualified`, version 1. The two earlier services retain their
processes and data. Credentials and persistent data stay outside Git.
Benchmark temporary databases and model subprocesses were cleaned up. The
review service and local evidence remain available.

The experiments remain isolated on the `experiment/crm-world` branch.
Production promotion was not performed.

Retain joined selections and atomic governed changes for the next design.
Measure selective discovery and indexed projection reads next. The database
question remains open: this prototype proves a usable agent contract and
rebuildable accepted history, while its physical representation still costs
more than the record baseline.
