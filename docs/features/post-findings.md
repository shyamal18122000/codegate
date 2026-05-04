# Feature: Post Findings Engine (`post_findings.py`)

## Purpose

`post_findings.py` is the Phase 2 engine. It reads `findings.json` written by the agent, runs the full filter pipeline (confidence, dismissed matching, rules compliance, cap, dedup, suppression tracking), scores the PR with size normalization, posts inline comments, handles fix verifications (including justified/deferred), writes back learned dismissals, and updates the PR summary. It is fully deterministic and can run without any LLM involvement.

## CLI

```bash
python src/post_findings.py \
    --findings .cr/findings.json \
    --pr 42 \
    --repo MyRepo \
    --project MyProject \
    --vcs ado \
    --commit-id <sha>
    [--dry-run]
```

`--dry-run` executes all read/filter/score/dedup logic but skips all VCS writes. Output JSON is still produced.

## Processing Pipeline

1. **Read and validate** -- parse `findings.json` against `commands/findings-schema.json`. Reject if required fields are missing. Check `schema_version` for compatibility.
2. **Load dismissed findings** -- read `.codereview/dismissed.jsonl` if present. Each line is a JSON object with `glob`, `category`, and `regex` match fields.
3. **Load per-module config** -- read `.codereview/modules/*.yml` for severity shifts, forced modes, category overrides.
4. **Confidence filter** -- drop findings below 0.7 confidence (default). Configurable via `.codereview.yml`.
5. **Dismissed matching** -- match each finding against dismissed rules (glob on file path, category match, regex on message). Matching findings are moved to `suppressed_findings[]` with `source: "dismissed"`.
6. **Rules compliance** -- apply `.codereview-rules.yml` rules. Rule violations are added as new findings with `source: "rule"`. Tracked in `rules_checked[]`.
7. **Cap** -- max 30 findings total, max 5 per file. Priority order when over cap: critical -> warning -> suggestion -> good. Capped findings recorded in `suppressed_findings[]` with `source: "cap"`.
8. **Fetch existing cr-ids** -- read current PR threads (ADO: activity class; GitHub: `gh api`), extract `<!-- cr-id: xxx -->` markers.
9. **Check posting journal** -- read `.cr/posted.jsonl` if present. Add any journal cr-ids to the dedup set (partial failure recovery).
10. **Dedup** -- skip findings whose cr-id already appears in posted threads or journal. Recorded in `suppressed_findings[]` with `source: "dedup"`.
11. **Score** -- instantiate `PRScorer`, apply mode multipliers from `findings.review_modes`, normalize total penalty by `sqrt(file_count)`, calculate star rating.
12. **Score persistence** -- save current score to `.cr/prior_score.json` for trend tracking.
13. **Post inline comments** -- ADO: direct activity class import; GitHub: `gh api` via `_gh_run_with_retry`. Each posted comment is journaled to `.cr/posted.jsonl`.
14. **Fix verification** -- if `fix_verifications[]` present:
    - Resolve threads for `fixed` items
    - Zero penalty for `justified` items
    - Downgrade severity (50% penalty) for `deferred` items
    - Leave `still_present` threads open
    - Generate before/after score comparison
15. **Learning write-back** -- auto-detect dismissals from `justified` fix verifications. Persist to `.codereview/dismissed.jsonl`. Auto-generate patterns when `dismiss_count >= 3`.
16. **Post/update summary** -- formatted markdown via `markdown_formatter`, includes score breakdown, fix comparison if re-push, Rules Compliance section, cost footer.
17. **Output CI JSON** -- structured JSON to stdout for pipeline gating.

### Pipeline Stats

`_run_filter_pipeline` tracks drop reasons for every finding that does not make it to posting:

| Source | Meaning |
|--------|---------|
| `confidence` | Below minimum confidence threshold |
| `dismissed` | Matched a dismissed pattern from `.codereview/dismissed.jsonl` |
| `cap` | Exceeded max findings or max per-file limit |
| `dedup` | cr-id already present in PR threads or posting journal |

Stats are logged at INFO level: `"Filter pipeline: 24 in, 18 posted, 2 dismissed, 2 capped, 2 deduped"`.

## Output CI JSON

```json
{
  "star_rating": 4,
  "star_count": 4,
  "findings_count": {"critical": 0, "warning": 3, "suggestion": 5},
  "findings_posted": true,
  "summary_posted": true,
  "suppressed_count": 3,
  "rules_checked": ["no-eval", "require-license-header"],
  "token_usage": {
    "prompt_tokens": 12500,
    "completion_tokens": 3200,
    "total_tokens": 15700,
    "estimated_cost": "$0.23"
  }
}
```

## Gate Thresholds

If `/workspace/.codereview.yml` exists, `post_findings.py` reads:
- `min_star_rating` -- CI fails if score falls below this value
- `fail_on_critical` -- CI fails if any critical findings are unresolved
- `fail_on_suppressed_security` -- CI fails if any security-category findings were suppressed (prevents silently dismissing security issues)

## Suppressed Findings Audit Trail

All suppressed findings are recorded in `suppressed_findings[]` in findings.json:

```json
{
  "suppressed_findings": [
    {
      "cr_id": "abc12345",
      "file": "src/auth.py",
      "line": 42,
      "category": "security",
      "severity": "warning",
      "source": "dismissed",
      "reason": "Matched dismissal rule: glob=src/auth.py, category=security"
    }
  ]
}
```

This provides full visibility into what was filtered and why, enabling audit of the filter pipeline.

## Cost Tracking

`token_usage` from findings.json is forwarded to the CI output JSON. Cost estimation uses the `MODEL_PRICING` table which maps model identifiers to per-token input/output rates.

## Partial Failure Recovery

Each successfully posted comment is journaled to `.cr/posted.jsonl`:

```jsonl
{"cr_id": "abc12345", "file": "src/foo.py", "line": 42, "posted_at": "2024-01-15T10:30:00Z"}
{"cr_id": "def67890", "file": "src/bar.py", "line": 17, "posted_at": "2024-01-15T10:30:01Z"}
```

On re-run, the journal is read before the dedup step. This handles the case where a previous run posted some comments but crashed before posting all of them or updating the summary.

## ADO vs GitHub VCS Paths

| Operation | ADO | GitHub |
|-----------|-----|--------|
| Fetch existing threads | `FetchPRCommentsActivity` | `gh api repos/{repo}/pulls/{pr}/comments` |
| Post inline comment | `PostPRCommentActivity` | `gh api repos/{repo}/pulls/{pr}/comments` (JSON body) |
| Resolve thread | `PostFixReplyActivity` | `gh api` reply to comment |
| Post summary | `UpdateSummaryActivity` | `gh pr comment {pr} --body "..."` |
| Update summary | `UpdateSummaryActivity` (update-in-place) | `gh api` to edit existing comment |

All GitHub calls go through `_gh_run_with_retry()` which applies exponential backoff on rate-limit responses (HTTP 429, "secondary rate limit" in stderr).

GitHub summary comments are updated in-place rather than creating new comments on each run.

## Comment Format

Every posted inline comment includes:
- Severity icon + category header
- Finding body + suggestion
- `<!-- cr-id: {id} -->` HTML comment at the end (used for dedup on next run)
