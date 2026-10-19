# Changing a world while it runs

A design for step 2, item 3 of [the plan](plan.md). It is a proposal for Ben
to decide on. Nothing here is built yet. Claude, 2026-10-19.

## Why

A world's definition is read once, when the server starts. No agent can add a
type or a field, and teamworld has no page for it either. The only way to
change a team's world is to edit its JSON file on the host and restart it.

Teamworld's first real use hit this on its first day. Ben asked his agent to
keep LinkedIn post ideas as their own type, linked to the tasks they came from.
The agent could only answer that it could not change the schema, and it
offered a project of tasks as a stand-in.

[The sixth experiment](sixth-experiment.md) showed that an agent can write a
working world from a plain-language brief. Teamworld's premise is that a
team's agent reshapes the starting world into the one the team needs. That
needs the agent to be able to change the definition, under the world's rules,
with the change in the audit.

## What it must do

1. **Change through the protocol.** An agent changes the definition with an MCP
   tool, as it changes a record. No restart, no file, no host access.
2. **Be governed by the world.** The definition says who may change it, in the
   rule language, as it says who may change a record.
3. **Keep every record valid.** After a change, every live record satisfies
   the new definition, or the change is refused and nothing happens.
4. **Be in the audit.** The audit shows when the definition changed, who
   changed it, through which agent, and what changed. Every record whose stored
   data changed as a result gets its own line in its history, as for any
   other change.
5. **Explain a refusal.** A refused change says which records stand in the
   way and why, so the agent can fix them and try again.

## The design in brief

- The database holds the definition, with every past revision. The file given
  at start-up only creates the world.
- A new `define` rule in the definition says who may change it.
- A new MCP tool and HTTP endpoint, `define`, replaces or removes whole types
  and can rename fields and enum values. It can check a change without making
  it.
- The engine applies the change in one transaction. It rewrites the records
  that need it, re-checks every record of a changed type, and refuses the
  whole change if any would be invalid.
- Each change is one `changed definition` line in the audit, plus an
  `update` event for each record it rewrote.

## Who may change a definition

The definition gains a top-level `rules` object with one rule, `define`. The
rule is judged with `actor` only, since there is no record:

```json
{"name": "team", "title": "Acme", "version": "1",
 "rules": {"define": "actor.role == 'admin'"},
 "types": {...}}
```

Without the rule, no one may change the definition at runtime, so every
existing world keeps working as it does today. `describe` reports the rule and
whether this actor passes it, as it does for a type's operations:

```json
"definition": {"revision": 4, "capabilities": {"define": true, "rules": {"define": "actor.role == 'admin'"}}}
```

A change must not lock out the person making it. The engine judges the
`define` rule again under the new definition and the migrated records, and
refuses a change after which this actor could no longer change the
definition. That is the same check `update` makes after an update. The team
world's existing "keep at least one admin" constraint then also keeps someone
able to change the definition.

## The `define` tool

```json
{"types": {"PostIdea": {...a whole type...}, "Task": {...the whole new Task...}, "Draft": null},
 "rename": {"Task.assignee": "owner"},
 "values": {"Task.status": {"todo": "backlog"}},
 "expected_revision": 4,
 "reason": "Keep LinkedIn post ideas as their own type.",
 "check": false}
```

| Key | Meaning |
| --- | --- |
| `types` | Each named type is replaced by the definition given, or added if it is new. `null` removes it. Types not named stay as they are |
| `rename` | Fields renamed in place, as `Type.old` to `new`, so their values carry over. Without it, a renamed field is a removed field and a new one |
| `values` | Enum values renamed or merged, per field. Records holding an old value get the new one |
| `expected_revision` | The revision the change was written against. A stale one is refused, as `expected_version` is for records |
| `reason` | Why, for the audit |
| `check` | When true, report what the change would do and change nothing |

Whole types, not a patch language, because agents already write whole types
well. That is what the sixth experiment measured, and `describe` hands them
each type in the same shape. A world with 200 types does not need to be sent
whole to change one.

The answer, for a check or a change, says what happens:

