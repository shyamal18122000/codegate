# Feature: Fix Verification and Re-push Flow

## Overview

When a developer pushes a fix after an initial review, CodeGate detects which prior findings were addressed and closes the corresponding PR threads. The summary shows a before/after score comparison. Developers can also justify or defer findings via PR replies.

## Verification Statuses

| Status | Meaning | Scoring Effect |
|--------|---------|----------------|
| `fixed` | Developer addressed the issue | Penalty removed |
| `still_present` | Issue remains in the code | Full penalty retained |
| `not_relevant` | File deleted, renamed, or code structurally moved | Penalty removed |
| `justified` | Developer provided valid reasoning for the pattern | Zero penalty |
| `deferred` | Developer acknowledges issue, will fix later | ~50% penalty (severity downgrade) |

## How It Works

### Phase 1 -- Agent (re-push)

On re-push, the agent performs a **delta-only review**: it reviews only the git diff between the old and new head commits, not the full PR diff. This keeps the review focused and reduces tool call usage.

The agent also runs **fix verification** (Step 6 of `review-pr-core.md`):
1. Fetch the existing cr-ids from current PR threads.
2. For each prior finding, check the current state of the code and any developer replies.
3. Classify each prior finding into one of the five statuses.
4. Write `fix_verifications[]` into `findings.json`.

### Phase 2 -- Poster

When `fix_verifications[]` is present in `findings.json`, `post_findings.py`:
1. Resolves/closes threads for `fixed` items (ADO: `PostFixReplyActivity`; GitHub: reply with "Fixed" via `gh api`).
2. Zeros penalty for `justified` items.
3. Downgrades severity for `deferred` items (`critical` -> `warning`, `warning` -> `suggestion`, `suggestion` stays `suggestion`).
4. Leaves `still_present` threads open.
5. Generates a before/after score comparison using `ScoreComparisonService`.
6. Includes the score comparison in the updated summary comment.
7. Triggers learning write-back for `justified` items (auto-adds to `dismissed.jsonl`).

## findings.json Structure (re-push)

```json
{
  "pr_id": 42,
  "fix_verifications": [
    {
      "cr_id": "abc12345",
      "status": "fixed",
      "reason": "Null check added on line 17"
    },
    {
      "cr_id": "def67890",
      "status": "still_present",
      "reason": "Input still not sanitized"
    },
    {
      "cr_id": "ghi11111",
      "status": "justified",
      "reason": "Developer explained this is intentional for backward compatibility",
      "counter_reason": "This pattern is required for backward compat with v1 API clients",
      "developer_reply": "This is intentional -- we need to support v1 clients until Q3 deprecation."
    },
    {
      "cr_id": "jkl22222",
      "status": "deferred",
      "reason": "Developer acknowledges the issue but defers to a follow-up PR",
      "counter_reason": "Will address in the logging refactor PR next sprint",
      "developer_reply": "Agreed this needs cleanup. Tracking in JIRA-1234, will fix in the logging refactor."
    }
  ],
  "findings": [...]
}
```

## FixVerification Fields

| Field | Required | Description |
|-------|----------|-------------|
| `cr_id` | yes | The prior cr-id exactly (e.g., `abc12345`) |
| `status` | yes | `fixed`, `still_present`, `not_relevant`, `justified`, or `deferred` |
| `reason` | yes | One sentence explaining the classification decision |
| `counter_reason` | no | The developer's core argument (extracted from reply) |
| `developer_reply` | no | Raw text of the developer's PR reply |

## Justified vs Deferred

**Justified** means the developer has a valid technical reason for the flagged pattern. The finding is effectively a false positive for this codebase. Examples:
- Using `eval()` in a sandboxed REPL environment
- Catching broad exceptions in a top-level error handler
- Hardcoded timeout values that are intentional tuning parameters

**Deferred** means the developer agrees the finding is valid but will address it later. Examples:
- Tech debt acknowledged but out of scope for this PR
- Requires a larger refactor tracked in a separate ticket
- Blocked on a dependency upgrade

## Configuration

One config knob in `.codereview.yml` controls the behavior:

| Key | Default | Description |
|-----|---------|-------------|
| `allow_deferred` | `true` | Whether `deferred` status is accepted. When `false`, deferred findings retain full penalty (treated as `still_present`). |

## GitHub Resolution

GitHub has no native thread resolution API (unlike ADO). Resolution is handled by:
1. Replying to the comment with "Fixed" via `gh api repos/{repo}/pulls/comments/{id}/replies`.
2. Optionally minimizing the comment via GraphQL (not yet implemented; planned for a future sprint).

## cr-id Matching

Fix verification matches on `cr_id` (8-char SHA1 hex). For matching to work across runs, the cr-id must be stable -- it is computed from `file:line:category` and does not change unless the file is renamed or the finding's line number shifts substantially.

## Learning Integration

When a finding is marked `justified`, the poster automatically:
1. Checks if a matching dismissal already exists in `.codereview/dismissed.jsonl`
2. If yes: increments `dismiss_count`; escalates scope if `dismiss_count >= 3`
3. If no: creates a new dismissal entry with `dismiss_count: 1`, scoped to the file
4. When a module-level dismissal reaches `dismiss_count >= 3`, auto-generates a learned pattern in `learned-patterns.jsonl`

This creates a feedback loop: developer justifications automatically train the system to suppress similar findings in the future.
