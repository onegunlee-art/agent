# Repository Instructions

- Keep the system local, on-demand, durable, resumable, and auditable.
- Use Python, SQLite as canonical state, and filesystem artifacts for handoffs
  and larger evidence.
- Write executable failing acceptance tests before implementation.
- Keep fixtures synthetic and deterministic.
- Never add OpenAI or Anthropic API calls, background daemons, schedulers,
  vector databases, external queues, or recursive Codex CLI calls in V0.1.
- Implement exactly three executive role specifications: CTO, CPO, and CMO.
- Treat runtime databases, logs, handoffs, evidence, and credentials as
  untracked local data.
- Keep SQLite as the only canonical decision and approval store; auxiliary
  tools may observe or index it but must not become a second ledger.
- Add external execution or open-source integrations one bounded component at
  a time, after an executable acceptance test establishes the need.
- Use a dedicated task branch. Do not push or merge to `main` without explicit
  CEO approval.
- For Company OS repository changes, follow the repo skill at
  `.agents/skills/company-os-build/SKILL.md`.
