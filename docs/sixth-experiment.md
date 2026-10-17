# Sixth experiment: worlds written by an agent

Every earlier world was written by someone who knew World well. This experiment
asks whether someone who has never seen it can write one, and whether a
fresh agent can then use that world. Codex put the question in its review on
2026-10-16: can somebody define an unfamiliar business domain, connect an
agent, and get useful work done without writing application code?

Here the author is an agent, Claude Code on Sonnet, not a person. It had only a
plain-language brief and [`writing-a-world.md`](writing-a-world.md). The runs
were on 2026-10-17.

## The test

`python3 -m bench.authoring` runs it.

1. **Write.** A fresh agent gets a brief and the reference, and answers with a
   world as JSON. If World refuses the world, the agent sees the error and
   tries again, up to three times.
2. **Use.** For each of ten tasks, a fresh World is seeded from that world, and
   another fresh agent gets only a URL and a token, as in the other experiments.

Three briefs are in `bench/briefs/`. None of these domains had been modelled
here before. Each brief names its people and key records by ID and states its
rules in plain words. The author chooses the types, fields, and how to write
each rule.

| Domain | Rules in the brief |
| --- | --- |
| A vet clinic | Only vets prescribe or book surgery. Appointments are with a vet. No appointment for an animal that has died. Weights to one decimal place. Appointments are cancelled, never deleted |
| A festival volunteer rota | Volunteers sign only themselves up, unless they are the coordinator or the team's lead. A shift takes no more volunteers than it needs. Nobody under 18 on the bar. No overlapping shifts |
| A tool lending library | Staff record loans. A loan lasts at most 14 days. One loan per tool at a time. No borrowing after a membership runs out. Power saws only after the safety induction. Members see only their own loans |

Each domain has ten tasks: two or three readings, four writes, and three or four
refusals.

The checks cannot know the author's field names, so they look for values. A
write is correct when a change after the seed holds every expected value. A
refusal is correct when no change holds the forbidden values and the answer
says the change was not made. A reading is correct when the answer holds the
facts. I wrote a world to each brief by hand. `python3 -m bench.authoring
check` confirms that every check fails on an untouched world and passes on the
correct change.

## Results

| Domain | Author's attempts | Author's time | World written | Tasks correct | Calls | Agent time |
| --- | --- | --- | --- | --- | --- | --- |
| Clinic | 1 | 22 s | 7.6 KB | 10 of 10 | 32 | 113 s |
| Festival | 2 | 37 s | 7.6 KB | 10 of 10 | 40 | 108 s |
| Library | 1 | 24 s | 6.6 KB | 10 of 10 | 41 | 109 s |

When run, the checks marked 28 of 30 correct. Both misses were checks that were
too narrow, not mistakes by the agents:

- **Clinic, cancel.** The author marked a cancellation with `cancelled: true`.
  The check looked only for the text "cancel".
- **Festival, new shift.** The author stored times as minutes since midnight,
  840 and 1080, so that overlap could be compared as numbers. The check looked
  only for "14" and "18".

Both checks now accept these forms, and both still fail on an untouched world.
The saved results keep the judgments as run.

The festival author's first answer was not valid JSON. It got the error, and
its second answer was accepted. Neither other author needed a second attempt.

## Were the refusals World's?

An agent can decline a request because it read a rule, without World ever
refusing it. So after the runs, each refusal's forbidden write was sent
straight to the authored world, as the task's actor, with the author's field
names (`authoring-enforcement.txt`).

World refused 9 of the 10. The exception is the library's 14-day limit. The
rule language has no date arithmetic, so the author could not write the rule.
It wrote the limit into the loan type's description instead. The task agent
read that, declined the 28-day loan, and said World would probably have
accepted it. It would have.

## What the authors wrote

The three worlds are in `docs/results/authored-*.json`. They are close to the
hand-written references, and in places better.

- **Every rule but one became a rule or a constraint.** That includes the count
  ("no more volunteers than it needs"), a rule across three records ("a team's
  lead may sign people up for that team's shifts"), and the overlap of two
  shifts in time.
- **The festival author made the bar rule general.** Each team has a minimum
  age, and the bar's is 18. It also turned times into minutes so overlap could
  be compared. Neither was in the brief.
- **The clinic author stored weights in tenths of a kilogram.** The reference it
  was given suggests exactly that, so this tells us little.
- **One weakness.** The clinic's rule against booking an animal that has died
  holds only when the appointment's animal changes, so an existing appointment
  stays valid after its animal dies. That reading is defensible.

## What it shows

On these three briefs, an agent that had never seen World wrote a working world
in under 40 seconds, from an 8 KB reference. Another agent then did every task
on it, in about 4 calls a task. The rules the author wrote were enforced by
World, not merely followed by the agent.

The limit was the rule language, not the author. A rule that needs arithmetic,
here a number of days between two dates, cannot be written. Decimals have to be
stored as whole numbers.

### Caveats

- One run of each, with one model, Sonnet, as both author and user.
- I wrote the briefs. They name IDs and state each rule plainly. A real team's
  description of how it works would be vaguer and would contradict itself.
- The author was an agent. Whether a person, working through their own agent,
  gets the same result is untested.
- The reference the authors read was written by me, and in one place it
  suggested the answer.

## Next

- Add date arithmetic, and perhaps decimals, to the rule language, so that a
  rule like the 14-day limit can be stated once and enforced. Done on
  2026-10-17: `days(a, b)` and `today()`. The library reference now enforces
  the limit as `days(record.out, record.due) <= 14`.
- Teamworld can let a team's own agent write and change its world. This
  experiment suggests that works for simple domains. Changing a world that
  already holds records is untested.
