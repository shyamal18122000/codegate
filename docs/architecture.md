# CodeGate -- Architecture

## Overview

CodeGate is a Docker-based AI code review system. An LLM agent (Codex, Claude, or Gemini) reads a PR and writes `findings.json`; a deterministic Python script then scores, deduplicates, and posts comments to ADO or GitHub. The two phases are explicitly separated so the agent never touches the VCS API and the poster never touches the LLM.

---

## Two-Phase Architecture

**Phase 1 -- Agent review**
The agent runs inside a Docker container with access to the workspace. It reads the PR diff and changed files using VCS tools, then writes `/workspace/.cr/findings.json`. The agent does not post anything.

Before the agent runs, the Docker entrypoint executes `tools/extract_signatures.py` to build a signature map for duplicate detection. The signature map is written to `/workspace/.cr/signature_map.json` (via stdout redirection) and made available to the agent in Step 5b-duplication.

**Phase 2 -- Deterministic posting**
`post_findings.py` reads `findings.json`, applies the full filter pipeline (confidence filter, dismissed-findings matching, rules compliance check, cap, dedup, suppression classification), scores the PR with size normalization, posts inline comments, handles fix verifications (including justified/deferred statuses), writes back learned dismissals, tracks partial failures via a posting journal, and updates the summary. This phase is fully deterministic and testable without a live LLM.

**Why this separation?**
- The agent is non-deterministic; the poster must be deterministic for idempotency.
- `--dry-run` can exercise the full poster path without VCS writes.
- Phase 2 can be re-run independently if posting fails (partial failure recovery via `.cr/posted.jsonl`).

---

## Idempotency via cr-id Deduplication

Every finding gets a stable identifier computed by `post_findings.py`:

```python
hashlib.sha1(f"{file}:{line}:{category}".encode()).hexdigest()[:8]
```

The agent sets `cr_id: null` in `findings.json`; the poster computes the hash and injects `<!-- cr-id: {id} -->` into every posted comment body. On re-runs, the poster fetches existing thread markers, extracts cr-ids, and skips findings whose cr-id is already present. This makes re-runs safe -- no duplicate comments, ever.

**Limitation:** cr-id uses the file path. If a file is renamed between runs, the cr-id changes and prior comments will not be matched. Accepted for v1.

---

## VCS Abstraction

Two distinct invocation patterns are used:

| Caller | ADO | GitHub |
|--------|-----|--------|
| Agent (Phase 1) | `python vcs.py <subcommand>` | `gh pr view`, `gh api` |
| Poster (Phase 2) | Activity classes imported directly | `subprocess.run` calling `gh` |

`vcs.py` is a thin argparse CLI that wraps the ADO activity classes and outputs JSON to stdout. It exists so the agent can call it as a shell command without knowing Python internals. `post_findings.py` bypasses `vcs.py` and imports activity classes directly for ADO (avoids subprocess overhead per comment).

For GitHub, all VCS calls in `post_findings.py` go through `_gh_run_with_retry()`, which wraps `subprocess.run` with exponential backoff for rate-limit errors.

---

## Penalty-Based Scoring

The PR receives a 1-5 star rating. Scoring deducts penalties per finding:

- **Critical** -- largest penalty
- **Warning** -- medium penalty
- **Suggestion** -- small penalty
- **Good** -- no penalty (positive signal)

**Review mode multipliers** use independent `if` blocks (not elif) so they stack when multiple modes are active:
- Security mode: security-category findings x2
- Performance mode: performance-category findings x2
- Architecture mode: best_practices-category x1.5
- Migration mode: all findings elevated to minimum critical severity

When multiple modes escalate the same finding, the strictest result wins.

### Size Normalization

Raw penalty totals are divided by `sqrt(file_count)` to produce a normalized score. This ensures large PRs with many files are not unfairly penalized compared to small focused PRs. The normalized score is what determines the star rating.

### Score Persistence

After scoring, the current score is saved to `.cr/prior_score.json`. On subsequent runs, this file provides the "before" score for comparison, enabling trend tracking across re-pushes without requiring fix_verifications.

### Tier-Aware Tool Budget

The agent's tool call budget scales with PR size:

| Tier | Files Changed | Tool Call Budget |
|------|--------------|-----------------|
| T1 | 1-3 | 25 |
| T2 | 4-10 | 40 |
| T3 | 11-25 | 60 |
| T4 | 26-50 | 80 |
| T5 | 51+ | 100 |

### Fix Verification Scoring

- `justified` findings receive zero penalty (developer provided valid reasoning)
- `deferred` findings receive approximately 50% penalty via `_downgrade_severity` (severity is dropped one level: critical -> warning, warning -> suggestion)

---

## Learning System

### Read Path

The `.codereview/` directory stores learning data:

```
.codereview/
  config.yml              # Learning system configuration
  conventions.md          # Team coding conventions
  dismissed.jsonl         # Dismissed finding patterns (one JSON object per line)
  learned-patterns.jsonl  # Auto-generated patterns from repeated dismissals
  modules/
    auth.yml              # Per-module config overrides
    api.yml
```

