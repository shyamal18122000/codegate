# Feature: Review Modes

## Overview

CodeGate supports 6 review modes. Four modes have dedicated prompt files; architecture and performance modes are handled via scoring multipliers only (no dedicated prompt files yet). The agent auto-detects the active mode(s) from changed file paths and PR labels. Multiple modes can be active simultaneously; mode multipliers stack using independent checks (the strictest multiplier per finding wins).

## Modes

| Mode | File | Detection signals |
|------|------|-------------------|
| standard | `commands/review-mode-standard.md` | Default; active when no other mode is detected |
| security | `commands/review-mode-security.md` | Auth/crypto files, secrets patterns in diff |
| architecture | _(scoring multiplier only)_ | Interface files, API contracts, breaking changes |
| performance | _(scoring multiplier only)_ | Hot paths, queries, memory-sensitive code |
| migration | `commands/review-mode-migration.md` | `.sql` files, migration scripts, DDL changes |
| docs/chore | `commands/review-mode-docs-chore.md` | Only `.md`/`.yml`/`.json` files changed, or PR label "docs"/"chore" |

## Auto-Detection Rules (Step 3 of review-pr-core.md)

The agent evaluates changed file paths and PR labels to set `review_modes[]` in `findings.json`. Each mode is checked independently (not elif) so multiple modes can match:
- **migration**: any `.sql` file or file path matching `*migration*`, `*migrate*`, `*schema*`
- **security**: auth files, crypto files, secrets handling, `*password*`, `*token*`, `*secret*`
- **architecture**: interface files, API contracts, >10 files changed
- **performance**: query files, cache files, index files
- **docs/chore**: all changed files are `.md`, `.yml`, `.json`, `.txt`, or PR label is "docs" or "chore"
- **standard**: always active unless docs/chore mode is the only mode detected

## Scoring Multipliers

Mode multipliers are applied in `PRScorer.apply_mode_multipliers(findings, review_modes)`. Multipliers use independent `if` blocks (not elif) so they stack:

| Mode | Affected category | Multiplier |
|------|------------------|------------|
| security | security | x2 |
| performance | performance | x2 |
| architecture | best_practices | x1.5 |
| migration | all | elevate to minimum critical |

When multiple multipliers apply to the same finding, the strictest (highest) multiplier is used.

## Mode Prompt Files

Each mode file contains a focused checklist. The agent reads the relevant mode file(s) during Step 3 and uses them to guide its file-by-file review in Step 5. The docs/chore mode explicitly specifies light-touch review -- deep code analysis is skipped.

## Intent Markers

Code can opt out of review using inline markers. The agent checks for these before flagging findings:

| Marker | Effect |
|--------|--------|
| `# cr: intentional` | Skip this line |
| `# cr: ignore-next-line` | Skip the next line |
| `# cr: ignore-block start` ... `# cr: ignore-block end` | Skip the entire block |

Intent markers work across all languages. The comment prefix (`#`, `//`, `/* */`) is language-appropriate; the `cr:` keyword is the detection signal.

When the agent encounters an intent marker, the finding is moved to `suppressed_findings[]` in findings.json with `dismissed_id: "intent-marker"`. Phase 2 (`post_findings.py`) classifies these into the `intent_marker` bucket and displays the count in the summary. See [post-findings.md](post-findings.md) for the full suppression flow.

## Per-Module Mode Forcing

Per-module config files (`.codereview/modules/*.yml`) can force specific modes for files matching a module's glob pattern via the `force_modes` field. For example, a module config for `src/auth/**` could force `security` mode regardless of auto-detection.
