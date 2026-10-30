# Working on World

World is built by several agents and one person. This file holds what an agent
needs that the code and the README do not already say. Record durable project
decisions here, in the repository, so every agent sees them. Do not keep them in
a private or per-tool memory store.

## Decisions

Each entry gives the decision, who made it and when, and the reason.

### World is two things

Ben, 2026-10-09. Weigh every proposal against these two aims.

1. A system of record for LLMs.
2. A human-readable audit. Every time a record is created, viewed, changed, or
   deleted, the audit shows when it happened, who did it, and what happened.
   Lines of plain text are enough.

Views count. A read that leaves no audit line is a defect.

The audit names software as well as people. A person issues a token to an agent,
and the audit records its reads and changes as `person via agent`. Give every
agent its own token. Do not let one work with a person's own token.

### A public demo comes after the evidence

Ben, 2026-10-13. Once the measurements give confidence that World is onto
something, stand up a demo application and let people use it. Until then the
work is experiments and improvements against the two aims above. Do not start
building or hosting a public demo before Ben says the evidence is enough. The
demo is planned in `docs/plan.md`. Hosting it needs Ben's decision.

Ben, 2026-10-16: the claim holds. The evidence is enough to build the demo.

### The plan to a public demo

Ben, 2026-10-14. Follow `docs/plan.md`. In short: firm up the evidence on a
large real dataset, then build a private demo of a team tool where people bring
their own agents, then open it to the public. Step 1 is done (2026-10-16).
Update the plan when a step finishes or a decision changes it.

### World is a database for agents, and the demo lives in its own repository

Ben, 2026-10-16. World is a database, not a language, protocol, or standard. It
has a definition language and a protocol, as SQL databases have a dialect and a
wire protocol. It becomes a standard only if someone builds a second engine for
its protocol.

The demo is World's first user and lives in a separate repository. Whatever
another product built on World would also need belongs in World: the engine,
the protocol, the MCP server, agent tokens and `person via agent`, and changing
a world's definition while it runs. Accounts, teams, sign-up, token pages, the
OAuth authorization server with its sign-in and consent page, the team-tool
definition, the audit page people see, and hosting belong in the demo. World
checks tokens and names its authorization server; it shows no page. When the demo has to work around World, fix World.

The name stays World (Ben, 2026-10-16). It sounds bigger than the thing is,
which is the joke: like Phoebe's tattoo of the whole world in *Friends*, it is
just a dot. Other names were considered and set aside: Agentbase and Ontic are
taken, and Realm is MongoDB's mobile database.

### Releases, and how teamworld depends on World

Ben, 2026-10-17, on Claude's recommendation. World tags releases from `main` as
`vMAJOR.MINOR.PATCH`, with a GitHub release that says what changed. `v0.1.0` is
the first. Teamworld pins one release, as a git submodule checked out at the
tag, so it builds against a fixed World and moves only when it chooses to. That
needs no packaging and no build step. When teamworld needs a change in World,
make it here, release it, and move teamworld's pin.

Teamworld follows the same constraint as World: the Python standard library and
plain browser JavaScript, with no packages or build step without Ben's
agreement. An OAuth server needs only hashing and random tokens, which the
standard library has.

### Access lasts until it is revoked

Ben, 2026-10-17, on Claude's recommendation. Giving an agent access creates a
grant that stands until someone revokes it. Its tokens expire and are renewed
under it without an audit line. Before this, access lasted only while an
unexpired token existed, so an agent renewing after a quiet weekend added a
`gave access` line the person never made, and the audit of access stopped being
true. An authorization server renews with `"renew": true`, which refuses when
the access was revoked. To be released as `v0.1.1`.

### The authorization server adds the people it invites

Ben, 2026-10-18, on Claude's recommendation. A token can only be issued for a
person already in the world, so an authorization server needs a way to add the
people it invites. `POST /api/members`, with the issuer key, creates them as
someone already in the world, without asking the type's create rule. The
alternative, letting each world's own rules allow members to create people,
would leave every world's author to get it right. Field write rules still
apply, so inviting cannot grant what the inviter could not.

