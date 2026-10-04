# World format prototype

The second experiment tries a small JSON format for domain definitions. It
supports the access pattern shared by the team and CRM examples. It is an
experimental vocabulary, not a stable protocol specification.

This guide records the second experiment. The [third experiment](third-experiment.md)
keeps this definition vocabulary and adds a separate candidate contract for
headless agent reads, temporal facts and atomic updates.

## Build a small world

Start with the records people need to keep, their relationships, and the changes
they need to make. For a CRM, that gives organizations, contacts, opportunities,
and interactions. Write representative scenarios before choosing a view: a sales
person reviews open opportunities, records an interaction, advances a stage,
and encounters a change that the rules must reject.

Then create a separate definition with a World ID, name, version, identity type,
record types, and fictional seed records. Each record type describes its fields,
permissions, and constraints. Run that definition through the shared engine.
Ask the shared client for useful views and review proposed changes against the
record history. Capture both valid changes and rejected ones.

If the domain needs a rule the vocabulary cannot express, record that gap before
extending the engine. The comparison between domains is the evidence for a new
format feature.

## Records and fields

Each definition has these top-level keys:

```json
{
  "id": "crm",
  "name": "CRM World",
  "version": "0.2-prototype",
  "actor_type": "Person",
  "types": {},
  "seed": []
}
```

`types` maps type names to descriptions and `fields`. Supported field kinds are
`text`, `enum`, `date`, `reference`, `list`, and `integer`. Fields can declare
required values, defaults, nullability, allowed enum values, reference targets,
list limits, and numeric bounds. Money in the CRM uses integer minor units and
a separate currency enum; the engine does not calculate exchange rates.

A field definition can look like this:

```json
{
  "owner": {"type": "reference", "target": "Person", "nullable": true, "default": null},
  "stage": {"type": "enum", "values": ["lead", "qualified", "proposal", "won", "lost"], "default": "lead", "required": true},
  "amount_minor": {"type": "integer", "minimum": 0, "default": 0, "required": true}
}
```

Seed entries contain `type`, `id`, and `data`. Date fields can use
`{"relative_days": -1}` for yesterday at first creation. Seed data runs once;
reopening a world does not overwrite edits or recreate deleted records.

## Permissions

Each type has a `policy`. The prototype uses the stored identity's `admin`,
`member`, or `viewer` role and an optional relationship path to an access scope.
A scope record declares its `members` and `visibility`.

For example, an opportunity takes its scope from its organization:

```json
{
  "scope": "organization",
  "read": "scope",
  "create": "member",
  "update": "member",
  "delete": "admin",
  "admin_fields": []
}
```

Admins can read and change records. Members can create and update within scopes
they belong to. Viewers can read team-visible records. A private scope is visible
to its members and admins. Moves require access to the current and destination
scope. `admin_fields` protects fields such as membership and visibility.
`author_field` attributes new records to the current person and protects existing
attribution. Identity records can use `update: "self"` while protecting role.

The scope path follows discovered references. An empty path means the current
record; `null` means no scope. Human-readable `rules` describe the policy, while
the engine enforces the structured `policy`.

## Business constraints

The prototype supports membership and relationship consistency checks:

```json
[
  {"kind": "member_of", "field": "owner", "members": "organization.members", "nullable": true},
  {"kind": "same_reference", "left": "contact.organization", "right": "organization", "nullable": true}
]
```

An opportunity's owner must belong to its organization. Its contact, when set,
must belong to that same organization. The engine checks the proposed state
across live records, including dependents of the edited record.

An enum can also declare allowed updates:

```json
[
  {
    "field": "stage",
    "allowed": {
      "lead": ["qualified", "lost"],
      "qualified": ["proposal", "lost"],
      "proposal": ["won", "lost"],
      "won": [],
      "lost": []
    }
  }
]
```

These transitions govern changes to an existing opportunity. The declaration
does not require newly created opportunities to start at `lead`. That remains
an explicit limit of this vocabulary.

Record state, permissions, versions, and audit events remain the engine's
responsibility. Layout selection and conversation remain in the temporary
client. The format has no executable expressions or client UI definitions.

More domains are needed to test access patterns other than membership, named
business actions, schema evolution, and larger datasets. This experiment should
inform those decisions without claiming to have settled them.
