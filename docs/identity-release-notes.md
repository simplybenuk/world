# Persistent agent identity, unreleased

This is a release-note draft. No tag or release has been created.

An authorization server can bind an optional persistent `identity` to a person's
agent grant at `POST /api/access`. Renewals preserve it; changing it requires
revocation. Existing grants, access files and worlds keep their previous behavior.

Rules can use authenticated `caller.identity` and `caller.agent`. `$identity`
defaults author references to the bound identity, but definitions must still check
authorship on creation. The world description returns trusted caller context.
Audit lines retain `person via software` and add `as identity` when bound. Record
history stores identity separately, so display-name changes do not change authorship.

The description no longer returns an actor's private fields when that actor's
record is unreadable to the caller. HTTP session and MCP operations preserve the
same identity context, including cookies, queries, history and atomic batches.

Every HTTP or MCP request has a shared rule-expression work budget, default
100,000 steps, configurable with `--rule-step-limit`. An exhausted operation is
refused and rolled back. `--identity-rate-limit N` adds an optional per-minute
request limit shared across an identity's connections and renewed tokens. Rate
counters are in memory and reset when the server restarts. Storage quotas remain
definition constraints.

For Teamworld Social this enables separately authenticated identities owned by
the same human. The Social definition and account management remain in Teamworld.

Validation before release:

- Run `python3 -m unittest discover -s tests`.
- Run `python3 -m bench`, which uses protocol clients and no model inference.
- Review Teamworld's identity integration against this exact revision.
- Merge, publish the chosen release tag, and move Teamworld's submodule pin.

Do not deploy Teamworld Social against an untagged or locally patched World.
