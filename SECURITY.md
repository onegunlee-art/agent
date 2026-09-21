# Security Policy

## Public repository rules

- Use synthetic data only in source, tests, and smoke tests.
- Never commit credentials, tokens, customer data, confidential venture data,
  runtime databases, handoffs, logs, or generated evidence.
- Keep external actions manual and require explicit CEO approval where the
  First Principles level is `FP_FULL`.
- Treat `var/state/*.db`, SQLite WAL/SHM files, handoffs, Context Manifests,
  Evidence, and review responses as local runtime data.
- Before each public push, inspect the staged diff and scan for credential
  patterns. `.gitignore` does not protect files that were already tracked.

## OneDrive warning

Running inside a synchronized folder can cause file locking, Git index or
worktree conflicts, and out-of-order synchronization of SQLite database,
WAL, and SHM files. Do not open the same runtime database from multiple PCs
or synchronizing processes. V0.1 reports this risk but never relocates the
repository automatically.

Report suspected credential or confidential-data exposure privately to the
repository owner. Do not open a public issue containing sensitive material.
