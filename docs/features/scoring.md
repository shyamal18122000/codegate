# Feature: Penalty-Based Scoring

## Overview

The PR scorer converts a list of findings into a 1-5 star rating. Scoring is penalty-based: each finding subtracts from a perfect score. The scorer is deterministic and has no LLM dependency.

## Severity Penalty Weights

| Severity | Penalty |
|----------|---------|
| critical | highest |
| warning | medium |
| suggestion | small |
| good | 0 (positive signal, no deduction) |

Exact penalty values are defined in the penalty matrix in `src/config.py`.

## Star Rating Thresholds

Star thresholds are configurable via `src/config.py`. Default mapping (approximate):
- 5 stars -- no or minimal findings
- 4 stars -- a few warnings, no criticals
- 3 stars -- several warnings
- 2 stars -- critical findings present
- 1 star -- multiple criticals or score below floor

The `PRScore` object includes both the emoji string (`overall_stars`) and a numeric `star_count` field (integer 1-5) for programmatic use.

## Mode Multipliers

`PRScorer.apply_mode_multipliers(findings, review_modes)` adjusts finding severities before scoring. It returns a **new list** of Finding objects — the original list is not mutated.

### Mode Stacking

Multipliers use **independent `if` blocks** (not `elif`), so multiple modes stack when active simultaneously. This was a deliberate fix from an earlier `elif` chain that only applied the first matching mode. Now a PR with both security and migration modes active will have security findings elevated to critical by migration mode *and* doubled by security mode.

For each finding, all matching mode checks fire independently. The strictest (highest severity) result across all candidates wins:

```python
# Migration dominates — elevates everything to critical
if 'migration' in modes:
    severity = 'critical'
else:
    candidates = [f.severity]
    # Security: warning -> critical for security-category findings
    if 'security' in modes and f.category == 'security':
        if f.severity == 'warning': candidates.append('critical')
    # Performance: warning -> critical for performance-category findings
    if 'performance' in modes and f.category == 'performance':
        if f.severity == 'warning': candidates.append('critical')
    # Architecture: suggestion -> warning for best_practices/architecture findings
    if 'architecture' in modes and f.category in ('best_practices', 'architecture'):
        if f.severity == 'suggestion': candidates.append('warning')
    severity = max(candidates, key=severity_order)
```

### Multiplier Table

| Mode | Affected category | Effect |
|------|------------------|--------|
| security | security | `warning` -> `critical` |
| performance | performance | `warning` -> `critical` |
| architecture | best_practices, architecture | `suggestion` -> `warning` |
| migration | all categories | all severities elevated to `critical` |

## Tier-Aware Tool Budget

The agent's tool call budget scales with PR size. Tier is assessed by the agent based on the number of changed files:

| Tier | Files Changed | Tool Call Budget |
|------|--------------|-----------------|
| T1 | 1-3 | 25 |
| T2 | 4-10 | 40 |
| T3 | 11-25 | 60 |
| T4 | 26-50 | 80 |
| T5 | 51+ | 100 |

The tier is recorded in `findings.json` and the `tool_calls` field tracks actual usage for monitoring.

## PR Size Normalization

Raw penalty totals are divided by `sqrt(file_count)` to produce a normalized score:

```
normalized_penalty = total_penalty / sqrt(file_count)
```

This prevents large PRs (many files, many findings) from being unfairly scored lower than small PRs. The square root provides a balance -- larger PRs are allowed proportionally more findings, but not linearly more.

**Example:**
- PR with 4 files, 20 penalty points: `20 / sqrt(4) = 10.0` normalized
- PR with 16 files, 40 penalty points: `40 / sqrt(16) = 10.0` normalized
- Both receive the same star rating despite different raw totals

The normalized penalty is used for star rating calculation. Raw penalty is still available in the breakdown.

## Score Persistence

After scoring, the current score is saved to `.cr/prior_score.json`:

```json
{
  "total_penalty": 12.5,
  "normalized_penalty": 6.25,
  "star_count": 4,
  "file_count": 4,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

On subsequent runs, this file provides the "before" baseline for trend comparison. This works independently of fix_verifications -- even a fresh re-push (no fix verification) can show score improvement.

## Fix Verification Scoring

When fix verifications are present, the scoring system handles two additional statuses beyond fixed/still_present/not_relevant:

| Status | Penalty Effect |
|--------|---------------|
| `fixed` | Finding removed from penalty calculation |
| `still_present` | Full penalty retained |
| `not_relevant` | Finding removed from penalty calculation |
| `justified` | Zero penalty -- developer provided valid reasoning |
| `deferred` | ~50% penalty via `_downgrade_severity` |

### Justified (zero penalty)

When a developer replies to a finding with a justification and the agent marks it `justified`, the finding's penalty is zeroed out entirely. The finding still appears in the summary for visibility but does not affect the star rating.

### Deferred (severity downgrade)

When a developer acknowledges a finding but defers the fix (marked `deferred`), the severity is downgraded one level:
- `critical` -> `warning`
- `warning` -> `suggestion`
- `suggestion` -> `suggestion` (unchanged — already lowest actionable severity)

This reduces the penalty for critical and warning findings by approximately 50% while still reflecting that the issue exists.

## Score Comparison (Re-push)

`ScoreComparisonService.format_as_markdown()` produces a before/after comparison when fix verifications are present:

```
Before fix:  ★★★☆☆  3/5 stars  (4 findings: 2 critical, 2 warning)
After fix:   ★★★★☆  4/5 stars  (2 findings: 0 critical, 2 warning)
Improvement: +1 star -- 2 findings resolved
```

This comparison is included in the PR summary comment on re-push.

## Schema Versioning

`findings.json` includes a `schema_version` field (string, currently `"1.0"`). The poster validates this in `run()` and rejects files with a mismatched version, ensuring Phase 1 and Phase 2 stay in sync across container upgrades.

## Zero-Finding Baseline

0 findings = 5 stars. The scorer never exceeds 5 stars even with "good" signals.

## API

```python
from pr_scorer import PRScorer
scorer = PRScorer(penalty_matrix=penalty_matrix, star_thresholds=star_thresholds)
adjusted = scorer.apply_mode_multipliers(findings, review_modes=["security"])
result = scorer.calculate_pr_score(adjusted)
# result.star_count: int (1-5)
# result.overall_stars: str (emoji string)
# result.total_penalty: float
# result.quality_level: str ("Perfect", "Excellent", etc.)
# result.category_penalties: dict
# result.issues_by_severity: dict
```
