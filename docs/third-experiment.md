# Third experiment: a domain the agent has never seen

In [the second experiment](second-experiment.md) an agent did as well on EspoCRM
as on World, because it already knew EspoCRM's API. This experiment removes that
advantage. Both sides hold a calibration lab made up for the test, so the agent
has to learn it from each system's own description.

## The domain

A lab keeps instruments calibrated. An instrument has a tag, a kind, a site, a
state, and a custodian. A calibration records a result and a drift. A deviation
is a problem raised against an instrument, open until someone gives it a
disposition. The rules:

- An adjusted or failed calibration needs its drift.
- A deviation with a disposition needs its closing date.
- Only a manager can decide a critical deviation.
- An instrument cannot return to service while it has an open deviation.
- A technician changes only their own instruments, calibrations, and deviations.

World holds this as `worlds/lab.py`, the third definition on the unchanged
engine. EspoCRM holds it as three custom entities with custom fields, links,
two roles, and a before-save script per entity, created through its admin API
by `bench/control/lab.py`. Each rule was checked to refuse the same request on
both sides.

## The tasks

Kofi, a technician, asks for five things in plain words: count the quarantined
instruments at one site, record an adjusted calibration, record a failed
calibration with a deviation and a quarantine, close a deviation, and return an
instrument to service when the rules forbid it.

Each side's prompt gives a URL, a credential, and the address of that system's
own description: `/api/world` for World and `/api/v1/Metadata` for EspoCRM.

```sh
python3 -m bench agent --domain lab --repeat 3
python3 -m bench agent --domain lab --control --repeat 3
```

## Result

Run on 2026-10-13 with `codex exec`, three runs of each task on each side.

| | World | EspoCRM |
| --- | --- | --- |
| Task runs correct | 15 of 15 | 15 of 15 |
| Calls | 54 | 114 |
| Rejected calls | 1 | 1 |
| Agent time, all 15 runs | 392 s | 679 s |
| Agent time, median run | 27 s | 47 s |
| Bytes fetched, median run | 6.6 KB | 767 KB |
| Self-description | 5.6 KB | 191 KB |

Both got every task right. On World the agent used half the calls and 58% of
the time.

| Task | World calls | EspoCRM calls | World seconds | EspoCRM seconds |
| --- | --- | --- | --- | --- |
| Count quarantined instruments | 2, 2, 2 | 5, 5, 5 | 19, 19, 17 | 34, 36, 29 |
| Record an adjusted calibration | 3, 3, 3 | 8, 9, 9 | 22, 23, 24 | 44, 50, 48 |
| Record a failure, deviation, and quarantine | 5, 5, 5 | 10, 9, 10 | 35, 28, 36 | 44, 49, 63 |
| Close a deviation | 4, 4, 4 | 8, 7, 7 | 29, 28, 26 | 47, 48, 47 |
| Refuse a return to service | 4, 4, 4 | 7, 8, 7 | 27, 31, 29 | 40, 57, 44 |

World's call counts are identical across all three runs of every task, and on
four of the five they equal the scripted shortest path.

### Where EspoCRM's calls went

The agent fetched the 191 KB metadata three or four times in most tasks before
it made a single request about the lab. A description that size does not fit in
one reading, so it went back for different parts. It then looked up users and
related records that World's description had already told it about.

The bytes are what crossed the wire. The agent filtered the metadata before
reading it, so its token use is lower than 767 KB suggests. The calls and the
time are the fair measures.

### A second agent

Run on 2026-10-13 with Claude Code (`claude -p --model sonnet`), limited to
`curl` and text filters, on the same tasks and prompts.

| | World | EspoCRM |
| --- | --- | --- |
| Task runs correct | 15 of 15 | 15 of 15 |
| Calls | 51 | 108 |
| Rejected calls | 0 | 4 |
| Agent time, all 15 runs | 172 s | 243 s |
| Agent time, median run | 12 s | 16 s |

The result holds. Claude also needed half the calls on World, and it made the
same number of calls in all three runs of every task. It thinks faster than the
first agent, so the time gap is smaller: World took 71% of EspoCRM's time, where
the first agent took 58%. The checker marked one of Claude's EspoCRM answers
wrong for naming the instruments that were not quarantined as well as those
that were. The answer was right, the checker has been loosened, and the table
counts it as correct.

Codex was run again on World after the rules became expressions (see
[Write a world](writing-a-world.md)). It made the same calls as before in 14 of
15 runs. In the other, Codex exited after 3 seconds without making a request.

### Limits

- Two agents, 15 runs a side each. The gap in calls is the same for both.
- The prompt pointed EspoCRM's agent at the full metadata. EspoCRM can return
  one part of it, and an agent that knew to ask for only the lab's entities
  would read far less. Nothing in the system tells an agent that.
- A scripted client that already knows each API needed 17 calls on World (14
  since the change below) and 12 on EspoCRM. EspoCRM's API is not slower to use once learned. It is slower to
  learn.

## Doing more in one call

After these runs, World gained two operations. Both are in `GET /api/world`.

- A query takes a `where` expression in the language of the rules, and may
  follow references: `record.instrument.tag == 'PIP-007' and record.disposition == 'open'`.
  `'harbor' in record.name` matches part of a text. `include` returns the
  records a result refers to. A query cannot see through a record the actor may
  not read.
- `POST /api/changes` saves several changes together or none of them. A later
  step can use the ID of a record an earlier step created.

| Lab tasks on World, 15 runs | Calls before | Calls after | Seconds before | Seconds after |
| --- | --- | --- | --- | --- |
| Scripted shortest path, 5 tasks | 17 | 14 | | |
| Codex | 54 | 44 | 392 | 506 |
| Claude | 51 | 44 | 172 | 176 |

Both agents used both operations without being told to, and every run was
correct. Calls fell by about 15%. Time did not fall. Three of the five tasks
were already at their floor, and a larger request takes the agent longer to
write. The Codex times vary by 100 seconds between identical runs on different
hours, so only the call counts are comparable for it.

The stronger gain is that the three writes of the failed-calibration task now
succeed or fail as one. Before, a refusal at the third write left the first two
saved.

The first version of this change made the CRM tasks worse. Claude needed 67
calls with 15 rejected, because the new query language had no way to search
part of a name and the description no longer mentioned the old filter that
did. With text search added, the same runs took 47 calls with none rejected.
Codex had needed 51 before the change.

## What the two experiments show together

| | Known domain (CRM) | Unknown domain (lab) |
| --- | --- | --- |
| Correct | level | level |
| Calls | level | World uses half, for both agents |
| Agent time | level | World takes 58% to 71% |
| Application size | 81 KB against 172 MB | the same engine |

World's advantage is that an agent can learn an unfamiliar system from one small
description. Where the agent already knows the other system, that advantage
disappears. A company's own records are the unfamiliar case.
