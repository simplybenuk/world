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
building or hosting a public demo before Ben says the evidence is enough.

### Order of improvements

Ben, 2026-10-13. After the third experiment, work in this order:

1. State each rule once, so the engine both enforces and describes it.
2. Run the comparisons with a second agent, to see whether the result belongs to
   World or to one model.
3. Let an agent do more in one call.
4. Test at enterprise size. This may need real infrastructure for World and for
   any control, so plan it with Ben before building.

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

## Layout

- `world/` is the engine: storage, validation, versions, history, and the HTTP
  protocol. It must not name a type, role, or rule from any one domain.
- `worlds/` holds one definition per domain: `team` (the default), `crm`, and
  `lab`. A definition is data. Rules are expressions the engine evaluates and
  shows to clients, so never enforce a rule in Python. Extend the language in
  `world/rules.py` if a rule cannot be written. See `docs/writing-a-world.md`. `python3 -m bench --domain lab` selects the lab's tasks.
- `bench/` measures discovery cost, round trips, and recovery. Run
  `python3 -m bench` after any protocol change and compare with the figures in
  `docs/second-experiment.md`. `python3 -m bench agent` spends the user's model
  account, so run it when the change warrants it.
- `client/` and `client_gateway/` are the reference client.

## Constraints

- World contains no UI definitions. See the design constraint in the README.
- The record service and client use only the Python standard library and plain
  browser JavaScript. Do not add a package or build step without asking.
- A systemd user service may be running the team world from this checkout with
  data in `~/.local/share/world`. Tests and experiments use temporary data
  directories and never that one.