During filtering, `post_findings.py` reads `dismissed.jsonl` and matches each finding against dismissal rules using glob (file path), category, and regex (message content). Matching findings are suppressed and classified as `suppressed_by_dismissal` in the audit trail.

Per-module config files (`.codereview/modules/*.yml`) support:

| Field | Effect |
|-------|--------|
| `severity_shift` | Shift all severities up/down for files in this module |
| `never_flag` | Categories to never flag in this module |
| `always_flag` | Categories to always flag regardless of confidence |
| `focus_categories` | Categories to prioritize |
| `min_confidence` | Override minimum confidence threshold |
| `force_modes` | Always activate these review modes for this module |
| `max_per_file` | Override the per-file finding cap |

### Write Path

When fix verification marks a finding as `justified`, the poster detects the dismissal via `_detect_dismissals` and merges it with existing entries via `_merge_dismissals`:
- Each dismissal gets a stable `dismissed_id` (`d-` + SHA1 of `file:category:title`)
- `dismiss_count` tracking (increments on repeated dismissals)
- Scope escalation: file-level dismissal (`file_pattern` = exact path) escalates to module-level (`file_pattern` = parent dir glob) at `dismiss_count >= 3`
- Auto-pattern generation via `_generate_learned_patterns`: when module-level dismissals reach count >= 3, a learned pattern with `confidence_modifier: -0.3` is written to `learned-patterns.jsonl`

The `learning_delivery` config in `.codereview/config.yml` controls how learning artifacts are delivered: `comment` (default, posted as PR comment), `commit` (committed to repo), or `none` (disabled).

**Note:** The helper functions for learning write-back (`_detect_dismissals`, `_merge_dismissals`, `_generate_learned_patterns`, `_build_learning_comment_md`, `_post_learning_comment`) are implemented but the file write-back and comment posting are not yet wired into the main `run()` flow. Dismissal detection and merging occur, but results are computed without being persisted.

---

## Rules Engine

Deterministic rules defined in `.codereview-rules.yml` are applied in agent Step 5a-rules, before the LLM review. Rule types:

| Type | Description |
|------|-------------|
| `forbidden_pattern` | Regex pattern that must not appear in matching files |
| `forbidden_import` | Import statement that must not appear |
| `required` | Pattern that must appear in matching files (e.g., license header) |

Rules produce findings with `source: "rule"` and are tracked in the `rules_checked[]` array in findings.json. A "Rules Compliance" section is added to the PR summary.

---

## Duplicate Detection

`tools/extract_signatures.py` runs in the Docker entrypoint before Phase 1. It uses AST parsing to extract function/method signatures and computes `body_hash` values (normalized: whitespace and variable names stripped). The output is `/workspace/.cr/signature_map.json` (via stdout redirection).

The agent consumes the signature map in Step 5b-duplication. If the signature map is unavailable, the agent falls back to `rg`-based similarity search.

---

## findings.json Schema

`findings.json` is the contract between Phase 1 and Phase 2. Schema is defined in `commands/findings-schema.json`.

**Top-level fields:**
- `schema_version` -- version string of the findings.json schema (e.g., `"1.0"`)
- `pr_id`, `repo`, `vcs` -- PR identity (`vcs` is `"ado"` or `"github"`)
- `review_modes` -- list of active modes (standard, security, migration, docs/chore, architecture, performance)
- `agent`, `tool_calls` -- observability metadata (`agent` is `"codex"`, `"claude"`, or `"gemini"`)
- `findings[]` -- list of Finding objects
- `fix_verifications[]` -- list of FixVerification objects (only on re-push)
- `suppressed_findings[]` -- findings suppressed by intent markers, dismissal patterns, or never-flag rules
- `rules_checked[]` -- array of RuleChecked objects (id, applied_to, findings_generated)
- `token_usage` -- object with `input_tokens` and `output_tokens` (integers); cost is computed by the poster using the `MODEL_PRICING` table

**Finding fields:** `id` (null from agent, filled by poster via SHA1 hash), `file`, `line`, `severity`, `category`, `confidence`, `title`, `message`, `suggestion` (optional)

