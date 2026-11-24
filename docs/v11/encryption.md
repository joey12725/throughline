# Encryption and what leaves the machine

Agents search plaintext: full-text search, vector search and `POST /v1/sql`
all run inside PostgreSQL over the stored text. Encrypting content columns
would break those, or put the key in the API process where it protects
nothing the asker gate (`access.md`) does not already. So content is not
encrypted per column. Everything under and around the database is, and
labelled content is kept away from model providers off this host.

| Layer | Protects against | How | Cost to agents |
|---|---|---|---|
| Volume under the PostgreSQL data directory | a stolen, returned or discarded disk | LUKS, an encrypted array or dataset; unlocked at boot from a key file or a TPM-sealed key | none |
| Backups | a copied dump or off-site store | `THROUGHLINE_BACKUP_ENCRYPT_CMD` is required by default (`ops.md`); a public-key recipient lets the server encrypt without being able to decrypt | none |
| Transport | a listener on the network | the API binds to loopback; across machines use WireGuard/Tailscale or a TLS reverse proxy, and `sslmode=verify-full` when PostgreSQL is on another host | none |
| Credentials | a leaked dump or process listing | tokens are stored only as SHA-256 hashes; keep provider keys in 0600 files or container secrets | none |
| Egress to model providers | a vendor holding private text | the label rule below (migration `0055_egress.sql`) | held content has no vector and no model judgement from a remote provider; keyword and structured search are unchanged |

## Volume encryption

The owner's choice for an unattended server is automatic unlock at boot from
a key file or a TPM-sealed key. That covers a disk that leaves the machine on
its own. It does not cover the whole machine being taken while it can still
boot, or anyone with root on the running server: those rest on the host's
own access controls and on the asker gate.

Throughline stores no key material and needs no setting for this layer.

## Label-aware egress (0055)

`tl.labels.egress` (default false) says whether content under a label kind
may be sent to a model provider off this host. A message is held when a label
of its channel, of its thread, or its own `access_label` is a kind with
`egress=false`, or (page revisions and derived records) when any source of its
lineage is held; the lineage walk is the one of `api.row_visible` and fails
closed on a cycle, a missing source or more than four levels. A conversation
chunk is held when any current message of its range is.

```
throughline lib access egress LABEL --allow [--purpose TEXT]
throughline lib access egress LABEL --deny
throughline lib access labels          # shows egress per label
```

Each change is appended to `tl.access_events` (op `label_egress`). The
setting never changes what an asker reads and does not move the policy
version.

"Off this host" means a provider that is not a local implementation and
whose endpoint is not loopback (`providers.egress.leaves`). A model served on
127.0.0.1 may read everything.

Where the rule is applied, when the provider leaves the host:

| Call | Held content |
|---|---|
| Embedding worker, contextual neighbours, vendor batch jobs (conversations and page passages) | not selected, not counted as pending, so a model still becomes active |
| Reranker and span scoring (`ranking.score_cached`) | not sent; the item keeps no score and sorts after scored ones (`stats.egress_held`) |
| `api.match` (semantic model) | the unit stays NULL with reason `egress_held`; the execution is degraded |
| Commitment and transcript jobs | sentences and turns are left out (`egress_held` in the result) |
| Librarian curate judge | the judgement is skipped when any source of the instruction or the items is held |
| Librarian controller views | the item is replaced by `{record_id, withheld: "egress_held"}`; the record is still delivered to the asker |

Functions (owner and the app role; not the reader): `tl.message_egress_ok(id)`,
`tl.conversation_egress_ok(id)`, `tl.messages_egress_held(ids[])`.

Limits:

- Forbidding a label does not recall what a vendor already received, and
  vectors stored meanwhile stay.
- A search query is the asker's own text and is still embedded by the
  configured provider.
- Controller views are filtered by message: an item that names neither a
  message id nor an `m:` record id passes as it is.
- Content with no label is not held. Label the channel, the thread or the
  message (`access_label` at ingest) for the rule to apply.
