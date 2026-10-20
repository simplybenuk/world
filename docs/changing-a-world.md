# Changing a world while it runs

Step 2, item 3 of [the plan](plan.md). Claude designed it on 2026-10-19 and
built it on 2026-10-20, when Ben asked for it to be built. The five open
decisions were taken as the design proposed them. They are listed at the end,
so that Ben can change any of them.

## Why

A world's definition used to be read once, when the server started. No agent
could add a type or a field, and teamworld had no page for it either. The only
way to change a team's world was to edit its JSON file on the host and restart
it.

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
- A `define` rule in the definition says who may change it.
- The MCP tool `define`, and `POST /api/definition`, replace or remove whole
  types and can rename fields and enum values. A change can be checked
  without being made.
- The engine applies the change in one transaction. It rewrites the records
  that need it, re-checks the records the change could affect, and refuses
  the whole change if any would be invalid.
- Each change is one `changed definition` line in the audit, plus an
  `update` event for each record it rewrote.

## Who may change a definition

The definition has a top-level `rules` object with one rule, `define`. The
rule is judged with `actor` alone, since there is no record, so it may not
name `record` or `saved`:

```json
{"name": "team", "title": "Acme", "version": "1",
 "rules": {"define": "actor.role == 'admin'"},
 "types": {...}}
```

Without the rule, no one may change the definition at runtime, so every
existing world keeps working as it did. `describe` reports the revision, the
rule, and whether this actor passes it, as it does for a type's operations:

```json
"definition": {"revision": 4, "capabilities": {"define": true, "rules": {"define": "actor.role == 'admin'"}}}
```

Over HTTP, `GET /api/world` lists the two definition operations only to
someone who may change the definition, so that no one else pays to read them.

A change must not lock out the person making it. The engine judges the
`define` rule again under the new definition and the migrated records. It
refuses a change after which this actor could no longer change the
definition, with `409 define_locked_out`. That is the same check `update`
makes after an update. The team world's "keep at least one admin" constraint
then also keeps someone able to change the definition.

## The `define` tool

Called with no change, `define` returns the definition as data, with its
revision. That is the form a change takes, which differs from what
`describe` shows each actor. `GET /api/definition` does the same, and
`?revision=N` returns an earlier one.

A change:

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
| `rules` | The world's rules, which give `define` |
| `title` | The world's title |
| `expected_revision` | The revision the change was written against. A stale one is refused, as `expected_version` is for records |
| `reason` | Why, for the audit |
| `check` | When true, report what the change would do and change nothing |

Whole types, not a patch language, because agents already write whole types
well. That is what the sixth experiment measured. A world with 200 types does
not need to be sent whole to change one.

The answer, for a check or a change, says what happens:

```json
{"revision": 5, "saved": true,
 "summary": "Added type PostIdea. Task: renamed assignee to owner, changed field status, status value todo became backlog.",
 "records": {"Task": {"checked": 112, "rewritten": 41}}}
```

A refusal is `409 definition_refused`. It names the type, the problem, and up
to 20 of the records in the way, and counts the other problems:

```json
{"code": "definition_refused",
 "message": "3 Task records would not fit the new definition, so nothing changed. due is required.",
 "details": {"field": "due", "type": "Task", "code": "required_field", "records": ["task-1f…", "task-9c…", "task-a2…"],
             "more": 0, "other_problems": 0}}
```

## What a change may do to records

The engine sorts each change by what it does to the records already saved.

| Change | What happens to saved records |
| --- | --- |
| Add a type | Nothing |
| Change rules, a field's `write_rule`, a description, or a constraint's message | Nothing. The new rules apply from the next request |
| Add a field without a default | Nothing. Rules read a missing field as `None`, as they do for any field a record lacks |
| Add a field with a default | Every live record gets the default, and is rewritten |
| Add a required field without a default | Refused while any live record of the type exists |
| Remove a field | Its value is taken out of every live record, which is rewritten. History keeps the old values |
| Rename a field with `rename` | Every live record holding it is rewritten |
| Rename an enum value with `values` | Every live record holding it is rewritten |
| Remove an enum value without `values` | Refused while a live record holds it |
| Narrow a field: lower a `max_length`, raise a `min`, change its type, make it required | Allowed if every live record still validates. Otherwise refused, naming them |
| Add or change a constraint | Allowed if every live record satisfies it. Otherwise refused, naming them |
| Remove a type | Refused while it has live records. A type another type still refers to cannot be removed either |
| Rename a type | Not offered. Records, history, and references are keyed by type name |
| Change the world's name or who acts | Refused |

Filling in a value the engine cannot know, such as a due date for 40 existing
tasks, is the agent's job, not the definition's. The agent adds the field as
optional, fills it in with `change`, so every value is in the audit as its own
decision, and then makes it required. A migration language would put those
decisions out of sight, so there is none. A default of `$actor` is not filled
in for existing records, because the person changing the definition did not
create them.

Deleted records keep the shape they had. `history` returns stored states as
they were.

Every definition, whether loaded or changed, must now name only fields that
exist in its rules, write rules, and constraints. A rule naming a missing
field used to fail only when it was judged. Now it is refused when the world
loads or the change is made.

## How the engine applies a change

All in one write transaction, so that a change either happens whole or not at
all, and no writer sees half of it:

