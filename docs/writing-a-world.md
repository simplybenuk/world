# Write a world

A world is one file in `worlds/` that describes a domain as data: its record
types, who may do what, and which states are valid. The engine stores the
records, enforces the rules, keeps the history, and serves the protocol. This
page is the reference for that file. `worlds/lab.py` is a complete example in
160 lines.

## The definition

```python
from world.definition import Definition

class Lab(Definition):
    name = "lab"                 # saved in the database; one database holds one world
    title = "Calibration lab"
    version = "0.2.0"
    types = TYPES
    actors = ("ada", "kofi")     # seeded people who receive access tokens

    def seed(self, today):
        return [("Person", "ada", {"name": "Ada", "role": "manager"}), ...]

DEFINITION = Lab()
```

Add the module's name to `AVAILABLE` in `worlds/__init__.py`, then serve it with
`python3 -m world.server --world lab --data-dir DIRECTORY`.

A world can also be one JSON file, with the same types and rules and its first
records listed. Every record of the actor type is a person who can be given
access, and a record comes after the records it refers to:

```json
{"name": "clinic", "title": "Harbourside Vets", "version": "1", "actor_type": "Person",
 "types": {"Person": {...}, "Appointment": {...}},
 "records": [{"type": "Person", "id": "okafor", "data": {"name": "Dr Amara Okafor", "role": "vet"}}]}
```

Serve it with `python3 -m world.server --world clinic.json --data-dir DIRECTORY`.
`bench/briefs/clinic-reference.json` is a complete example.

The engine parses every rule when the world loads. A rule it cannot parse stops
the server from starting and names the type.

## A type

```python
"Deviation": {
    "description": "A problem found with an instrument, open until someone decides its disposition.",
    "fields": {
        "instrument": {"type": "reference", "target": "Instrument", "required": True},
        "severity": {"type": "enum", "required": True, "values": ["minor", "major", "critical"]},
        "closed_on": {"type": "date", "nullable": True, "default": None},
        "raised_by": {"type": "reference", "target": "Person", "required": True,
                      "default": "$actor", "write_rule": "actor.role == 'manager'"},
    },
    "rules": {
        "read": "actor.id is not None",
        "create": "actor.role == 'manager' or record.raised_by == actor.id",
        "update": "actor.role == 'manager' or record.raised_by == actor.id",
        "delete": "actor.role == 'manager'",
    },
    "constraints": [
        {"check": "record.disposition == 'open' or record.closed_on is not None",
         "message": "A deviation with a disposition needs its closed_on date.",
         "fields": ["closed_on"]},
    ],
}
```

One type must describe the people who act. It is `Person` unless the definition
sets `actor_type`.

### Fields

| Type | Value | Options |
| --- | --- | --- |
| `text` | string | `min_length`, `max_length` |
| `enum` | one of `values` | `values` |
| `integer` | whole number | `min`, `max` |
| `boolean` | `true` or `false` | |
| `date` | `YYYY-MM-DD` | |
| `reference` | the ID of a live record of type `target` | `target` |
| `list` | list of `items` | `items`, `unique`, `max_items` |

Every field accepts `required`, `nullable`, `default`, and `write_rule`. The
default `"$actor"` is the ID of whoever creates the record.

### Rules

Each type states four rules. The engine evaluates them and sends the same text
to clients in `GET /api/world`.

| Rule | Judged against |
| --- | --- |
| `read` | the saved record |
| `create` | the proposed record |
| `update` | the saved record, then the proposed record. Both must pass |
| `delete` | the saved record |

A field's `write_rule` says who may change that field in an update. It does not
apply to a create. Use the `create` rule for that.

### Constraints

A constraint is a state that must hold after a create or an update, whoever
makes the change.

| Key | Meaning | Default |
| --- | --- | --- |
| `check` | the expression that must be true | required |
| `message` | what to tell the client when it is not | required |
| `fields` | the fields a client should look at | none |
| `on` | which of `create`, `update`, `delete` it applies to | create and update |
| `code`, `status` | the error returned | `constraint_failed`, 422 |

