# Run the first experiment

World keeps a small team's records in SQLite. A browser client discovers their
types and permissions, asks Codex for a temporary view, and lets you review a
proposed edit before saving it. Reloading the browser discards the view and
conversation. Saved records and their history remain.

## Start the server

The record service and browser client need Python 3.11 or newer. They use the
Python standard library and plain browser JavaScript, with no package install or
build step. AI view generation also needs the Codex CLI on the server, signed in
as the same operating-system user that runs World.

Run these commands from the repository directory:

```sh
python3 --version
codex login status
python3 -m world.server
```

The server listens on `127.0.0.1:8787` by default. The first start creates the
team records and writes data under `~/.local/share/world`. Further starts reopen
that database without resetting it.

World uses the server user's existing Codex login. If the login check reports
that you are signed out, run `codex login` and complete the ChatGPT sign-in flow.
For a server without a browser, `codex login --device-auth` is another sign-in
option. See the [official OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth).

Leave the server running. In another terminal, from the same repository, run:

```sh
python3 -m world.server access --actor ben
```

Open the private URL it prints. Its `#token` fragment signs you in as Ben. The
client removes the fragment from the address bar and uses an HTTP-only session
cookie. Treat the whole URL as a credential. Keep it out of commits, screenshots,
shared logs, and tickets.

For an isolated trial, give both commands the same `--data-dir`:

```sh
python3 -m world.server --data-dir /tmp/world-trial --port 8788
```

```sh
python3 -m world.server access --data-dir /tmp/world-trial --port 8788 --actor ben
```

If you connect through a different address, supply that browser address to the
access command with `--url http://your-server-address:8787`. The service installer
below records the address so later access commands can use it automatically.

## Try the four requests

Start as Ben with the fresh client. Send these requests in order:

1. `Show me what needs attention.`
2. `Make this more visual.`
3. `Show the website project instead.`
4. `Mark the documentation task as complete.`

Each request sends the discovered schema, records you can see, and recent
conversation context to Codex. The model selects a view's record type, record
IDs, fields, and layout. The client renders that plan. Exact titles, wording,
and record selections can vary between requests.

The second request should change how the same work appears. The third should
focus on the website project. These requests change only the client view.

The fourth should produce a proposed change to the `documentation` task, setting
`status` to `done`. Review the record name and changed field, then click
**Confirm change**. Wait for the saved confirmation. Generating a proposal alone
does not update a record. **Dismiss** leaves it unchanged.

Open the documentation task and choose **History**, or open **Activity** and its
**Record history** tab. The accepted edit records who changed it, the reason,
the previous state, and the new state. The protocol also accepts evidence
references.

Close the tab and open the client again. You get a fresh view and conversation.
Choose **Tasks** under **Record types** and open **Write documentation**. Its
status should still be `done`, with the saved edit in its history. Opening a
second client produces the same result.

If a proposed edit conflicts with a newer version, the server rejects it. Reload
the client and ask for a new proposal using the current record.

## Try another person's permissions

The initial actors are:

| Actor | Role | Access |
| --- | --- | --- |
| `ben` | Admin | All initial records; creates, edits, and permitted deletions |
| `maya` | Member | Website work; edits and creates within that project |
| `alex` | Member | Website and private World work; edits and creates in both |
| `sam` | Viewer | Team-visible website work; reads only |

Sign out, then print and open Sam's private link:

```sh
python3 -m world.server access --actor sam
```

Sam can browse the website project and its tasks. The private World project
and its tasks do not appear. Asking Sam's client to complete documentation
should explain the permission limit and produce no change. The record service
also rejects direct write requests authenticated as Sam.

The stored `Person.role` and project membership determine permissions. An actor
name or role in a request body or custom header does not change who is signed in.

## Keep it running as a service

On a Linux server with a user systemd session, run:

```sh
scripts/install-service.sh --host 127.0.0.1 --port 8787
```

Use the server's trusted network address for `--host` when you want to connect
from another machine. Stop a foreground server using the same port first.
If a proxy supplies the browser URL, add `--url https://your-browser-address`.

The installer writes `~/.config/systemd/user/world-experiment.service`, enables
it, and restarts it. It uses the repository as its working directory, the current
Python executable, and the installed Codex executable. It saves the browser URL
in the data directory's `client-url` file.