1. Check the `define` rule and `expected_revision`.
2. Build the new definition from the old with the change, and check it as a
   world is checked when it loads. Check that `rename` and `values` name
   fields that exist.
3. Save the new revision, then rewrite every live record of a changed type
   that needs it: apply the renames and value maps, drop removed fields, and
   fill defaults. Validate each, and its references. Each rewritten record's
   version goes up by one, its references are indexed again, and it gets an
   `update` event giving the change's reason, as `ben via claude-code`.
4. If any record was rewritten or any constraint changed, judge every
   constraint against every live record of its type. A constraint may count
   the records of any type, so this is not limited to the types the change
   named. At teamworld's size this costs milliseconds. In a large world it
   reads every record of a type with constraints.
5. Judge the `define` rule for the actor under the result.
6. If anything fails, roll back and refuse with the first problem and up to
   20 records. If `check` is true, roll back and report.
7. Otherwise commit, and run the new definition.

Rewritten records' versions go up, so an agent holding an old version gets the
usual `version_conflict` and reads again. That is right: what it read was
written against the old definition.

Every transaction also checks the database's latest revision when it starts,
and runs it if it has moved. A change made by another process, such as a
command run on the host, then takes effect at once.

The transaction is held while every record is rewritten. At a million records
that would block writers for a while. Batching is for when use shows it
matters, as the plan's other engine work is.

## Storage

An append-only table, guarded by the same triggers as `events`:

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
A world from before this release takes the definition it is started with as
revision 1. A Python definition is stored the same way: its types are already
plain data, and its `seed()` runs only when the world is created.

## The audit

A change is one line. Its record is `World` and the new revision, so it fits
the five columns that teamworld's audit page and anyone reading
`GET /api/audit` already expect:

```
2026-10-20T09:12:40.118Z  ben via claude-code  changed definition  World 5  Added type PostIdea. Task: added field idea. Reason: Keep LinkedIn post ideas as their own type.
```

Every record the change rewrote gets its own `changed` line after it, as for
any update. Its detail names the fields that changed, including those
removed, and ends `Reason: Definition revision 5: Keep LinkedIn post ideas as
their own type.`

Everyone in the world may read `changed definition` lines, since everyone can
read the definition through `describe`. Revision 1 has no line: the records
the world was created with already have theirs.

## Start-up and the file

Once the database holds the definition, the file given with `--world` no
longer decides it. At start-up:

- **A new database** takes the file's definition as revision 1 and seeds its
  records, as before.
- **The file is the one last applied.** The database's latest revision runs,
  including any changes made while it ran.
- **The file now matches the latest revision.** The latest revision runs, and
  the file is remembered as applied.
- **The file changed, and no one has changed the definition at runtime since
  the file was last applied.** The file's definition is applied as a change by
  `system`, through the same steps, so a World release that changes
  `worlds/team.py` reaches a running world with an audit line. If it would
  leave records invalid, the server will not start and says which.
- **The file changed, and so did the database at runtime.** The server will
  not start, and names both, rather than discard either. `--world-from-file`
  applies the file anyway, as a change by `system`.

`world_meta` keeps a hash of the file's definition and the revision at which
it was last applied.

## What it means for teamworld

- Its `team.json` gains `"rules": {"define": "actor.role == 'admin'"}`, so a
  team's founder, and the admins they make, can reshape the world through
  their agents. Teamworld writes a team's file once, when the team starts, so
  for teams that already exist it must add the rule to their files. Each world
  then applies it as a change by `system` when it restarts.
- The copy of the definition in each team's directory only creates the world.
  The world itself holds the current definition.
- The consent page says an agent can read and change whatever the person can.
  For an admin, that now includes the definition, and the page should say so.
- The audit page needs no change.
- Its `AGENTS.md` gap "Changing a world while it runs" closes when it moves
  its pin to the release with this.

## Decisions taken as proposed

Ben may change any of these.

1. **Who may change the definition by default.** No one, unless the
   definition has a `define` rule. Admins by default would need the engine to
   know what an admin is, which it must not.
2. **Rewrites in the audit.** A line for every rewritten record, so each
   record's history is complete. A change that renames a status held by
   15,000 issues writes 15,000 lines. The alternative is one line for the
   change and a note in each record's history that is not an audit line.
3. **Lines about records of a removed type.** Judged by the last revision
   that had the type, so the people who could read them still can.
4. **Type renames.** Left out. Agents can add a type, copy records into it
   with `change`, and remove the old one, but record ids change and history
   stays with the old records.
5. **Who may read past revisions.** Everyone, through `define` with no change
   or `GET /api/definition?revision=N`, since everyone may read the current
   one. It belongs with the plan's still-open question of who may read history
   (item 5).

## Still to do

Measure it as the sixth experiment measured authoring: give fresh agents a
running team world and briefs that need it reshaped, such as "we track post
ideas, linked to the tasks they come from" or "tasks need a due date, and
todo is now called backlog". Count how many finish, how many calls they need,
and whether any refusal left them stuck. Agent runs spend Ben's account, so
this needs his go-ahead.

`tests/test_define.py` covers the table of changes, refusals that name records
and save nothing, a stale revision, the lockout, check, a second store on the
same database, MCP, and start-up in each of its cases.
