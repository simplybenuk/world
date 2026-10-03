# First experiment: a persistent world and a temporary client

The question was whether one discoverable record service could support a client
that changes with a person's request, while preserving records, permissions, and
history across those changes.

The experiment uses a small team with `Person`, `Project`, `Task`, and `Comment`
records. [The run guide](first-experiment.md) gives the commands and requests for
reproducing it with a fresh database.

## Observations

The browser discovers the schema and the signed-in actor's visible records.
Codex selects record IDs, fields, and a layout from that context. Four consecutive
requests produced an attention list, a board of the same work, a board focused on
another project, and a proposed task edit.

Generating the edit did not change the database. Accepting it saved the new
status and an audit event with the actor, reason, and previous and resulting
states. The accepted state survived a server restart. A fresh browser started
with a fresh view and conversation, while still reading the saved records.

The service enforced access independently of the generated plan. A viewer could
not write or see the private project. A stale edit was rejected rather than
overwriting a newer change.

## Evidence

- The standard-library suite passed 50 tests covering the store, HTTP protocol,
  and plan validation.
- The browser suite passed with fixture plans and with the real Codex provider.
  It exercised the four requests, acceptance, history, generic forms, conflicting
  edits, restart persistence, a fresh client, and viewer access.
- The server was installed as a user systemd service and checked through its
  trusted network address. Verification used separate disposable databases.

Run the commands in the guide to repeat the checks. Model wording and record
selection can vary; the assertions concern the underlying behavior.

## What this establishes

This demonstrates a separation between persistent governed records and a
temporary client view for one domain. The browser already renders forms and
record views from schema information rather than from a task-specific page.

It does not yet establish a reusable World format. The engine's type definitions,
permissions, and relationship checks contain team-specific code. The client has
four available layouts rather than an unrestricted interface generator. AI
generation requires the server's authenticated Codex CLI and sends visible
records to the model provider.

## Second experiment

Describe a small CRM as a separate World definition and run it through the same
engine and client. Use organizations, contacts, opportunities, and interactions,
with access rules and at least one business constraint. Record which parts can
be reused and which require changes. That comparison should inform the next
version of the format before a broader framework is proposed.