Write a constraint between two records once, on either of them. When a record
changes, the engine re-checks the constraints of every record that refers to
it, directly or through other records. `worlds/crm.py` says a primary contact
must belong to the opportunity's organization. That one line also stops the
contact moving to another organization.

The engine skips that re-check when no constraint in the world reads a field
that changed. Adding a maintainer to a project with 15,000 issues re-checks
none of them, because no constraint reads `maintainers`. A constraint that uses
`can_read` may read anything, so a world with one always re-checks.

The engine always rejects a reference to a missing record and the deletion of a
record that others refer to. Do not write constraints for those.

## The rule language

A rule is one expression in Python syntax. The engine parses it into a syntax
tree and walks the tree. It never runs it as Python.

| Name | Meaning |
| --- | --- |
| `actor` | the person acting |
| `record` | the record being judged |
| `saved` | its stored state. `None` while it is being created |

- **Fields.** `record.state`, `actor.role`. Every record also has `id` and `type`.
- **References.** A dot follows a reference to the record it names:
  `record.organization.owner`. A missing value anywhere in the path gives
  `None`, so `record.opportunity.owner == actor.id` is false when there is no
  opportunity.
- **Comparison.** `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `is None`,
  `is not None`. A reference equals the ID it holds, so `record.owner == actor.id`
  and `actor.id in record.members` both work. Ordering against `None` is false.
- **Text.** `'harbor' in record.name` is true when the text contains that part,
  ignoring case. There are no string methods.
- **Logic.** `and`, `or`, `not`, and brackets.
- **Values.** Strings, whole numbers, `True`, `False`, `None`, and tuples of them.

| Function | Meaning |
| --- | --- |
| `changed('field')` | the field differs from the saved record, or the record is new |
| `can_read(record.project)` | the actor may read that record |
| `any(d.state == 'open' for d in Deviation if d.instrument == record.id)` | some live record of that type matches |
| `count(p for p in Person if p.role == 'admin')` | how many live records of that type match |
| `days(record.out, record.due)` | the whole days from the first date to the second, negative if the second is earlier. `None` if either is missing |
| `today()` | today's date in UTC, as `YYYY-MM-DD`, when the rule is judged |

A loan of at most 14 days is `days(record.out, record.due) <= 14`. Because a
missing date gives `None`, and ordering against `None` is false, write
`record.due is None or days(record.out, record.due) <= 14` when the date is
optional. A rule with `today()` is judged when a change is made. The engine
does not re-judge saved records as days pass.

Other arithmetic, function definitions, other function calls, and names other
than those above are refused when the world loads.

## The same language in queries

`POST /api/query` accepts a `where` expression in this language, over `record`
and `actor`. A client that has read a world's rules already knows how to query
it. A query sees only what the actor may read, so a path through a record they
cannot read gives `None`.

## What the engine derives

You do not write these. The engine works them out from the rules for each actor.

- **Type capabilities.** `GET /api/world` says whether the actor can ever create,
  update, or delete each type. A capability is false only when the actor's own
  fields rule it out, as `actor.role == 'manager'` does for a technician.
- **`writable: false`** on a field whose `write_rule` the actor can never meet.
- **Record capabilities.** Every record returned says whether this actor can
  update or delete it now.

## Limits

- A Python definition can seed dates relative to today. A JSON world's records
  are fixed.
- Apart from `days()`, there is no arithmetic, so a rule cannot add up amounts.
- There are no decimal numbers. Store 31.5 kg as 315 tenths of a kilogram.
- A rule that refuses a request returns the rule's text, not which part of it
  failed.
- Type and field names use letters, digits, and underscores, and start with a
  letter.
- A text field of up to 300 characters, and every enum, date, integer, and
  reference, is indexed for exact matches. Searching part of a text reads every
  record of the type.
