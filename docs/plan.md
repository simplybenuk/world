# Plan to a public demo

Agreed by Ben on 2026-10-14. The four experiments so far show that an agent
needs about half the calls on World as on EspoCRM for a domain it has not seen,
at 20 records and at a million, and that World is far smaller. This plan takes
that evidence to something people can use.

## The demo

A team tool in the style of Jira, offered as "bring your own agent":

1. Someone signs up and gets their own team world, with boards and tasks.
2. They connect their own agent, such as Claude, ChatGPT, or Codex, to it.
3. They and their team work through their agents, and watch the records and
   the audit grow.

World still provides no interface and no model. The reference browser client
stays as a fallback for people without an agent.

A large dataset from Hugging Face is not the demo. It is the next piece of
evidence, because it tests World on real data rather than generated data.

## Step 1: firm up the evidence

- Load a large real dataset from Hugging Face into a world. Prefer one shaped
  like a CRM or a task tracker, so the existing tasks still make sense, with
  real text, real distributions, and messy values. Check its licence.
- Widen the five tasks per domain to about twenty, covering reading, counting,
  searching, single writes, related writes, and refusals.
- Add a third agent to Codex and Claude Code.
- Run World and EspoCRM on the same data, tasks, and agents, as in the fourth
  experiment.

Done when: the comparison on real data is written up with its raw results in
`docs/results/`, and Ben has decided whether the claim holds.

## Step 2: a private demo

Needs Ben's decision on where it is hosted, under what domain, and who is
invited first.

1. **A standard way for agents to connect.** Offer the protocol as a Model
   Context Protocol (MCP) server, so connecting an agent means adding a
   connector, not pasting a prompt.
2. **Accounts and one world per team.** Sign-up, inviting members, and letting
   people issue and revoke tokens for their own agents.
3. **Changing a world while it runs.** A team will want its own statuses and
   fields. A world's definition must become data that can change safely, with
   existing records migrated and the change in the audit.
4. **A readable audit page.** Show each team its audit, so the second of
   World's aims is visible.

Done when: a handful of invited people have used it with their own agents.

## Step 3: a public demo

1. **Safety for strangers.** Limits on how much work one query may cost, since
   the rule language can range over a whole type. Rate limits per token,
   quotas per team, and backups.
2. **Open sign-ups.**

Done when: Ben opens it.

## Engine work to do when use shows it matters

- Confirm several writes with one wait for the disk, to raise throughput.
- Count and search inside SQLite when the actor may read every candidate.
- Make history and the reference index compact.

See [the fourth experiment](fourth-experiment.md) for the measurements behind
these.
