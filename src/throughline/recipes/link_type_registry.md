---
{
  "name": "link_type_registry",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Use a registered link type, or propose a new one.",
  "needs": [
    "view:link_types",
    "route:GET /v1/link-types"
  ],
  "statements": [
    "SELECT t.type, t.status, t.inverse, t.synonyms FROM api.link_types t WHERE t.status = 'active' ORDER BY t.type"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "type",
      "status"
    ]
  }
}
---

# Use a registered link type or propose one

Link types form a closed registry (GET /v1/link-types, api.link_types). Synonyms fold to their registered type; free text is refused for agents.

## Adapting

- Pick an active type; a synonym written by a source is stored with type_as_written.
- Missing type: POST /v1/proposals {"kind": "link_type", ...}; the owner registers, merges or retires it.

## Troubleshooting

- A 'candidate' type was written by a plugin and awaits the owner; do not rely on it.
- Merges re-type links and keep history; query by the registered name.
