# Claude Review Handoff

Claude is an independent manual reviewer for V0.1. The application creates a
structured ReviewRequest in JSON and Markdown, pauses in `WAITING_FOR_OPUS`,
and later ingests a real ReviewResult supplied by the CEO.

Do not claim a review occurred unless a real response was supplied. Automated
tests use a deterministic FakeReviewer.