**FixVerification fields:** `cr_id`, `status` (fixed | still_present | not_relevant | justified | deferred), `reason`, `counter_reason` (developer's argument, optional), `developer_reply` (raw reply text, optional)

**SuppressedFinding fields:** `id`, `file`, `line`, `category`, `title`, `reason`, `dismissed_id` (classification key: `"intent-marker"`, `"never-flag"`, or a cr-id for dismissed patterns), `severity` (optional)

**PRScore fields (in summary):** `total_penalty`, `overall_stars` (emoji string), `star_count` (integer 1-5), `quality_level`, `category_penalties`, `category_stars`, `issues_by_severity`

---

## Post Findings Engine -- Filtering and Cap Logic

`post_findings.py` applies filters via `_run_filter_pipeline`, which tracks drop reasons for each finding:

1. **Confidence filter** -- drop findings below 0.7 (configurable via `.codereview.yml`)
2. **Per-file cap** -- max 5 findings per file, prioritized by severity (critical -> warning -> suggestion)
3. **Total cap** -- max 30 findings total, prioritized by severity

Dedup happens after the pipeline by comparing cr-ids against existing VCS threads and the posting journal (`.cr/posted.jsonl`).

Dismissed matching, rules compliance, and per-module config are Phase 1 (agent) responsibilities. The agent populates `suppressed_findings[]` and `rules_checked[]` in findings.json. The poster classifies suppressed findings into three buckets using the `dismissed_id` field: `intent_marker`, `dismissed_pattern`, and `never_flag`.

Pipeline stats (`drop_stats`) are returned for transparency: `total_produced`, `dropped_confidence`, `dropped_per_file_cap`, `dropped_total_cap`, `suppressed`, `posted`.

---

## Partial Failure Recovery

`post_findings.py` writes a posting journal to `.cr/posted.jsonl`. Each line records a successfully posted comment with its cr-id, file, and line. On re-run after a partial failure, the journal is read first and already-posted cr-ids are skipped in addition to the VCS thread check. This avoids duplicate posts even when the VCS thread fetch is incomplete.

---

## Cost Tracking

`findings.json` includes a `token_usage` object with raw token counts:

```json
{
  "token_usage": {
    "input_tokens": 12500,
    "output_tokens": 3200
  }
}
```

The poster computes cost estimates using the `MODEL_PRICING` table in `post_findings.py`, which maps agent names (`codex`, `claude`, `gemini`) to per-million-token input/output rates. The estimated cost (e.g., `"$0.2300"`) is included in the summary comment and CI output JSON.

---

## Gate Thresholds

`post_findings.py` reads `/workspace/.codereview.yml` if present and applies gate thresholds to the CI output JSON:
- `min_star_rating` -- fail CI if score falls below this
- `fail_on_critical` -- fail CI if any critical findings remain unresolved
- `fail_on_suppressed_security` -- fail CI if any security findings were suppressed. This prevents security issues from being silently dismissed.

The structured JSON output to stdout is consumed by CI pipelines to set pass/fail status.

---

## Docker Container

Base image: `node:22-slim`. Layers:
- System: Python 3, git, curl, jq, ripgrep
- GitHub CLI (`gh`)
- NPM globals: `@openai/codex`, `repomix`
- Python venv: `azure-devops`, `pydantic`, `pydantic-settings`, `msrest`
- Copied into image: `commands/`, `src/`, `tools/`, `templates/`, `PROJECT-CLAUDE.md`

`PYTHONPATH` points to `/app/src`. `entrypoint.sh` orchestrates: signature extraction (`tools/extract_signatures.py`), Phase 1 (agent dispatch by `$AGENT` env var), and Phase 2 (`post_findings.py` invocation).

---

## Config Philosophy

`src/config.py` (via pydantic-settings) carries only:
- ADO auth: PAT, system token, organization URL, project
- GitHub: `GH_TOKEN`
- VCS selector: `VCS` (ado/github)
- Penalty matrix and star thresholds

All AI/LLM settings were removed from config -- the agent CLI handles its own auth. This keeps the Python layer fully VCS-focused.

---

## Key Trade-offs

| Decision | Chosen approach | Alternative considered | Reason |
|----------|----------------|------------------------|--------|
| VCS invocation in poster | Import ADO activities directly | subprocess vcs.py | Avoids per-comment subprocess overhead |
| cr-id generation | Poster computes SHA1 hash | Agent computes hash | LLMs can't reliably compute SHA1 |
| GitHub thread resolution | Reply "Fixed" + optional GraphQL minimize | Native resolve (not available) | GitHub has no native thread resolution API |
| Agent-to-VCS boundary | Agent never calls post; poster never calls LLM | Merged single script | Enables dry-run, re-run, and deterministic tests |
| Comment consolidation | cr-id dedup in poster | LLM-based comment merging (old approach) | Deterministic, no LLM cost, idempotent |
| Score normalization | sqrt(file_count) divisor | Linear divisor, log divisor | sqrt balances between raw and over-normalized |
| Learning delivery | Default to PR comment | Always commit | Comment is less invasive; commit optional |
| Rules before LLM | Deterministic rules run first | Rules as LLM context | Deterministic rules are cheaper and more reliable |

---

## Risk Register Summary

| Risk | Severity | Status |
|------|----------|--------|
| Codex sandbox conflicts with Docker | High | `--sandbox=none` fallback available |
| Prompt quality determines review quality | High | Iterative tuning complete |
| cr-id instability on file rename | Medium | Accepted limitation for v1; documented |
| ADO SDK version compatibility | Medium | Pinned `azure-devops>=7.1,<8.0` |
| Agent exceeds tool-call budget | Medium | Tier-aware budgets mitigate; Phase 2 still processes partial findings |
| GitHub API pagination not handled | Low | Max 30 findings cap limits impact; `--paginate` planned |
| Docker image size > 2 GB | Medium | tools/ directory adds minimal size; multi-stage build available |
| Learning write-back conflicts | Low | File-level locking on dismissed.jsonl; scope escalation is additive only |
| Rules engine regex performance | Low | Rules are applied only to changed files; pattern count expected < 50 |

