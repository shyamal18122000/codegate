# Feature: Rules Engine

## Overview

The rules engine provides deterministic, LLM-free checks that run before the agent review. Rules are defined in `.codereview-rules.yml` at the repository root. Rule violations are added as findings and tracked separately from LLM-generated findings.

## Configuration File

```yaml
# .codereview-rules.yml
rules:
  - id: no-eval
    type: forbidden_pattern
    pattern: "eval\\("
    severity: critical
    message: "eval() is forbidden -- use ast.literal_eval() for safe parsing"
    glob: "**/*.py"

  - id: no-requests-direct
    type: forbidden_import
    pattern: "^import requests$|^from requests import"
    severity: warning
    message: "Use the internal HttpClient wrapper instead of requests directly"
    glob: "src/**/*.py"

  - id: license-header
    type: required
    pattern: "# Copyright \\d{4}"
    severity: suggestion
    message: "All source files must include a copyright header"
    glob: "src/**/*.py"
```

## Rule Types

| Type | Description | Violation condition |
|------|-------------|-------------------|
| `forbidden_pattern` | Regex pattern that must NOT appear in matching files | Pattern found in a changed file |
| `forbidden_import` | Import statement that must NOT appear | Import pattern found in a changed file |
| `required` | Regex pattern that MUST appear in matching files | Pattern NOT found in a changed file |

## Rule Schema

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `id` | yes | string | Unique identifier for the rule (used in `rules_checked[]`) |
| `type` | yes | string | `forbidden_pattern`, `forbidden_import`, or `required` |
| `pattern` | yes | string | Regex pattern to match |
| `severity` | yes | string | `critical`, `warning`, or `suggestion` |
| `message` | yes | string | Human-readable message explaining the rule and fix |
| `glob` | no | string | File glob pattern to scope the rule (default: `**/*`) |
| `category` | no | string | Finding category (default: `best_practices`) |

## Agent Execution Flow

Rules are applied in **Step 5a-rules** of the agent review, before the LLM file-by-file review (Step 5):

1. Load `.codereview-rules.yml` from `/workspace/`
2. For each changed file in the PR:
   a. Filter rules to those whose `glob` matches the file path
   b. For `forbidden_pattern` and `forbidden_import` rules: scan the file for the pattern. If found, create a finding at the matching line.
   c. For `required` rules: scan the file for the pattern. If NOT found, create a finding at line 1.
3. Rule-generated findings are added to `findings[]` with an internal marker indicating they came from rules (not LLM).
4. All evaluated rule IDs are recorded in `rules_checked[]` in findings.json.

## findings.json Integration

### rules_checked Array

The `rules_checked` array in findings.json lists all rule IDs that were evaluated:

```json
{
  "rules_checked": ["no-eval", "no-requests-direct", "license-header"],
  "findings": [
    {
      "id": null,
      "file": "src/utils/parser.py",
      "line": 42,
      "severity": "critical",
      "category": "best_practices",
      "title": "Rule violation: no-eval",
      "message": "eval() is forbidden -- use ast.literal_eval() for safe parsing",
      "confidence": 1.0,
      "source": "rule"
    }
  ]
}
```

Rule-generated findings always have `confidence: 1.0` (deterministic match) and include the rule ID in the title.

## PR Summary -- Rules Compliance Section

The PR summary includes a "Rules Compliance" section:

```markdown
### Rules Compliance

| Rule | Status | Files |
|------|--------|-------|
| no-eval | PASS | 0 violations |
| no-requests-direct | FAIL | 1 violation in src/utils/http.py |
| license-header | FAIL | 2 files missing header |
```

## Interaction with Other Features

- **Learning system:** Rule violations are not eligible for dismissal. They are deterministic and must be fixed.
- **Suppression:** Rule findings cannot be suppressed via `dismissed.jsonl` or intent markers. The only way to "dismiss" a rule is to remove it from `.codereview-rules.yml`.
- **Scoring:** Rule findings are scored identically to LLM findings -- they receive penalty points based on severity and category.
- **Mode multipliers:** Rule findings are subject to mode multipliers like any other finding.

## Best Practices for Rules

- Use rules for patterns that are always wrong in your codebase (no exceptions).
- Use the learning system (dismissals) for patterns that are sometimes acceptable.
- Keep rule count reasonable (< 50) to avoid slow entrypoint times.
- Prefer `forbidden_pattern` with a specific regex over broad patterns that generate false positives.
- Use `glob` to scope rules to relevant file types and directories.