```sh
systemctl --user status world-experiment.service
python3 -m world.server access --actor ben
journalctl --user -u world-experiment.service -n 50
```

The status should be `active`. The access command should print a link using the
installed service's URL. For a service using another data directory, pass the
same `--data-dir` to the access command.

To restart or stop the service:

```sh
systemctl --user restart world-experiment.service
systemctl --user stop world-experiment.service
```

## Protocol reference

All record operations require a server-issued token. Browser requests use the
session cookie; other clients can send `Authorization: Bearer TOKEN`. Request
bodies must be JSON objects with `Content-Type: application/json`.

| Method and path | Request or result |
| --- | --- |
| `GET /health` | Public service status and whether the Codex executable is available |
| `POST /api/session` | Sign in with `{"token":"TOKEN"}` and receive a session cookie |
| `GET /api/session` | Authenticated actor record |
| `DELETE /api/session` | Clear the browser session cookie |
| `GET /api/world` | Actor, types, fields, rules, capabilities, query operators, and the operations in this table |
| `GET /api/types/Task` | Description of one type |
| `GET /api/records?type=Task` | Visible records of one type; omit `type` for all visible records |
| `POST /api/query` | `{"type":"Task","filters":[{"field":"status","op":"ne","value":"done"}]}` |
| `GET /api/records/Task/documentation` | One visible record, including version and capabilities |
| `POST /api/records/Task` | Create with `data`, `reason`, and optional `evidence` |
| `PATCH /api/records/Task/documentation` | Update with `changes`, `expected_version`, `reason`, and optional `evidence` |
| `DELETE /api/records/Task/ID` | Delete with `expected_version`, `reason`, and optional `evidence` |
| `GET /api/history/Task/documentation` | Ordered audit events in `events` |
| `GET /api/audit` | Plain-text audit of the records you can read, one line per event; `?limit=N` returns the latest N, 500 by default |
| `GET /api/audit/Task/documentation` | The same lines for one record |

Types are `Person`, `Project`, `Task`, and `Comment`. References contain plain
record IDs, such as `"website"`, rather than `"Project#website"`. A record contains
`id`, `type`, `data`, `version`, timestamps, and capabilities. Task status accepts
`todo`, `doing`, or `done`. Dates use `YYYY-MM-DD` or `null` where allowed.

Query filters combine with AND. Supported operators are `eq`, `ne`, `contains`,
`lt`, `lte`, `gt`, `gte`, and `in`; their values must match the field's type.
The schema returned by `/api/world` describes field constraints and permissions.
The service checks relationships and permissions again for every mutation.

History events use `action`, `at`, `actor`, `requested_by`, `reason`, `evidence`,
`before`, and `after`. The before and after states contain the record's fields
under `data`. A deletion retains the record's history.

The audit is the same history as text a person can read, with views included.
Each line gives the time, the actor, what they did, the record, and the detail:

```text
2026-10-09T06:32:22.939Z  priya  viewed  Opportunity harbor-pilot  Found by a query.
2026-10-09T06:32:22.941Z  priya  changed  Opportunity harbor-pilot  stage from "proposal" to "lost"; lost_reason from null to "Budget frozen.". Reason: Harbor Logistics froze their budget.
```

The service writes a view line whenever it returns a record from a read, a list,
a query, or a history request. The line commits with the read. View and event
rows cannot be updated or deleted. Values longer than 120 characters are
shortened in the audit and kept in full in the history. Reading the audit does
not add a line.

Software acting for a person should use its own token. This prints one:

```sh
python3 -m world.server access --actor ben --agent codex
```

The token has Ben's permissions. The audit records what it reads and changes as
`ben via codex`, and history events carry the name in `agent`. The name comes
from the token, so a request cannot claim a different one. Records that the
client gateway sends to a model appear as `via client-gateway`.

Errors return an `error` object with a `code` and `message`. Malformed input
returns 400, missing authentication 401, denied access 403, and a missing record
404. Invalid fields or relationships return 422. Missing or invalid mutation
versions return 428; a stale version or blocked deletion returns 409. Read the
current record and review a new change before retrying a conflict.

### Use the record protocol with curl

Set the URL to the service you are using. Paste only the token portion of your
private sign-in URL when prompted. These Bash examples keep the credential out of
shell history:

