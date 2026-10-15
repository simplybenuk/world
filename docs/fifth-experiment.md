# Fifth experiment: real data

In progress. This is step 1 of [the plan](plan.md). The earlier experiments used
seeded or generated records. This one loads real public data, so the text,
distributions, and mess are those of a working team. So far World's side is
built and measured without a model, the EspoCRM control holds the same
records, and a third agent is ready. The agent runs are still to do.

## The data

`open-index/open-github-issues` on Hugging Face holds every issue, pull request,
and comment of a number of public GitHub repositories, as Parquet files. Its
licence is the Open Data Commons Attribution License 1.0, so any use must credit
it. The original content is also covered by GitHub's terms. Ben chose it on
2026-10-14 over the Public Jira Dataset and Olist.

Seven repositories were loaded, 175 MB of Parquet files downloaded on 2026-10-14:

| Repository | Issues and pull requests | Comments |
| --- | --- | --- |
| facebook/react | 34,030 | 138,652 |
| microsoft/TypeScript | 62,281 | 286,771 |
| redis/redis | 14,823 | 53,958 |
| etcd-io/etcd | 21,262 | 89,131 |
| duckdb/duckdb | 19,108 | 46,584 |
| vuejs/core | 12,251 | 32,769 |
| vuejs/docs | 3,283 | 4,909 |

```sh
python3 -m bench.github download
python3 -m bench.github build
python3 -m bench.github verify
python3 -m bench.github measure
python3 -m bench --domain tracker
```

## The world

`worlds/tracker.py` holds six types:

| Type | From | Records |
| --- | --- | --- |
| Person | every login that opened, commented on, or was assigned an issue | 76,615 and the administrator |
| Project | each repository, with its maintainers | 7 |
| Label | the labels on its issues | 485 |
| Milestone | the milestones on its issues | 301 |
| Issue | issues and pull requests, with state, close reason, assignees, labels, milestone, lock, and dates | 167,038 |
| Comment | comments, with author, text, and date | 652,161 |

The rules follow GitHub's. Anyone may open an issue or comment. A project's
maintainers label, assign, plan, and lock its issues. An author may edit and
close their own issue. Only maintainers may comment on a locked issue. Eight
constraints keep an issue consistent. For example, a closed issue needs a
close reason, only a pull request can be merged, and its labels and milestone
must belong to its project.

The loader writes the records straight into the database, with a history event
for each that names its source on GitHub. Then `verify` judges every issue by
the world's eight constraints through the engine. All 167,038 pass.

### What the data needed

- **Maintainers.** The dataset does not list them. A person is a maintainer of a
  project if any of their comments there carries GitHub's `OWNER`, `MEMBER`, or
  `COLLABORATOR` association. That gives 20 to 151 maintainers a project. It
  misses maintainers who never commented, and it uses each person's association
  at the time of the download, not when they commented.
- **Merged pull requests.** The `merged` flag is wrong: none of react's 19,672
  pull requests carries it. A pull request with a `merged_at` time is loaded as
  merged.
- **Gaps.** 613 comments belong to issues missing from the dataset and were left
  out. Issue numbers have gaps where GitHub issues were deleted or moved.
- **Reopened issues.** 188 open issues carry the reason `reopened`. They are
  loaded as open, with no close reason.
- **Long text.** Titles reach 754 characters and bodies exceed GitHub's own
  limit of 65,536. The limits are 1,024 and 262,144 characters.
- **Deleted accounts.** GitHub shows every deleted account as `ghost`, which
  is one person here with 3,098 issues.
- **Damaged text.** Some bodies contain stray control characters, such as a
  bell character, that look like damage in the source. They are loaded as
  they are.
- **Bots.** Logins ending in `[bot]` are marked as bots. Others, such as
  `Copilot`, are not.

The data needed one new field type, `boolean`, for whether an issue is locked.

## Size

The database is 3,017 MB for 896,608 records, 3,365 bytes a record. It builds
in 104 seconds. The records are larger than the generated ones in [the fourth
experiment](fourth-experiment.md) (1,394 bytes) because they hold real text.

| Part | Size |
| --- | --- |
| Records | 1,098 MB |
| History | 1,413 MB |
| Field indexes | 183 MB |
| Reference index | 236 MB |
| Other indexes | 33 MB |

History is the largest part, because each creation event holds a full copy of
the record. The plan already lists making history compact.

## Single operations

Medians on a copy of the database, called directly on the engine.

| Operation | Time |
| --- | --- |
| Describe the world | 10 ms |
| Read one issue by ID | 7 ms |
| Find an issue by project and number | 8 ms |
| Comments on one issue | 11 ms |
| Comment on an issue | 9 ms |
| Label an issue | 18 ms |
| Add a maintainer to a project | 138 ms |
| Audit lines for one issue | 2 ms |
| First 100 of one author's issues | 77 ms |
| Count open issues in one project | 182 ms |
| Count issues with one label in one project | 596 ms |
| Search titles in one project | 593 ms |
| Search titles in every project | 5,755 ms |

The weak spots are the ones the fourth experiment found: counting and searching
judge every candidate in Python. A search across all 167,038 issues takes about
six seconds.

### A write that took 70 seconds

Before this experiment, adding a maintainer to redis took 70 seconds and held
the world's write lock throughout. The engine re-checked the constraints of
all 14,823 issues that refer to the project, although none of those
constraints reads `maintainers`. The engine now works out which fields each
constraint can read, and skips the re-check when a change touches none of them.
The same write takes 138 ms. A constraint that does read the changed field is
still re-checked: moving a label to another project is refused while issues
carry it.

