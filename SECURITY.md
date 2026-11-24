# Security policy

Throughline holds the most personal data its owner has. Security reports are
taken seriously and handled privately.

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security
problem.** Report it privately through GitHub's private vulnerability
reporting:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**
   (<https://github.com/joey12725/throughline/security/advisories/new>).
3. Describe the problem, the affected version or commit, and how to
   reproduce it.

Only the maintainers can read the report. We aim to acknowledge it within
seven days, agree on a fix and a disclosure date with you, and credit you in
the advisory unless you prefer otherwise.

**Use synthetic data only.** Never attach a real archive, real messages,
tokens, `.env` files, database dumps or backups to a report. Reproduce with
the public twin (`python -m fixtures --seed 47`) or invented records; if a
bug only shows with your data, describe its shape (channel names, field
types, sizes) instead.

## Scope

In scope:

- the core API, CLI, migrations and the read-only SQL surface (`/v1/sql`,
  `api.*` views), in particular anything that lets a restricted asker see a
  record, count, label or error it should not;
- authentication and sign-in (tokens, sessions, OpenID Connect);
- model egress control (`THROUGHLINE_EGRESS_ALLOW`, label-aware egress);
- backups and their encryption;
- the Docker image and `scripts/install.sh` / `scripts/update.sh`;
- the Companion app and its bridge;
- plugins shipped in this repository and the SDK.

Out of scope: vulnerabilities in third-party services a connector talks to,
issues that need an attacker who already holds the owner token or the
database superuser password, and deployments that expose the API to the
internet without TLS against the documentation.

## Supported versions

Before 1.0, security fixes land on the latest release only. Update with
`scripts/update.sh` (Docker) or by pulling the latest tag.
