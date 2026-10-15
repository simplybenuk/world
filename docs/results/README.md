# Results

The raw output behind the figures in the experiment write-ups. Each agent file
holds one entry per task run: whether it was correct, every request the agent
made with its status and size, the agent's answer, and, for World, the audit
lines it left. Agent files are named `agent-DOMAIN-[control-]AGENT.json`;
`control` means EspoCRM. The `report` and `control` files are measurements
taken without a model.

Run commands and write-ups are in the experiment documents. The files were
copied from `artifacts/bench` on 2026-10-14. Dry runs and one-task trials are
left out. A later run with the same label replaced an earlier one in two cases,
noted below.

## Second experiment: a CRM, and EspoCRM as the control

| File | What it is |
| --- | --- |
| `report-baseline.json` | World's CRM measured before the protocol change |
| `agent-codex-baseline.json` | Codex on World's CRM before the protocol change, one run per task |
| `report.json` | World's CRM measured after the protocol change |
| `agent-codex.json` | Codex on World's CRM, three runs per task, 2026-10-12 |
| `agent-control-codex.json` | Codex on EspoCRM's CRM, three runs per task, 2026-10-12 |
| `control.json` | EspoCRM's size, metadata, scripted tasks, and response to ten mistakes |

The single Codex run after the protocol change (5 of 5 correct, 17 calls) was
replaced by `agent-codex.json`. Its figures survive only in
[the second experiment](../second-experiment.md).

## Third experiment: a domain the agent has never seen

| File | What it is |
| --- | --- |
| `agent-lab-codex.json` | Codex on World's lab |
| `agent-lab-control-codex.json` | Codex on EspoCRM's lab |
| `agent-lab-claude.json` | Claude Code on World's lab |
| `agent-lab-control-claude.json` | Claude Code on EspoCRM's lab |
| `agent-lab-codex-rules.json` | Codex on World's lab after rules became expressions |
| `agent-lab-codex-onecall.json` | Codex on World's lab with the new query and batch operations |
| `agent-lab-claude-onecall.json` | Claude Code on the same |
| `agent-crm-claude-onecall.json` | Claude Code on World's CRM with text search added |
| `report-crm.json`, `report-lab.json` | Both worlds measured without a model, after the new operations |

`agent-crm-claude-onecall.json` is the run with text search (47 calls). It
replaced the run without it (67 calls, 15 rejected), whose figures are only in
[the third experiment](../third-experiment.md).

## Fourth experiment: a company-sized world

| File | What it is |
| --- | --- |
| `agent-enterprise-claude.json` | Claude Code on World's million-record world. Its three "adjusted" runs were marked wrong by a polluted database |
| `agent-enterprise-claude-adjusted.json` | The "adjusted" task re-run on a clean build |
| `agent-enterprise-control-claude.json` | Claude Code on EspoCRM with the same million records |
| `control-enterprise.json` | EspoCRM's size, memory, metadata, and scripted tasks at that size |

## Fifth experiment: real data

| File | What it is |
| --- | --- |
| `build-tracker.json` | What the loader wrote from the GitHub data, and what it adjusted |
| `verify-tracker.txt` | Every loaded issue judged by the tracker world's constraints |
| `measure-tracker.txt` | Size and single operations on the tracker world |
| `report-tracker.json` | The twenty tracker tasks solved by script, without a model |
