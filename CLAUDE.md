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
ID with its commit and canonical trusted Evidence IDs. The program resolves
those IDs to the same WorkOrder's PASS Runs and rechecks every Evidence file
hash, then run:

```powershell
company work resume <work_order_id> --repair-manifest <repair_manifest.json>
```

A successful repair creates a new ReviewRequest. It does not complete the
WorkOrder; only PASS on the new request can do that.

Review and repair handoffs require a clean Git source state. Commit the code
first; untracked source, tracked modifications, unsafe index flags, and
unapproved ignored paths are rejected. Project-local `__pycache__`,
`.egg-info`, tracked symlinks, and gitlinks are also rejected. The generated
JSON and Markdown request files are both hash-checked before result ingest.