### A World runs no model unless its host asks

Ben, 2026-10-18, on Claude's recommendation. The reference client's planner
runs Codex on the server, on the host's account, for anyone with a token. That
was safe while World ran only for Ben, but a hosted world, such as each of
teamworld's teams, would let any member spend the host's account. The planner
now runs only when the server is started with `--codex` or with
`WORLD_CODEX_BIN` set, as `scripts/install_service.py` does. Released in
`v0.1.2`.

### A world's definition changes while it runs

Ben asked for it on 2026-10-20, from Claude's design in
`docs/changing-a-world.md`. The database holds the definition and every
revision. The `--world` file only creates the world, and a changed file is
applied as a change by `system`. A `define` rule in the definition says who
may change it, and without one no one may. An agent changes it with the
`define` tool. A change that would leave any live record invalid is refused,
naming the records, and nothing is saved. There is no migration language:
values the engine cannot know are filled in by the agent with `change`, so
each is in the audit. The design's five open decisions were taken as it
proposed them, and Ben may revisit them.

### Order of improvements

Ben, 2026-10-13. After the third experiment, work in this order:

1. State each rule once, so the engine both enforces and describes it.
2. Run the comparisons with a second agent, to see whether the result belongs to
   World or to one model.
3. Let an agent do more in one call.
4. Test at enterprise size. Agreed 2026-10-13: the target is about 200 types, a
   million records, and 50 agents at once. Do it locally in three stages: index
   the engine, build a world of that size, then test many agents at once.
   Bringing the control to the same size is a fourth stage that needs Ben's
   decision on where it runs. All four stages are done, on this machine
   (2026-10-14). At a million records and 201 types, an agent needed 61 calls on
   World and 134 on EspoCRM, in 170 s against 273 s. See
   `docs/fourth-experiment.md`.

### The real dataset is GitHub issues

Ben, 2026-10-14. Step 1 of the plan uses `open-index/open-github-issues` from
Hugging Face (ODC-By 1.0, so the figures and any copy must credit it). Of the
three candidates it was on Hugging Face, permissively licensed, real, and shaped
like the team tool the demo will be. The Public Jira Dataset (CC BY 4.0, on
Zenodo) and Olist (CC BY-NC-SA) were the alternatives. Seven repositories make
about 900,000 records in the `tracker` world. See `docs/fifth-experiment.md`.

### World is the system of record, and provides no LLM

Ben, 2026-10-08. World holds records, rules, and history in the simplest and
most effective way it can. People bring their own agents to read, visualise, and
change a world. Fast hosted models make interface generation someone else's
solved problem, so the durable value is in the record layer.

- Do not propose bundling, hosting, or choosing a model as part of World.
- `client_gateway` and `client` are a reference client and test harness. They
  show that an agent can use a world. They are not the product.
- The aim is a database reimagined for agents. Existing databases assume a
  developer has read the schema and written an application around it.

### The proof is a comparison against an open-source CRM

Ben, 2026-10-08. A traditional application of roughly 100 MB is the control. The
same domain as a World should be much smaller and faster, and later experiments
work on improving both. The long-term picture is an enterprise-sized system that
fits on a USB key.

- The control must be an existing open-source CRM, not one written here.
- The control is EspoCRM, chosen by Claude on 2026-10-09 because each user gets
  an API key and a role, so the tasks run as the same people with the same
  limits. `bench/control/` starts and seeds it. See `docs/second-experiment.md`.
- On the CRM, which the agent already knew, World and EspoCRM were level on
  success, calls, and time (2026-10-12). On a made-up calibration lab, both got
  every task right and World used half the calls and 58% of the time
  (2026-10-13). Claude Code gave the same result on the lab: half the calls, 71%
  of the time. World's advantage is in domains an agent has not seen, in size,
  and in refusing wrong requests. See `docs/third-experiment.md`.
- On real GitHub issues, about 900,000 records, all three agents were at least
  as correct on World (2026-10-16). Codex and Claude Code needed 52% and 33% of
  the calls. A weaker free model, MiMo, got 60 of 60 on World and 14 of 25 on
  EspoCRM, mostly timing out. See `docs/fifth-experiment.md`.