## The tasks

Twenty tasks, asked in plain words by three real people from the data:

- sundb maintains redis.
- ORESoftware has opened issues in TypeScript, redis, and react, and maintains
  none of them.
- edison1105 maintains vuejs/core.

| Kind | Tasks |
| --- | --- |
| Reading | an issue's title and state; who opened a pull request and when it was merged; an issue's labels |
| Counting | open issues with a label; one person's issues, and how many are open; issues assigned to me; a milestone's issues, and how many are open |
| Searching | my issue from a description; an open issue from a description; who mentioned a word in an issue's comments |
| Single writes | comment; label; close as not planned; open an issue |
| Related writes | assign, label, and comment at once; close as a duplicate and comment; create a milestone and put two issues in it |
| Refusals | comment on a locked issue; label another project's issue; assign an issue as its author but not a maintainer |

Every answer was read from the data. `bench/tracker.py` holds the prompts, a
scripted solution for each, and a checker. The scripted solutions answer all
twenty correctly in 57 calls. Each checker fails on an untouched world with an
empty answer.

## The third agent

`bench/agents/opencode.sh` runs opencode with NVIDIA's Nemotron 3 Ultra, an
open-weights model from a third family beside Codex's and Claude's. It uses
opencode's free tier, so its runs spend no one's account.

```sh
python3 -m bench agent --domain tracker --command bench/agents/opencode.sh --label opencode --repeat 3
```

As for Claude Code, the shell is limited to curl and text filters, and the
agent gets a home directory of its own, so no user configuration or skills
reach it. Four things differ:

- **Reading files.** The free tier refuses requests without opencode's file
  reading tool, so that tool stays. It cannot reach outside the agent's empty
  workspace.
- **Writing in the workspace.** A shell redirect such as `echo x > file` is
  allowed. The workspace is thrown away after each run.
- **Model choice.** The free models change from day to day. A different model
  can be set with `WORLD_OPENCODE_MODEL`. Name the model with any result.
- **Where prompts go.** The free tier sends prompts to opencode's provider. The
  prompts hold public data, a local address, and a token for a world that exists
  only during the run.

The Gemini CLI was tried first. It has no key on this machine, and Google's
Antigravity CLI, which is signed in, cannot limit its tools.

A trial of two tasks gave a correct label. The checker marked the other run
wrong although the answer was right: it described issue 3067 instead of quoting
its title. The checker now accepts either.

## The control

EspoCRM 10.0.10 holds the same records in a second instance on port 8792, so
that its description holds only the tracker. `bench/control/tracker.py` builds
it through the admin API, as an administrator would:

- **Entities.** Six custom entities with the same fields. Labels, assignees, and
  maintainers are many-to-many links with list fields.
- **Maintainers.** Each project is a team, and its maintainers are members. A
  Maintainer role lets them change their team's issues, labels, and milestones.
- **Contributors.** A Contributor role may open issues and comment. Field-level
  rights stop it changing assignees, labels, milestones, and locks.
- **Rules.** Before-save scripts hold the eight issue constraints and the rule
  for locked issues. Each refusal was checked to refuse the same request as
  World.

```sh
WORLD_CONTROL_PORT=8792 docker compose -p world-control-tracker -f bench/control/compose.yaml up -d
python3 -m bench.control.tracker setup
python3 -m bench.control.tracker load
python3 -m bench control --domain tracker
```

The loader writes the records `bench.github` produces straight into MariaDB, in
11.5 minutes: 167,038 issues and 652,161 comments, the same as World. It keeps a
copy of the tables a task can change, so each task starts from the loaded state.
A reset takes about 20 seconds.

Three things differ from World:

- **Authors.** World fills in the author from the token. In EspoCRM the client
  must name the author, and the checker accepts either the named author or the
  user who created the record.
- **An author's own issue.** World lets an author edit and close their own
  issue. EspoCRM's "own" means the assigned user, which the loaded issues do not
  have, so authors cannot. No task depends on it.
- **Titles.** EspoCRM indexes an entity's name, and MariaDB's key limit allows
  760 characters. The longest title is 754.

| | World | EspoCRM |
| --- | --- | --- |
| Self-description | 8.7 KB, about 2,200 tokens | 198 KB, about 49,000 tokens |
| Data | 3,017 MB, with a history event for every record | 1,909 MB, with no history for the loaded rows |
| Software | 118 KB, plus Python's 66 MB | 1.53 GB as two container images |
| Memory while idle | 30 MB | 502 MB across two containers |

Both sides answer all twenty tasks correctly by script:

| | World | EspoCRM |
| --- | --- | --- |
| Calls, scripted shortest path | 57 | 74 |
| Time, all twenty | 0.9 s | 96.8 s |

The World count includes reading its description once per task, and
EspoCRM's does not. Most of EspoCRM's time is finding an issue by its number.
That takes about 6.5 seconds, probably because EspoCRM sorts every list by
creation time and does not index the number. That is how it ships, so an agent
will pay the same cost.

## Still to do

The runs: each agent, each task, three times, on both sides.

## Raw results

In `docs/results/`: `build-tracker.json`, `verify-tracker.txt`,
`measure-tracker.txt`, `report-tracker.json`, and `control-tracker.json`.
