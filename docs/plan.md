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

Progress, 2026-10-16: the dataset is `open-index/open-github-issues`, loaded as
the `tracker` world with about 900,000 records and twenty tasks, and EspoCRM
holds the same records. The third agent is opencode with Xiaomi's MiMo 2.6
Flash. The runs are written up with their raw results. Codex and Claude Code
were as correct on both sides and needed half the calls or fewer on World.
MiMo got 60 of 60 on World and 14 of the 25 EspoCRM runs it finished. The CRM
and lab worlds keep their five tasks, so their results stay comparable with the
earlier experiments. See [the fifth experiment](fifth-experiment.md).

Done, 2026-10-16: Ben judged that the claim holds.

## Step 2: a private demo

Needs Ben's decision on where it is hosted, under what domain, and who is
invited first. The demo is built in its own repository. Work that any product
on World would need, such as the MCP server, is done in World.

1. **A standard way for agents to connect.** Offer the protocol as a Model
   Context Protocol (MCP) server, so connecting an agent means adding a
   connector, not pasting a prompt. Done in World on 2026-10-16: `POST /mcp`
   with nine general tools and a bearer token. World's part of OAuth is done
   too: it names its authorization server in protected resource metadata, and
   lets that server issue, list, and revoke expiring agent tokens. The
   authorization server, with its sign-in and consent page, belongs in the
   demo.
2. **Accounts and one world per team.** Sign-up, inviting members, and letting
   people issue and revoke tokens for their own agents. This includes the OAuth
   authorization server that chat apps' connectors sign in through.
3. **Changing a world while it runs.** A team will want its own statuses and
   fields. A world's definition must become data that can change safely, with
   existing records migrated and the change in the audit. Start from the JSON
   definitions and declared stage transitions on `experiment/crm-world`; see
   [the parallel line](crm-world-branch.md).
   Designed, awaiting Ben's decisions, 2026-10-19: see
   [changing a world while it runs](changing-a-world.md).
4. **A readable audit page.** Show each team its audit, so the second of
   World's aims is visible.
5. **Who may read history.** Decide whether reading a record's history needs
   access to it at the time as well as now, as `experiment/crm-world` required.

Done when: a handful of invited people have used it with their own agents.

## Step 3: a public demo

1. **Safety for strangers.** Limits on how much work one query may cost, since
   the rule language can range over a whole type. Rate limits per token,
   quotas per team, and backups.
2. **Open sign-ups.**

Done when: Ben opens it.

Evidence for step 2, 2026-10-17: an agent that had never seen World wrote
worlds for three new domains from plain-language briefs, and fresh agents did
all 30 tasks on them. So teamworld can let a team's agent write its world. See
[the sixth experiment](sixth-experiment.md).

## Engine work to do when use shows it matters

- Confirm several writes with one wait for the disk, to raise throughput.
- Count and search inside SQLite when the actor may read every candidate.
- Make history and the reference index compact.
- Report record counts in the description, so an agent can choose between
  reading everything and searching.
- Describe what fields mean, such as a money field's unit or the difference
  between when something happened and when it was recorded.

See [the fourth experiment](fourth-experiment.md) for the measurements behind
these.