```bash
export WORLD_URL=http://localhost:8787
read -r -s -p 'World token: ' WORLD_TOKEN
printf '\n'
export WORLD_TOKEN

curl --fail --show-error "$WORLD_URL/api/world" \
  -H "Authorization: Bearer $WORLD_TOKEN"

curl --fail --show-error "$WORLD_URL/api/types/Task" \
  -H "Authorization: Bearer $WORLD_TOKEN"

curl --fail --show-error "$WORLD_URL/api/records?type=Task" \
  -H "Authorization: Bearer $WORLD_TOKEN"

curl --fail --show-error "$WORLD_URL/api/query" \
  -H "Authorization: Bearer $WORLD_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"type":"Task","filters":[{"field":"project","op":"eq","value":"website"},{"field":"status","op":"ne","value":"done"}]}'
```

This next request saves a real edit. Read the current version before sending it:

```bash
WORLD_VERSION="$(curl --fail --silent --show-error \
  "$WORLD_URL/api/records/Task/documentation" \
  -H "Authorization: Bearer $WORLD_TOKEN" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["record"]["version"])')"

curl --fail --show-error -X PATCH "$WORLD_URL/api/records/Task/documentation" \
  -H "Authorization: Bearer $WORLD_TOKEN" \
  -H 'Content-Type: application/json' \
  --data "{\"changes\":{\"status\":\"done\"},\"expected_version\":$WORLD_VERSION,\"reason\":\"Completed the first experiment\",\"evidence\":[\"Manual verification\"]}"

curl --fail --show-error "$WORLD_URL/api/history/Task/documentation" \
  -H "Authorization: Bearer $WORLD_TOKEN"

unset WORLD_TOKEN WORLD_VERSION
```

### Client gateway

`POST /client/intent` accepts `message` and optional `context` containing the
previous `view` and `conversation`. It returns `message`, `view`, `proposals`, and
`provider`. A view selects `layout`, `type`, `record_ids`, `fields`, and optional
`group_by`, with a title and description. Supported layouts are `list`, `board`,
`table`, and `overview`.

A normalized proposal contains `operation`, `type`, `id`, `changes`,
`expected_version`, and `reason`. After review, `POST /client/apply` accepts
`{"proposal":PROPOSAL}` with optional `reason` and `evidence`. The record service
enforces the same permissions, validation, and version checks as direct record
requests. Proposal generation does not save changes.

## Boundaries and verification

This is a prototype for a small trusted network. Tokens are local credentials,
and access links grant the permissions of their associated actors. The default
server binds to loopback; a trusted network or an HTTPS proxy supplies remote
access.

AI generation uses the server's Codex account and cloud inference. It sends
only the authenticated actor's visible records, but those records and recent
conversation contents leave the server for generation. Requests can take tens
of seconds, hit account usage limits, or time out. An unavailable provider
returns an error instead of a simulated view. You can still browse records and
use the discovered create and edit forms.

`/health` checks for the Codex executable. A successful AI request also verifies
that login, network access, and account usage are working.

The database stores record state and audit history. The record model contains
no view, layout, or model-session definitions. Generation instructions belong
to the client gateway. The browser keeps views and conversations in memory.
Each generation uses an ephemeral Codex session and a temporary output directory.

Run the standard-library suite from the repository:

```sh
python3 -m unittest discover -s tests -v
```

These tests use temporary databases and a stub for AI generation. They check
permissions, validation, concurrent edits, persistent history, HTTP
authentication, origin restrictions, and invalid model output without using
the real Codex account. The four browser requests above verify the live AI
path and the experiment's visible result.

For automated browser verification, install the development-only Node tools and
Chromium, then run the browser suite:

```sh
npm ci
npx playwright install chromium
npm run test:browser
```

The default suite uses fixture plans and a temporary database. It checks the
four requests, proposal acceptance, history, schema-derived forms, a server
restart, and a fresh mobile client. Screenshots go in `artifacts/browser`.

To run those browser checks with the actual Codex provider:

```sh
npm run test:browser -- --live
```

This uses the server user's Codex account and consumes model usage. It still
creates its own temporary database, so it does not change the deployed world.
Screenshots go in `artifacts/live`.
