# Feature: Post Findings Engine (`post_findings.py`)

## Purpose

`post_findings.py` is the Phase 2 engine. It reads `findings.json` written by the agent, runs the filter pipeline (confidence filter, per-file cap, total cap), scores the PR with size normalization and mode multipliers, deduplicates against existing VCS threads, posts inline comments, handles fix verifications (including justified/deferred), detects learning dismissals, and updates the PR summary. Dismissed matching, rules compliance, and per-module config are Phase 1 (agent) responsibilities — the poster reads and displays their results. It is fully deterministic and can run without any LLM involvement.

## CLI

```bash
python src/post_findings.py \
    --findings .cr/findings.json \
    --workspace /workspace \
    --commit-id <sha> \
    [--dry-run]
```

The `pr_id`, `repo`, and `vcs` fields are read directly from `findings.json` (no separate CLI flags needed).

`--dry-run` executes all read/filter/score/dedup logic but skips all VCS writes. Output JSON is still produced.

## Processing Pipeline

1. **Read and validate** -- parse `findings.json` against `commands/findings-schema.json`. Reject if required fields are missing. Check `schema_version` matches expected version (`"1.0"`).
2. **Filter pipeline** (`_run_filter_pipeline`) -- confidence filter (drop below 0.7) + per-file cap (max 5) + total cap (max 30). Priority order when over cap: critical -> warning -> suggestion. Tracks drop stats per reason.
3. **Apply mode multipliers** -- `PRScorer.apply_mode_multipliers` adjusts finding severities based on `review_modes` (e.g., security mode x2 for security findings).
4. **Fetch existing cr-ids** -- read current PR threads (ADO: activity class; GitHub: `gh api`), extract `<!-- cr-id: xxx -->` markers.
5. **Check posting journal** -- read `.cr/posted.jsonl` if present. Add any journal cr-ids to the dedup set (partial failure recovery).
6. **Dedup** -- skip findings whose cr-id already appears in posted threads or journal.
7. **Score** -- exclude `justified` findings (zero penalty), downgrade `deferred` severity (`critical` -> `warning`, `warning` -> `suggestion`), then calculate star rating with `sqrt(file_count)` normalization.
8. **Score comparison** -- load prior score from `.cr/prior_score.json`, generate before/after markdown if fix verifications present. Save current score for next run.
9. **Evaluate CI gate** -- check `.codereview.yml` thresholds (`min_star_rating`, `fail_on_critical`, `fail_on_suppressed_security`).
10. **Post inline comments** -- ADO: direct activity class import; GitHub: `gh api` via `_gh_run_with_retry`. Each posted comment is journaled to `.cr/posted.jsonl`. Journal is cleaned up after all posts succeed.
11. **Fix verification** -- if `fix_verifications[]` present:
    - Resolve threads for `fixed` and `justified` items
    - Reply with acknowledgement for `deferred` items
    - Reply with counter-reason for `still_present` items (if counter_reason provided)
    - Respects `allow_deferred` config (when false, deferred treated as still_present)
12. **Learning write-back** -- detect dismissals from `justified` fix verifications via `_detect_dismissals`. Merge with existing `.cr/dismissed.jsonl` via `_merge_dismissals` (increments dismiss_count, escalates scope at count >= 3). Generate learned patterns when module-level dismissals reach count >= 3. _(Note: learning comment posting and file write-back are implemented as helpers but not yet wired into the main `run()` flow.)_
13. **Post/update summary** -- the summary is built from **mode-adjusted findings** (after `apply_mode_multipliers`), not the raw findings. This means severity counts in the summary reflect the escalated severities. Includes: score breakdown, suppressed findings by source (intent markers, dismissed patterns, never-flag rules), filter pipeline stats, fix verification counts (all 5 statuses with deferred penalty note), Rules Compliance section, CI gate result, and cost footer. GitHub summaries are updated in-place using the `<!-- CODEGATE-summary -->` marker.
14. **Output CI JSON** -- structured JSON to stdout for pipeline gating.

**Note:** Dismissed-findings matching, rules compliance, and per-module config loading are Phase 1 (agent) concerns. The agent populates `suppressed_findings[]` and `rules_checked[]` in findings.json; the poster reads and displays them but does not re-apply these filters.

### Pipeline Stats

`_run_filter_pipeline` returns `drop_stats` tracking what happened to each finding:

| Key | Meaning |
|-----|---------|
| `total_produced` | Total findings including pre-suppressed |
| `dropped_confidence` | Below minimum confidence threshold (0.7) |
| `dropped_per_file_cap` | Exceeded max 5 findings per file |
| `dropped_total_cap` | Exceeded max 30 findings total |
| `suppressed` | Pre-suppressed by Phase 1 (intent markers, dismissed patterns, never-flag) |
| `posted` | Findings that passed all filters |

Dedup happens separately after the pipeline (comparing against VCS threads and posting journal).

## Output CI JSON

The structured output to stdout matches the actual `run()` return dict:

```json
{
  "pr_id": 42,
  "repo": "MyRepo",
  "vcs": "ado",
  "review_modes": ["standard", "security"],
  "agent": "codex",
  "tool_calls": 35,
  "filtering": {
    "total_raw": 24,
    "after_confidence_filter": 22,
    "filtered_low_confidence": 2,
    "after_cap": 20,
    "dropped_per_file_cap": 1,
    "dropped_total_cap": 1,
    "deduped_already_posted": 3,
    "new_findings_posted": 17,
    "post_errors": [],
    "suppressed_count": 4
  },
  "score": {
    "total_penalty": 8.5,
    "overall_stars": "⭐⭐⭐⭐☆",
    "quality_level": "Excellent",
    "issues_by_severity": {"critical": 0, "warning": 3, "suggestion": 14},
    "category_penalties": {"security": 4.0, "best_practices": 2.5, "performance": 2.0}
  },
  "gate": {"passed": true, "reasons": []},
  "dry_run": false,
  "findings": [{"id": "abc12345", "file": "src/auth.py", "line": 42, "severity": "warning", "category": "security", "title": "...", "confidence": 0.85}],
  "fix_verifications": [],
  "rules_checked": [{"id": "no-eval", "applied_to": 5, "findings_generated": 0}],
  "has_comparison": false,
  "cost_estimate": "$0.2300",
  "token_usage": {"input_tokens": 12500, "output_tokens": 3200}
}
```

## Gate Thresholds

If `/workspace/.codereview.yml` exists, `post_findings.py` reads gate configuration via `_load_codereview_yml()`:

| Key | Default | Description |
|-----|---------|-------------|
| `min_star_rating` | 0 (disabled) | CI fails if star rating falls below this value |
| `fail_on_critical` | `true` | CI fails if any critical findings are unresolved |
| `fail_on_suppressed_security` | `false` | CI fails if any security-category findings were suppressed |
| `allow_deferred` | `true` | Whether `deferred` fix verification status is accepted. When `false`, deferred findings are treated as `still_present` (full penalty, thread not acknowledged) |
| `learning_delivery` | `"comment"` | How learning write-back artifacts are delivered (`comment`, `commit`, `none`) |

The gate evaluation in `_evaluate_gate()` produces a `{"passed": bool, "reasons": [...]}` dict included in the CI output and summary.

## Intent Markers and Suppression Flow

Code can opt out of review using inline markers (`# cr: intentional`, `# cr: ignore-next-line`, `# cr: ignore-block start/end`). These are detected by the Phase 1 agent during file review. When the agent encounters a marker, it adds the finding to `suppressed_findings[]` with `dismissed_id: "intent-marker"` instead of the main `findings[]` array.

Similarly, findings matching per-module `never_flag` rules are suppressed with `dismissed_id: "never-flag"`, and findings matching dismissed.jsonl patterns are suppressed with the pattern's `dismissed_id`.

The poster does not re-evaluate markers — it receives pre-classified suppressed findings and displays them in the summary grouped by source.

## Suppressed Findings Audit Trail

All suppressed findings are recorded in `suppressed_findings[]` in findings.json by the Phase 1 agent. Each entry uses the `SuppressedFinding` dataclass:

```json
{
  "suppressed_findings": [
    {
      "id": "abc12345",
      "file": "src/auth.py",
      "line": 42,
      "category": "security",
      "severity": "warning",
      "title": "Broad exception handler",
      "reason": "Matched dismissal rule: file_pattern=src/auth/**, category=security",
      "dismissed_id": "d-a1b2c3d4"
    }
  ]
}
```

### Classification (`_classify_suppressed_by_source`)

The poster classifies suppressed findings into 3 buckets based on the `dismissed_id` field:

| `dismissed_id` value | Bucket | Meaning |
|---------------------|--------|---------|
| `"intent-marker"` | `intent_marker` | Suppressed by `# cr: intentional`, `ignore-next-line`, or `ignore-block` |
| `"never-flag"` | `never_flag` | Suppressed by per-module `never_flag` config |
| Any other value (e.g., `"d-a1b2c3d4"`) | `dismissed_pattern` | Suppressed by a dismissed.jsonl pattern match |

