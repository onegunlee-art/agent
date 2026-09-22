# Claude Review Handoff

Claude is an independent manual reviewer for V0.1. The application creates a
structured ReviewRequest in JSON and Markdown, pauses in `WAITING_FOR_OPUS`,
and later ingests a real ReviewResult supplied by the CEO.

Do not claim a review occurred unless a real response was supplied. Automated
tests use a deterministic FakeReviewer.

## Manual procedure

1. Open the JSON or Markdown path printed when a WorkOrder enters
   `WAITING_FOR_OPUS`.
2. Give that complete ReviewRequest to Claude Opus.
3. Save Claude's structured response as JSON without changing the request ID
   or request hash.
4. Ingest it with:

   ```powershell
   company review ingest <review_id> --file <review_result.json>
   ```

The required ReviewResult fields are:

- `schema_version` (`2`)
- `review_request_id`
- `review_request_hash`
- `reviewed_commit` (must equal the request's `source_commit`)
- `reviewed_tree_sha256` (must equal the request's `source_tree_sha256`)
- `source` (`user_supplied` for a real manual handoff)
- `verdict` (`PASS` or `CHANGES_REQUIRED`)
- `findings` (array)
- `required_changes` (array)

`CHANGES_REQUIRED` must contain at least one uniquely identified required
change. `PASS` must contain no required changes. The program validates the
request file, request hash, source binding, structure, and correlation; it
does not cryptographically prove the reviewer's identity.

After `CHANGES_REQUIRED`, create a repair-manifest JSON covering every change
ID with its commit and canonical trusted Evidence IDs. First register at least
one test result for each change:

```powershell
company evidence add-test-result <work_order_id> `
  --review <review_id> `
  --change <change_id> `
  --file <test_result_file> `
  --node-id <pytest_node_id> `
  --source-commit <current_commit>
```

The resulting `TEST_RESULT` ID must be in that change's `evidence_ids`. Its
immutable file SHA-256, test node IDs, review/change identity, source commit,
and source-tree SHA-256 are revalidated. Generic Run Evidence may be included
as additional context but cannot replace change-specific test evidence.

The result file is a UTF-8 JSON PASS receipt; opaque logs and failed reports
are rejected:

```json
{
  "schema_version": 1,
  "kind": "PYTEST_RESULT",
  "status": "PASSED",
  "exit_code": 0,
  "source_commit": "<current Git commit>",
  "source_tree_sha256": "<current tracked-tree SHA-256>",
  "tests": [
    {"node_id": "tests/test_example.py::test_change", "outcome": "PASSED"}
  ]
}
```

Every selected `--node-id` must occur as `PASSED` in that receipt. The command
requires the explicit execution-time `--source-commit`; neither the CLI nor
the receipt may silently relabel an older run. Then run:

```powershell
company work resume <work_order_id> --repair-manifest <repair_manifest.json>
```

A successful repair creates a new ReviewRequest. It does not complete the
WorkOrder; only PASS on the new request can do that.

Every schema-v2 request's `response_schema.required_values` states the exact
required `schema_version`, `reviewed_commit`, and `reviewed_tree_sha256`.

Review and repair handoffs require a clean Git source state. Commit the code
first; untracked source, tracked modifications, unsafe index flags, and
unapproved ignored paths are rejected. Project-local `__pycache__`,
`.egg-info`, tracked symlinks, and gitlinks are also rejected. The generated
JSON and Markdown request files are both hash-checked before result ingest.