```json
{"revision": 5, "summary": "Added type PostIdea. Task: added field idea, renamed assignee to owner, renamed status value todo to backlog.",
 "records": {"Task": {"rewritten": 41, "checked": 112}}}
```

A refusal uses the errors agents already know, with the records in the way,
at most 20 of them:

```json
{"code": "definition_refused", "message": "3 Task records have no value for the new required field due.",
 "details": {"field": "Task.due", "records": ["task-1f…", "task-9c…", "task-a2…"], "more": 0}}
```

The same goes over HTTP as `POST /api/definition`. `GET /api/definition`
returns the current definition and its revision.

## What a change may do to records

The engine sorts each change by what it does to the records already saved.

| Change | What happens to saved records |
| --- | --- |
| Add a type | Nothing |
| Change rules, a field's `write_rule`, a description, or a constraint's message | Nothing. The new rules apply from the next request |
| Add an optional or nullable field | Nothing. Rules read a missing field as `None`, as they do today |
| Add a required field with a default | Every live record gets the default, and is rewritten |
| Add a required field without a default | Refused while any live record of the type exists |
| Remove a field | Its value is taken out of every live record, which is rewritten. History keeps the old values |
| Rename a field with `rename` | Every live record holding it is rewritten |
| Rename an enum value with `values` | Every live record holding it is rewritten |
| Remove an enum value without `values` | Refused while a live record holds it |
| Narrow a field: lower a `max_length`, raise a `min`, change its type, make it required | Allowed if every live record still validates. Otherwise refused, naming them |
| Add or change a constraint | Allowed if every live record of the type, and every record that refers to one, satisfies it. Otherwise refused, naming them |
| Remove a type | Refused while it has live records, or while another type refers to it |
| Rename a type | Not offered in the first version. Records, history, and references are keyed by type name |

Filling in a value the engine cannot know, such as a due date for 40 existing
tasks, is the agent's job, not the definition's. The agent adds the field as
optional, fills it in with `change`, so every value is in the audit as its own
decision, and then makes it required. A migration language would put those
decisions out of sight, so there is none.

Deleted records keep the shape they had. `history` already returns stored
states as they were.

## How the engine applies a change

All in one write transaction, so that a change either happens whole or not at
all, and no writer sees half of it:

1. Check `expected_revision` and the `define` rule.
2. Build the new types from the old with `types`, check them with
   `Definition.check()`, and check that `rename` and `values` name fields that
   exist.
3. For every live record of a changed type, compute its new data: apply the
   renames and value maps, drop removed fields, and fill defaults. Validate it
   with `validate_data` and its references with `_validate_relationships`.
4. Judge every constraint of every changed type, and of every type that can
   reach one, against the new records. That is the re-check `update` already
   does, widened to every record.
5. Judge the `define` rule for the actor under the result.
6. If anything fails, roll back and refuse with the first problem and up to 20
   records. If `check` is true, roll back and report.
7. Otherwise write each rewritten record with its version raised by one, its
   references indexed again, and an `update` event giving the change's reason,
   as `ben via claude-code`. Add the field indexes the new fields need. Save
   the new revision and its audit line, and commit.
8. Swap the store's types, rules, and derived sets (`_dependent`,
   `_constraint_reads`) for the new ones under a lock.

Rewritten records' versions go up, so an agent holding an old version gets the
usual `version_conflict` and reads again. That is right: what it read was
written against the old definition.

Every request also checks the database's latest revision when it starts its
transaction, and reloads the definition if it has moved, as access grants
reload when their file changes. A change made by another process, such as a
command run on the host, then takes effect at once.

The first version holds the transaction while it rewrites every record. At
teamworld's size that is milliseconds. At a million records it would block
writers for a while. Batching is for when use shows it matters, as the plan's
other engine work is.

## Storage

A new append-only table, guarded by the same triggers as `events`:

```sql
CREATE TABLE definitions (
    revision INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    agent TEXT,
    reason TEXT NOT NULL,
    summary TEXT NOT NULL,
    definition TEXT NOT NULL,   -- the whole definition, as JSON
    after_event INTEGER NOT NULL
);
```

