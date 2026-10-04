# World

World explores systems of record whose primary users are agents.

A person works through their own agent in ChatGPT, Claude, Codex, or another
client. The service holds accepted data, its meaning, relationships, rules and
history. An application website can provide connection and account management;
it does not need to provide the everyday interface to the records.

The current direction is a **governed, discoverable model of a domain**. An agent
should be able to find what exists, request the information it needs, inspect
where that information came from, and propose a valid change. The service
checks permissions and accepts the change atomically.

This repository contains runnable prototypes and their findings. It is not a
production database or a settled protocol.

## The hypothesis

An agent may need fewer calls and less context when the system describes its
meaning and valid operations directly. That could reduce integration work and
improve correctness. It does not establish that a new storage engine would be
faster than existing databases.

We are testing three related ideas:

- Types, field meanings, relationships and enforceable rules are discoverable.
- Accepted facts and changes have history and provenance. Current records can
  be projections of that history.
- An agent can request a bounded selection of related facts and propose an
  atomic change with explicit preconditions.

The server remains responsible for validity. An agent's inference or proposal
is not an accepted business fact.

For example, an agent could request Northstar's open opportunities, their
owners, latest interactions and permitted next stages in one structured read.
It could then propose advancing a renewal to proposal, conditioned on the
record still having the version it read.

The same model can support a CRM or a project world. Domain definitions do not
contain pages, forms, dashboards, or navigation.

## Experiments

| Experiment | Decision tested | Evidence |
| --- | --- | --- |
| [1. Persistent records and a temporary client](docs/first-experiment.md) | Can a client discover governed records and propose changes? | [First findings](docs/first-experiment-findings.md) |
| [2. A CRM on the shared engine](docs/second-experiment.md) | Can another domain run from a separate definition? | [Second findings](docs/second-experiment-findings.md) |
| [3. A headless fact World](docs/third-experiment.md) | Can joined reads, temporal facts and atomic changes improve agent interaction? | [Third findings](docs/third-experiment-findings.md) |

The first two browser clients choose among fixed layouts using model-generated
view plans. They established persistence and reuse across definitions. They did
not establish that an entire interface appears from a prompt. The third
experiment moves the primary interaction into an agent and provides a headless
record service.

The comparison includes the existing API's bulk read. On the small CRM,
both real agents answered correctly with discovery and one bulk read. Joined
selections reduce the data returned for targeted questions, while the current
fact-store prototype uses more storage. The findings record the measurements
and failed runs.

## Run the headless experiment

Use Python 3.11 or newer. The scripted comparison does not require a model:

```sh
python3 -m experiments.agent_world.benchmark --output artifacts/agent-world/benchmark.json
```

For the real agent comparison, install the Codex CLI and sign in, then run:

```sh
python3 -m experiments.agent_world.benchmark --live --trials 1 --output artifacts/agent-world/live.json
```

Both commands use disposable fictional CRM databases. Results include the
comparison scope and measured quantities. The [run guide](docs/third-experiment.md)
describes the protocol, persistence scenarios and limits.

To keep a separate fictional World available to an agent:

```sh
python3 -m experiments.agent_world.server --data-dir .world/agent-world --port 8789
```

In another terminal:

```sh
python3 -m experiments.agent_world.server access --data-dir .world/agent-world --port 8789 --actor ben
```

The access command prints a private bearer credential. Keep it outside Git.
The service root returns a short service description. Its authenticated
`/api/world` discovery and JSON operations are the working interface.

## Repository map

- `world/` is the record engine from experiments one and two.
- `definitions/` contains the team and CRM definitions.
- `experiments/agent_world/` contains the isolated third prototype, transport,
  comparison and bounded agent driver.
- `client/` and `client_gateway/` contain the earlier browser experiment.
- `docs/` records how to run each experiment and what it established.

The [definition format guide](docs/world-format-prototype.md) covers experiment
two. The third experiment adds a candidate read and transaction contract; it
has not replaced that definition format with a new standard.

## Existing ideas and open questions

Temporal fact databases already explore immutable facts and historical queries.
[Datomic's information model](https://docs.datomic.com/datomic-overview.html) is
relevant prior work. [MCP](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
can expose a service's tools without deciding the underlying record model.

World's open question is whether a shared description and governed operations
make systems of record easier for agents to use across domains. Storage size,
query cost, model context, calls and correctness all matter. The experiments
measure bounded cases and preserve failures; they do not establish production
scalability or interoperability with every agent client.
