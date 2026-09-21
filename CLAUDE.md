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

- `schema_version` (`1`)
- `review_request_id`
- `review_request_hash`
- `source` (`user_supplied` for a real manual handoff)
- `verdict` (`PASS` or `CHANGES_REQUIRED`)
- `findings` (array)
- `required_changes` (array)

`CHANGES_REQUIRED` must contain at least one required change. The program
validates structure and correlation; it does not cryptographically prove the
reviewer's identity.