When `dismissed_id` is missing or empty, the poster falls back to matching keywords in the `reason` field ("intentional"/"intent" -> intent_marker, "never_flag"/"never flag" -> never_flag, else -> dismissed_pattern).

These buckets are displayed in the summary comment:
```
## Suppressed Findings
- Intent markers: 2
- Dismissed patterns: 3
- Never-flag rules: 1
- Total suppressed: 6
```

## Cost Tracking

`findings.json` includes a `token_usage` object with `input_tokens` and `output_tokens` (integers). The poster computes cost estimates via `_compute_cost_estimate()` using the `MODEL_PRICING` table:

| Agent | Input ($/1M tokens) | Output ($/1M tokens) |
|-------|---------------------|----------------------|
| codex | $3.00 | $12.00 |
| claude | $3.00 | $15.00 |
| gemini | $1.25 | $5.00 |

The estimated cost string (e.g., `"$0.2300"`) is included in the summary comment and the CI output JSON as `cost_estimate`.

## Partial Failure Recovery

Each successfully posted comment is journaled to `.cr/posted.jsonl` via `_append_posted_journal()`:

```jsonl
{"cr_id": "abc12345", "ts": "2024-01-15T10:30:00+00:00"}
{"cr_id": "def67890", "ts": "2024-01-15T10:30:01+00:00", "comment_id": "12345"}
```

The journal flow in `run()`:
1. `_load_posted_journal()` reads existing journal cr-ids and merges them with VCS thread cr-ids for dedup
2. After each successful post, `_append_posted_journal()` writes the cr-id to the journal
3. After all posts succeed with no errors, `_cleanup_posted_journal()` deletes the journal file

On re-run after a partial failure, the journal ensures already-posted findings are skipped even if the VCS thread fetch is incomplete.

## ADO vs GitHub VCS Paths

| Operation | ADO | GitHub |
|-----------|-----|--------|
| Fetch existing threads | `FetchPRCommentsActivity` | `gh api repos/{repo}/pulls/{pr}/comments` |
| Post inline comment | `PostPRCommentActivity` | `gh api repos/{repo}/pulls/{pr}/comments` (JSON body) |
| Resolve thread | `PostFixReplyActivity` | `gh api` reply to comment |
| Post summary | `UpdateSummaryActivity` | `gh pr comment {pr} --body "..."` |
| Update summary | `UpdateSummaryActivity` (update-in-place) | `gh api` to edit existing comment |

All GitHub calls go through `_gh_run_with_retry()` which applies exponential backoff on rate-limit responses (HTTP 429, "secondary rate limit" in stderr). Retries up to 3 times with exponential delay (1s, 2s, 4s).

### GitHub Summary Update-in-Place

GitHub summaries use `_post_or_update_summary_github()` which:
1. Fetches all existing issue comments on the PR via `gh api repos/{repo}/issues/{pr}/comments --paginate`
2. Searches for the `<!-- CODEGATE-summary -->` marker in comment bodies
3. If found: PATCHes the existing comment via `repos/{repo}/issues/comments/{id}`
4. If not found: POSTs a new comment via `repos/{repo}/issues/{pr}/comments`

This keeps the PR timeline clean — one summary comment is reused across re-runs instead of creating new ones.

## cr-id Generation (`_assign_cr_ids`)

Every finding needs a stable, deterministic identifier for dedup across runs. The agent sets `id: null` in findings.json; the poster computes it:

```python
hashlib.sha1(f"{file}:{line}:{category}".encode()).hexdigest()[:8]
```

- 8-character hex string (e.g., `"abc12345"`)
- Deterministic: same file + line + category always produces the same cr-id
- Findings that already have a non-null `id` are left unchanged (backwards compatibility)

**Limitation:** cr-id uses the file path. If a file is renamed between runs, the cr-id changes and prior comments will not be matched. Accepted for v1.

## Comment Format

Every posted inline comment includes:
- Severity icon (`🔴` critical, `⚠️` warning, `💡` suggestion) + category header
- Finding title (bold) + message body
- Suggestion (if present)
- Confidence percentage
- `<!-- cr-id: {id} -->` HTML comment at the end (used for dedup on next run)

## Dry-Run Mode

When `--dry-run` is passed:
- All read/filter/score/dedup logic executes normally
- All VCS writes are skipped (no comments posted, no threads resolved, no summary posted)
- The output JSON includes an additional `summary_md` field containing the full summary markdown that would have been posted
- Useful for testing the scoring engine against hand-crafted findings without affecting the PR
