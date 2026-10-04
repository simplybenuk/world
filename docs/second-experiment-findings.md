# Second experiment findings

The question was whether a CRM could reuse the team experiment's record engine
and browser client, with domain differences in a separate World definition.
The two runnable examples support that approach for membership-based access,
record relationships, and enum transitions.

This is a prototype for the team developing the World method. Its source is in
the isolated `experiment/crm-world` branch. The
[run guide](second-experiment.md) gives the launch and verification commands;
the [format guide](world-format-prototype.md) describes the authoring sequence.

## What changed

The first engine had team-specific type and permission branches. The shared
engine now loads a JSON definition. Both examples use the same `WorldStore`,
HTTP handlers, Codex planner, and browser files.

| Part | Team definition | CRM definition |
| --- | --- | --- |
| Identity | Person | Person |
| Domain records | Project, Task, Comment | Organization, Contact, Opportunity, Interaction |
| Access scope | Project | Organization |
| Relationship rules | Owners and authors belong to the project | Owners and authors belong to the organization; customer references agree |
| Enum changes | Task status accepts any declared status | Opportunity stage follows declared next values |
| Seed | 14 fictional records | 14 fictional records |

The definitions describe field shapes, policy, constraints, transitions, and
initial records. They contain no layouts or conversation state. The CRM has no
separate client or domain-specific Python rule implementation.

The shared engine gained integer fields with bounds, reference-path traversal,
structured policy checks, relationship consistency checks, and enum transitions.
The browser needed neutral prompt suggestions and schema-derived plural labels.
Its existing generic forms already supported numeric inputs; they now use the
discovered bounds. Planning instructions now use domain descriptions rather than
assuming tasks and projects.

## Observed scenarios

The live CRM requests selected Ben's open opportunity, rendered the same record
as a board grouped by stage, and proposed changing Northstar renewal from
`qualified` to `proposal`. Generating that proposal left its record and history
unchanged. Acceptance saved the stage and one audit event. Reloading discarded
the view, while a process restart preserved the accepted record.

The browser also created, edited, and deleted a contact through discovered
forms. Integer editing saved the exact minor-unit amount. A direct `lead` to
`won` edit returned 422, retained the editor's input, and changed neither the
record nor its history. The scenarios also checked cross-organization links,
dependent records during moves, protected fields, and stale versions.

The installed CRM service gives Ben access to all 14 initial records. Sam sees
10, including Northstar but excluding Atlas, with no update capability. The
team and CRM use separate data, credentials, and browser cookie names. Both
services were active when checked; the first service continued using its
original checkout and records.

## Verification evidence

Verified on 2026-10-04:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
npm run test:browser
npm run test:crm-browser
npm run test:browser -- --live
npm run test:crm-browser -- --live
```

All 87 Python tests passed. Both fixture browser suites and both real Codex
browser suites passed. One team request initially failed at the provider step;
a focused retry and the subsequent complete live run succeeded. Generation can
still fail independently of record browsing and editing.

Browser screenshots are local evidence in the ignored `artifacts/browser`,
`artifacts/crm-browser`, `artifacts/live`, and `artifacts/crm-live` directories.
They are not required to run the examples. Verification used disposable
databases and stopped its own browser/server processes. The separate CRM user
service remains running for review, with its private data directory retained.

## Decision and limits

The next World example can start as a definition and scenarios rather than a
new set of pages and CRUD routes. These two domains give evidence for a shared
runtime and a small declarative vocabulary.

They share the same membership access pattern. A domain with approval chains,
row ownership rules, or transactional business actions may need a different
policy vocabulary. Named business actions and schema migrations remain outside
this experiment. New opportunities may start at any declared stage; the
transition map governs later edits. Each mutation checks the live record set,
and larger datasets have not been measured.

The client still offers four layouts. Codex uses cloud inference and can fail
or choose different record selections. The format is experimental, and
production promotion remains a separate decision.
