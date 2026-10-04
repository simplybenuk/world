# Second experiment: a CRM definition

This is a local prototype for the team reviewing the World development method.
Its question is whether a CRM can run through the same record engine and browser
client as the team experiment, with domain differences in a separate definition.

This is the earlier browser experiment. The current direction is an agent as
the human's primary interface, tested in the [third experiment](third-experiment.md).
The browser here selects among fixed layouts; it does not generate a complete
application interface from a prompt.

The evidence is a working CRM, a working team world on the same engine, and
server-enforced CRM rules. This experiment does not cover production rollout,
schema migrations, external CRM imports, billing, or a general business-action
language.

## Run the CRM

From this prototype checkout, use Python 3.11 or newer and an authenticated
Codex CLI:

```sh
python3 -m world.server --world definitions/crm.world.json --data-dir .world/crm --port 8788
```

In another terminal, from the same checkout:

```sh
python3 -m world.server access --world definitions/crm.world.json --data-dir .world/crm --port 8788 --actor ben
```

Open the private URL it prints. The browser discovers `Organization`, `Contact`,
`Opportunity`, and `Interaction`. `Person` describes the people who can sign in;
CRM contacts are separate records.

The database and credentials in `.world/crm` are disposable local dependencies.
They contain fictional records and stay outside Git. A second run reopens the
same data. To start fresh, use a different directory, such as `.world/crm-2`, in
both commands. The engine rejects opening a database bound to another World ID.

## Review the scenarios

Send these requests in sequence:

1. `Show my open sales opportunities.`
2. `Make this a pipeline board by stage.`
3. `Move the Northstar renewal opportunity to proposal.`

The first two requests should change the browser view without changing records.
The third should offer a proposal for review. Before accepting, open the renewal
opportunity and check that its stage remains `qualified`. Accept the proposal,
then check its saved `proposal` stage and history. Reloading starts a fresh view
and conversation while retaining the record change.

Browse the CRM types and create a contact using the discovered form. Open the
Northstar pilot opportunity, choose **Edit**, and try changing its stage directly
from `lead` to `won`. The service should reject this skipped transition and leave
the record and history unchanged. Valid progression is `lead`, `qualified`,
`proposal`, then `won`. An open opportunity can also become `lost`; terminal
stages cannot reopen.

Try assigning a contact from the private Atlas organization to a Northstar
opportunity. The service should reject the inconsistent relationship, including
when an admin attempts it. Moving a contact must also preserve the organizations
of existing related opportunities and interactions.

Sign out and print Sam's link with the same access command and `--actor sam`.
Sam can read the team-visible Northstar records, cannot see Atlas, and cannot
save edits. Maya can change records within Northstar, but cannot change account
membership or visibility. Roles come from stored identities and authentication.

## Compare both definitions

The team definition is [team.world.json](../definitions/team.world.json). The
CRM definition is [crm.world.json](../definitions/crm.world.json). Both use the
same `WorldStore`, HTTP routes, Codex planner, and `client` files.

Use a separate port and data directory to run the team through this engine:

```sh
python3 -m world.server --world definitions/team.world.json --data-dir .world/team --port 8789
```

The [first experiment's requests](first-experiment.md#try-the-four-requests)
still apply. The generalized engine describes and governs both domains; neither
definition contains pages, forms, boards, or model conversation state.

## Verify

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
npm ci
npx playwright install chromium
npm run test:browser
npm run test:crm-browser
```

The Python scenarios exercise both worlds, relationships, transitions,
permissions, conflicts, and atomic history. Browser verification uses fixture
plans and temporary databases by default. For the actual model provider:

```sh
npm run test:crm-browser -- --live
```

This consumes the server user's Codex usage. Its database is still temporary;
it does not edit the running CRM. Screenshots stay in the ignored `artifacts`
directory.

## Run alongside the first server experiment

On a Linux server with a user systemd session:

```sh
scripts/install-service.sh --world definitions/crm.world.json \
  --service-name world-crm-experiment.service \
  --data-dir ~/.local/share/world-crm-experiment \
  --host YOUR_TRUSTED_NETWORK_IP --port 8788 \
  --url http://YOUR_SERVER_NAME:8788
```

This installs a separate user service, database, and credentials. Different
World IDs also use different browser cookie names, so browsing both ports keeps
their sessions separate.

```sh
systemctl --user status world-crm-experiment.service
python3 -m world.server access --world definitions/crm.world.json \
  --data-dir ~/.local/share/world-crm-experiment --actor ben
```

Stop this prototype with `systemctl --user stop world-crm-experiment.service`.
Keep the definition and verification results for review. Promoting the prototype
into production remains a separate decision.

The format and authoring sequence are described in
[World format prototype](world-format-prototype.md).
Read [the verified findings](second-experiment-findings.md) for the comparison
and remaining questions.
