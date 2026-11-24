---
{
  "name": "catalogue_first",
  "version": 1,
  "schema_version": "0045",
  "status": "tested",
  "summary": "Orient before querying: read the catalogue index, then the functions with exactness, cost and examples.",
  "needs": [],
  "statements": [
    "SELECT api.describe() AS index",
    "SELECT api.describe('functions') AS functions"
  ],
  "expect": {
    "min_rows": 1,
    "columns": [
      "functions"
    ]
  }
}
---

# Catalogue first

Start every session here. `api.describe()` (or `GET /v1/describe`) returns the subjects, the channels this asker can see and the function names. `api.describe('functions')` lists each function with signature, exactness, cost class, level, access and a runnable example. `api.describe('channel:<name>')` adds record kind, fields and coverage; `api.describe('recipes')` lists recipes like this one.

## Adapting

- Read `api.describe('surface')` once: catalogue, SQL, context, recall, and the librarian only for large investigations.
- Read the section of a function family before using it (`recall`, `result_sets`, `semantic_sql`, `grain`, `time`, `record_kinds`).
- A hidden channel reads exactly like an unknown one; describe is scoped to the asker.

## Troubleshooting

- A function missing from `functions` is not installed on this server; recipes that need it are reported skipped.
- A WHERE clause outside a limited search filters only the candidates that search returned: `SELECT * FROM api.search('x', 20) WHERE channel='c'` can return nothing while channel c has matches. Put filters into the function's own arguments (search_filtered, recall's channels/after/before) or pass a NULL limit.
