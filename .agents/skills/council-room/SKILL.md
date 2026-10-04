---
name: council-room
description: Run or resume an auditable AI Company OS product-definition meeting with the CEO and exactly three executives (CTO, CPO, CMO), then present a hash-bound user-scenario approval bundle. Use when the user wants to define, debate, resume, or approve a product through the local company council. Do not use for ordinary unrecorded chat or to bypass product approval.
---

# Council Room

Connect this Codex chat to the local Company OS ledger. A chat message is not
recorded merely because this skill is loaded: deliver every CEO meeting message
through a real `company council` command and return its receipt.

## Start or resume

1. Read `AGENTS.md` and confirm the repository root and ledger path.
2. If the user supplied a session ID, run `company council status <session-id>`
   before doing anything else. Continue that session; do not create a new one.
3. For a new meeting, create one Idea, then run
   `company council open <idea-id> --idempotency-key <unique-key>`.
4. Put the CEO's exact current message in a UTF-8 local handoff file under
   `var/handoffs/council-room/`. Invoke:

   ```text
   company council turn <session-id> --message-file <path> --idempotency-key <unique-key>
   ```

5. Show the CTO, CPO, and CMO `speech` fields separately and without rewriting
   them. A clearly labeled moderator summary may follow. Summarize at most three
   important questions under: confirmed, unresolved, disagreements, next step.
6. Inspect `unresolved_decisions` before closing. If any remain, ask the CEO for
   an explicit decision, write the version-1 decision JSON, and run
   `company council decide <session-id> --decision-file <path> --idempotency-key <key>`.
   Never invent a CEO decision. Closing is expected to fail until the current
   unresolved-decision set is hash-bound to that decision.
7. Return a receipt containing the session ID, turn ID/number, CEO-message
   SHA-256, frozen-input SHA-256, and each role's provider/model/status/output
   SHA-256. A file hash proves the stored text, not how an app rendered it.

The default routes are CTO=Codex subscription CLI, CPO=Claude Code subscription
CLI, and CMO=Codex subscription CLI. Exactly three separate role executions are
required. Do not impersonate a missing executive or ask one model to generate
all three voices.

## Failure and retry

- Keep completed role outputs. For `QUOTA_WAIT`, `AUTH_REQUIRED`, timeout, or
  another unfinished role, report the exact state and use
  `company council retry <session-id> --turn <n> --role <role> --idempotency-key <new-key>`
  only after the user asks to continue or the external condition is resolved.
- Retry never carries a new CEO message and never reruns a completed role.
- If Company OS is stopped, do not start another provider process.
- Never place API keys, subscription tokens, or credentials in prompts,
  handoffs, receipts, or Git.

## Design and scenario approval

When the product is concrete enough, close the meeting and prepare the five
versioned artifacts: product brief, development schema, user scenarios,
implementation plan, and approval bundle. Use `company product draft` with a
definition file. Present the user scenarios in plain Korean before asking for
approval.

Obtain the exact sentence from `company product approval-text <bundle-id>`.
Only the CEO may provide that sentence. Record it with `company product approve`.
Do not execute product WorkOrders before `company product plan` succeeds for the
same product ID and bundle. A vague reply such as "좋아" does not approve a
different product, version, Git target, merge, push, or server deployment.

CEO statements prove the CEO's own requirements and scope choices. They do not
by themselves prove external market, legal, or technical facts.

## Honest completion boundary

This skill orchestrates local commands; it does not add a new chat UI and does
not capture ordinary messages automatically. Report subscription CLI, local
preview, Git commit, review, merge, push, and server hosting as separate facts.
