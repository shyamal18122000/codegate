# Feature: Learning System

## Overview

CodeGate's learning system allows the review engine to adapt to project-specific patterns over time. It has two sides: **read** (suppress findings that match known dismissals) and **write** (auto-learn new dismissals from developer feedback). The system is file-based -- all state lives in the `.codereview/` directory in the repository.

## Directory Structure

```
.codereview/
  config.yml              # Learning system configuration
  conventions.md          # Team coding conventions (read by agent)
  dismissed.jsonl         # Dismissed finding patterns (one JSON object per line)
  learned-patterns.jsonl  # Auto-generated patterns from repeated dismissals
  modules/
    auth.yml              # Per-module config for src/auth/**
    api.yml               # Per-module config for src/api/**
```

## Dismissed Findings (`dismissed.jsonl`)

Each line in `dismissed.jsonl` is a JSON object representing a pattern to suppress:

```jsonl
{"glob": "src/auth/**", "category": "security", "regex": "broad exception", "dismiss_count": 3, "scope": "module", "reason": "Intentional top-level error handler", "created": "2024-01-10T10:00:00Z"}
{"glob": "src/utils/eval_sandbox.py", "category": "security", "regex": "eval\\(", "dismiss_count": 1, "scope": "file", "reason": "Sandboxed eval in controlled environment", "created": "2024-01-12T14:30:00Z"}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `glob` | yes | File path glob pattern to match (e.g., `src/auth/**`, `*.py`) |
| `category` | yes | Finding category to match (e.g., `security`, `best_practices`) |
| `regex` | no | Regex pattern to match against the finding message. If omitted, all findings matching glob + category are suppressed. |
| `dismiss_count` | yes | Number of times this pattern has been dismissed |
| `scope` | yes | `file` or `module` -- determines matching breadth |
| `reason` | no | Human-readable reason for the dismissal |
| `created` | yes | ISO 8601 timestamp of first dismissal |
| `last_dismissed` | no | ISO 8601 timestamp of most recent dismissal |

### Matching Logic

During the filter pipeline, each finding is checked against all dismissal rules:

1. **Glob match** -- finding's file path is tested against the rule's `glob` pattern
2. **Category match** -- finding's category must equal the rule's `category`
3. **Regex match** (if present) -- finding's message is tested against the rule's `regex`

All present conditions must match for suppression. A rule with only `glob` and `category` suppresses all findings of that category in matching files.

## Learned Patterns (`learned-patterns.jsonl`)

Auto-generated patterns from repeated dismissals. Format:

```jsonl
{"pattern_id": "lp-001", "glob": "src/auth/**", "category": "security", "regex": "broad exception", "confidence": 0.85, "generated_from": 3, "created": "2024-01-15T10:00:00Z"}
```

Learned patterns are consumed by the agent during review -- they serve as additional context about known false positive patterns in the codebase.

## Per-Module Config (`modules/*.yml`)

Each YAML file in `.codereview/modules/` configures review behavior for files matching a glob pattern:

```yaml
# .codereview/modules/auth.yml
glob: "src/auth/**"
severity_shift: +1          # Elevate all severities by 1 level
never_flag:
  - code_style              # Don't flag code style in auth module
always_flag:
  - security                # Always flag security regardless of confidence
focus_categories:
  - security
  - best_practices
min_confidence: 0.6         # Lower threshold for auth module
force_modes:
  - security                # Always activate security mode for auth files
max_per_file: 8             # Allow more findings per file in auth module
```

### Module Config Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `glob` | string | required | File path glob pattern this module applies to |
| `severity_shift` | integer | 0 | Shift severity up (+) or down (-). +1 means suggestion->warning, warning->critical |
| `never_flag` | list[string] | [] | Categories to never flag in this module |
| `always_flag` | list[string] | [] | Categories to always include regardless of confidence |
| `focus_categories` | list[string] | [] | Categories to prioritize when cap is hit |
| `min_confidence` | float | 0.7 | Override minimum confidence threshold |
| `force_modes` | list[string] | [] | Review modes to always activate for this module |
| `max_per_file` | integer | 5 | Override per-file finding cap |

## Learning Write-Back

### Auto-Detect from Justified Verifications

When `post_findings.py` processes a `justified` fix verification:

1. Extract the finding's file, category, and message
2. Search `dismissed.jsonl` for a matching existing rule
3. **If match found:** increment `dismiss_count`, update `last_dismissed`
4. **If no match:** create a new entry with `dismiss_count: 1`, `scope: "file"`, glob set to the exact file path

### Scope Escalation

When `dismiss_count` reaches 3 for a file-scoped dismissal:
- The scope is escalated from `file` to `module`
- The `glob` is broadened from the exact file path to the containing directory pattern (e.g., `src/auth/login.py` -> `src/auth/**`)

### Auto-Pattern Generation

When a module-scoped dismissal reaches `dismiss_count >= 3`:
- A new entry is written to `learned-patterns.jsonl`
- The pattern includes the glob, category, and regex from the dismissal
- `generated_from` records how many dismissals triggered the pattern

### Delivery Configuration

The `learning_delivery` field in `.codereview/config.yml` controls how learning artifacts are delivered:

| Value | Behavior |
|-------|----------|
| `comment` (default) | Learning changes are posted as a PR comment summarizing what was learned |
| `commit` | Learning changes are committed directly to the repo branch |
| `none` | Learning is disabled; no write-back occurs |

```yaml
# .codereview/config.yml
learning_delivery: comment
```

## `cr learn` CLI

The `cr learn` command provides manual management of the learning system:

### Commands

```bash
# List all current dismissals and learned patterns
cr learn --list

# Add a manual dismissal rule
cr learn --add --glob "src/utils/**" --category "code_style" --reason "Team prefers this style"

# Remove a dismissal rule by index or pattern
cr learn --remove --glob "src/utils/**" --category "code_style"

# Analyze current dismissals: show stats, suggest escalations
cr learn --analyze

# Show learning system statistics
cr learn --stats
```

### `--list` Output

```
Dismissed findings (dismissed.jsonl): 12 rules
  #1  src/auth/**          security    /broad exception/     count=5  scope=module
  #2  src/utils/eval.py    security    /eval\(/              count=1  scope=file
  ...

Learned patterns (learned-patterns.jsonl): 3 patterns
  #1  src/auth/**          security    /broad exception/     confidence=0.85
  ...
```

### `--stats` Output

```
Learning System Statistics
  Dismissal rules:     12
  Learned patterns:     3
  File-scoped:          7
  Module-scoped:        5
  Most dismissed:       src/auth/** / security (5 dismissals)
  Last updated:         2024-01-15T10:00:00Z
```

### `--analyze` Output

```
Escalation candidates (file-scoped with dismiss_count >= 2):
  src/auth/login.py  security  /broad exception/  count=2  -> will escalate at 3

Pattern candidates (module-scoped with dismiss_count >= 2):
  src/auth/**  security  /broad exception/  count=4  -> pattern already generated
```
