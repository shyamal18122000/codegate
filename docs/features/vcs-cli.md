# Feature: VCS CLI (`vcs.py`)

## Purpose

`vcs.py` is a thin argparse CLI that gives the agent a stable shell interface to ADO VCS operations. It wraps the activity classes and outputs JSON to stdout. Errors go to stderr.

The agent calls `python vcs.py <subcommand>` during Phase 1. `post_findings.py` does not use `vcs.py` -- it imports activity classes directly for performance.

## Subcommands

| Subcommand | Wraps | Output |
|-----------|-------|--------|
| `get-pr <pr_id> --repo --project` | `FetchPRDetailsActivity` | PR metadata JSON |
| `list-threads <pr_id> --repo --project [--include-replies]` | `FetchPRCommentsActivity` | Threads JSON, including extracted cr-ids |
| `post-comment <pr_id> --repo --project --file --line --body` | `PostPRCommentActivity` | Posted comment JSON |
| `resolve-thread <pr_id> --repo --project --thread-id --reply --status` | `PostFixReplyActivity` | Resolution result JSON |
| `get-file --repo --project --path --commit` | `FetchFileContentActivity` | File content JSON |
| `post-summary <pr_id> --repo --project --body` | `UpdateSummaryActivity` | Summary update result JSON |

### --include-replies Flag

The `list-threads` subcommand accepts an optional `--include-replies` flag. When set, the activity populates the `replies` field on each `ExistingCommentThread` with the full reply chain (list of `{author, content, date}` dicts). This is used by the agent during fix verification (Step 6 of `review-pr-core.md`) to read developer responses for reply-aware classification into justified/deferred/still_present statuses.

### cr-id and Developer Reply Awareness

Each thread returned by `list-threads` includes:
- `cr_id` -- extracted from `<!-- cr-id: xxx -->` markers in the comment body
- `replies` -- (when `--include-replies` is set) developer replies that inform fix verification classification

## Auth

Auth is read from environment variables via pydantic-settings (`Settings` class). The agent container sets these env vars; no credentials are embedded in `vcs.py`.

## cr-id Extraction

`list-threads` returns threads with an additional `cr_ids` field. The activity extracts `<!-- cr-id: xxx -->` markers from each comment body, so the agent can load `existing_cr_ids` into `findings.json` without parsing raw HTML.

## GitHub Path

For GitHub PRs, the agent calls `gh` CLI directly (not `vcs.py`). GitHub-specific calls during Phase 1:
- `gh pr view --json` -- fetch PR metadata
- `gh api repos/{repo}/pulls/{pr}/comments` -- fetch existing review comments
- `gh pr diff` -- fetch PR diff