- An agent that had never seen World wrote worlds for three new domains from
  plain-language briefs, and a fresh agent did all 30 tasks on them
  (2026-10-17). World enforced every rule but one: the rule language has no
  date arithmetic. See `docs/sixth-experiment.md`.
- Agent runs spend the user's model account, so ask first.
- Report size both as the shipped artifact alone and with everything it needs
  to run.

### Measure what the record service controls

Agreed 2026-10-08, proposed by Claude. Model speed is outside World's boundary.
The comparison measures what an agent pays to use the records:

1. Discovery cost: how much an agent reads to learn what exists and what it may do.
2. Round trips per task.
3. Cold success rate: how often an agent with no documentation gets a task right.
4. Recoverability: whether a rejected write says enough to correct it in one more call.

Change the contract before the storage engine. Replace SQLite only when a
measurement shows it is the limit.

### Persistent identities are separate from software connections

Ben approved the Social experiment recommendations on 2026-10-29. An issuer may
bind a persistent `identity` to a person's software grant. Rules see trusted
`caller.identity` and `caller.agent`; display names confer no authority. The
binding cannot change while the grant stands. History records the person,
software and identity. Definitions can use `$identity` defaults, but create rules
must still reject forged authorship. This is generic engine functionality;
Social's identities, accounts and definition belong in Teamworld.

## Layout

- `world/` is the engine: storage, validation, versions, history, the HTTP
  protocol, and MCP at `/mcp` (`world/mcp.py`), whose tools call the same code
  as the HTTP API. It must not name a type, role, or rule from any one domain.
- `worlds/` holds one definition per domain: `team` (the default), `crm`,
  `lab`, `enterprise` (the lab plus 197 generated types, for scale tests), and
  `tracker` (real GitHub issues, for step 1 of the plan). A definition is data. Rules are expressions the engine evaluates and
  shows to clients, so never enforce a rule in Python. Extend the language in
  `world/rules.py` if a rule cannot be written. See `docs/writing-a-world.md`. `python3 -m bench --domain lab` selects the lab's tasks.
- `python3 -m bench.scale` builds and times the million-record world in
  `artifacts/bench/scale`. It measures on a copy. Never write to the built
  database, because the agent tasks start from it.
- `python3 -m bench.github` downloads the GitHub data, builds the tracker world
  in `artifacts/bench/tracker`, and checks it against the world's constraints.
  The same rule applies: never write to the built database. `bench/parquet.py`
  reads the Parquet files with the standard library.
- `bench/control/tracker.py` builds the tracker's control in a second EspoCRM
  on port 8792 (`docker compose -p world-control-tracker`). See
  `docs/fifth-experiment.md`.
- `docs/results/` keeps the raw output behind every figure in the experiment
  documents. `artifacts/` is scratch space and may be deleted. When a write-up
  cites a run, copy its file into `docs/results/` and list it in that README.
- `bench/agents/` holds the commands for agents other than the default Codex.
- `bench/` measures discovery cost, round trips, and recovery. Run
  `python3 -m bench` after any protocol change and compare with the figures in
  `docs/second-experiment.md`. `python3 -m bench agent` spends the user's model
  account, so run it when the change warrants it.
- `client/` and `client_gateway/` are the reference client.
- `experiment/crm-world` is a separate line of work from 2026-10-04, checked out
  at `/home/ben/projects/world-crm-experiment`. It is a reference, not to be
  merged. `docs/crm-world-branch.md` records what it found.

## Constraints

- World contains no UI definitions. See the design constraint in the README.
- The record service and client use only the Python standard library and plain
  browser JavaScript. Do not add a package or build step without asking.
- `world-experiment.service` runs the team world from this checkout on port 8787,
  with data in `~/.local/share/world`. `world-crm-experiment.service` (8788) and
  `world-agent-experiment.service` (8789) run from the `experiment/crm-world`
  worktree. Tests and experiments use temporary data directories and other
  ports, and never touch these services or their data.