Revision 1 is the definition the world was created with, written by `system`.
A Python definition is stored the same way: its types are already plain data,
and its `seed()` runs only when the world is created.

## The audit

A change is one line. Its record is `World` and the new revision, so it fits
the five columns that teamworld's audit page and anyone reading
`GET /api/audit` already expect:

```
2026-10-19T09:12:40.118Z  ben via claude-code  changed definition  World 5  Added type PostIdea. Task: added field idea. Reason: Keep LinkedIn post ideas as their own type.
```

Every record the change rewrote gets its own `changed` line after it, as for
any update. Its detail names the fields that changed and ends
`Reason: Definition revision 5: Keep LinkedIn post ideas as their own type.`

Everyone in the world may read `changed definition` lines, since everyone can
already read the definition through `describe`.

## Start-up and the file

Once the database holds the definition, the file given with `--world` no
longer decides it. At start-up:

- **A new database** takes the file's definition as revision 1 and seeds its
  records, as today.
- **The file matches the revision it last applied.** The database's latest
  revision is used, including any changes made at runtime since.
- **The file changed, and no one has changed the definition at runtime since
  the file was last applied.** The file's definition is applied as a change by
  `system`, through the same steps, so a World release that changes
  `worlds/team.py` reaches Ben's running world with an audit line. If it would
  leave records invalid, the server refuses to start and says which.
- **The file changed, and so did the database at runtime.** The server refuses
  to start and names both, rather than discard either. `--world-from-file`
  applies the file anyway, as a change by `system`.

`world_meta` keeps a hash of the file's definition at the revision it was
last applied.

## What it means for teamworld

- Its `team.json` gains `"rules": {"define": "actor.role == 'admin'"}`, so a
  team's founder, and the admins they make, can reshape the world through
  their agents.
- The copy of the definition in each team's directory only creates the world.
  The world itself holds the current definition.
- The consent page says an agent can read and change whatever the person can.
  For an admin, that now includes the definition, and the page should say so.
- The audit page needs no change.
- Its `AGENTS.md` gap "Changing a world while it runs" closes when it moves
  its pin to the release with this.

## Decisions for Ben

1. **Who may change the definition by default.** This proposes no one, unless
   the definition has a `define` rule. Teamworld would give it to admins. The
   alternative, admins by default, needs the engine to know what an admin is,
   which it must not.
2. **Rewrites in the audit.** This proposes a line for every rewritten record,
   so each record's history is complete. A change that renames a status held
   by 15,000 issues then writes 15,000 lines. The alternative is one line for
   the change and a note in each record's history that is not an audit line.
3. **Lines about records of a removed type.** Their type's rules are gone, so
   the engine cannot judge who may read them. This proposes judging them with
   the last revision that had the type. The simpler alternative is to show them
   only to those who may change the definition.
4. **Type renames.** Left out of the first version. Agents can add a type,
   copy records into it with `change`, and remove the old one, but record ids
   change and history stays with the old records.
5. **Who may read past revisions.** This proposes everyone, through
   `GET /api/definition?revision=N`, since everyone may read the current one.
   It belongs with the plan's still-open question of who may read history
   (item 5).

## Building it

1. Store the definition in `definitions` and start from it, with the start-up
   rules above. Existing databases gain revision 1 from their file. No
   behaviour changes.
2. Add the `define` rule, `describe`'s `definition` block, and
   `GET /api/definition`.
3. Add the change itself, with `check`, over HTTP and MCP: the table of
   changes above, the transaction, the audit line, and the rewritten records'
   events.
4. Document it in [writing a world](writing-a-world.md) and the README, and
   release it.
5. Measure it as the sixth experiment measured authoring: give fresh agents a
   running team world and briefs that need it reshaped, such as "we track post
   ideas, linked to the tasks they come from" or "tasks need a due date, and
   todo is now called backlog". Count how many finish, how many calls they
   need, and whether any refusal left them stuck. Agent runs spend Ben's
   account, so this needs his go-ahead.

Tests for step 3 should cover every row of the table of changes, a refusal
that names records and changes nothing, a stale `expected_revision`, the
lockout check, a change seen by a second store on the same database, and
start-up with a changed file in each of its four cases.
