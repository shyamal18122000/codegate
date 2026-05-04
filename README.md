# CodeGate

AI-powered code review for pull requests. CodeGate runs as a Docker container in your CI pipeline, reviews changed files with an LLM agent, then deterministically scores, filters, and posts findings as inline PR comments.

## Key Features

- **Two-phase architecture** -- LLM agent writes findings; deterministic Python engine scores and posts. The agent never touches VCS APIs; the poster never touches the LLM.
- **Six review modes** -- standard, security, architecture, performance, migration, docs/chore. Auto-detected from file paths and PR labels; modes stack.
- **Penalty-based scoring** -- 1-5 star rating with per-category penalty breakdown. Size-normalized via `sqrt(file_count)` for fair comparison across PRs.
- **Fix verification** -- on re-push, classifies prior findings as fixed, still_present, not_relevant, justified, or deferred. Before/after score comparison in summary.
- **Learning system** -- dismissed findings (`.codereview/dismissed.jsonl`), learned patterns, per-module config. Auto-generates patterns after 3 dismissals. `cr learn` CLI for manual management.
- **Rules engine** -- deterministic `.codereview-rules.yml` for forbidden patterns, forbidden imports, and required patterns. Applied before LLM review.
- **Duplicate detection** -- AST-based signature extraction (`tools/extract_signatures.py`) identifies copy-paste code before the agent runs.
- **Intent markers** -- `# cr: intentional`, `ignore-next-line`, `ignore-block` to suppress known patterns.
- **Partial failure recovery** -- `.cr/posted.jsonl` journal tracks posted comments; safe to re-run after partial failures.
- **Real cost tracking** -- `token_usage` in findings.json with per-model pricing.
- **Idempotent** -- cr-id dedup (`sha1(file:line:category)[:8]`) ensures re-runs never duplicate comments.

## Quick Start

### GitHub Actions

```yaml
# .github/workflows/codegate.yml
name: Code Review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  pull-requests: write
  contents: read

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run CodeGate
        run: |
          docker run --rm \
            -v "${{ github.workspace }}:/workspace" \
            -e PR_ID=${{ github.event.pull_request.number }} \
            -e REPO=${{ github.repository }} \
            -e VCS=github \
            -e GH_TOKEN=${{ secrets.GITHUB_TOKEN }} \
            -e COMMIT_ID=${{ github.event.pull_request.head.sha }} \
            -e AGENT=codex \
            -e OPENAI_API_KEY=${{ secrets.OPENAI_API_KEY }} \
            ghcr.io/shyamal18122000/codegate:latest
```

### Azure DevOps

See [docs/features/ci-integration.md](docs/features/ci-integration.md) for the full Azure Pipelines YAML.

### Configuration

Drop these files in your repo root:

| File | Purpose |
|------|---------|
| `.codereview.md` | Coding conventions, focus areas, anti-patterns for the agent |
| `.codereview.yml` | Gate thresholds (`min_star_rating`, `fail_on_critical`, `fail_on_suppressed_security`) |
| `.codereview-rules.yml` | Deterministic rules (forbidden patterns, required patterns, forbidden imports) |
| `.codereview/config.yml` | Learning system config (dismissal matching, write-back settings) |
| `.codereview/conventions.md` | Team conventions consumed by the learning system |
| `.codereview/modules/*.yml` | Per-module overrides (severity shifts, focus categories, forced modes) |

## Architecture

```
CI Pipeline
  |
  v
+---------------------------+     +---------------------------+
| Phase 1: Agent Review     |     | Phase 2: Post Findings    |
| (LLM in Docker container) | --> | (Deterministic Python)    |
| Writes findings.json      |     | Scores, filters, posts    |
+---------------------------+     +---------------------------+
         |                                   |
         v                                   v
  .cr/findings.json              PR inline comments + summary
```

**Phase 1** runs the LLM agent (Codex, Claude, or Gemini) inside Docker. The agent reads the PR diff, applies review mode checklists, checks intent markers, and writes `.cr/findings.json`. Before the agent runs, `tools/extract_signatures.py` extracts function signatures for duplicate detection.

**Phase 2** runs `post_findings.py` which: validates findings, applies dismissed-findings matching, runs the filter pipeline (confidence, cap, dedup, suppression), scores with size normalization, posts inline comments with cr-id markers, handles fix verifications (including justified/deferred), writes back learned dismissals, and updates the PR summary.

## Review Modes

| Mode | Trigger | Effect |
|------|---------|--------|
| standard | Default | Baseline code review |
| security | Auth/crypto files, `security` label | Security findings penalty x2 |
| performance | Query/cache files, `performance` label | Performance findings penalty x2 |
| architecture | API/interface files, `architecture` label | Best practices penalty x1.5 |
| migration | `.sql` files, migration paths | All findings elevated to critical |
| docs/chore | Only doc/config files changed | Light-touch review, max 10 findings |

Modes stack. A PR touching auth files and SQL migrations activates both security and migration modes.

## Scoring

Penalty-based: each finding deducts points based on severity and category. The total penalty is normalized by `sqrt(file_count)` for fair cross-PR comparison. Score persistence (`.cr/prior_score.json`) enables before/after tracking.

The agent assigns a tier (T1-T5) based on PR size, which determines the tool call budget:

| Tier | Files | Budget |
|------|-------|--------|
| T1 | 1-3 | 25 |
| T2 | 4-10 | 40 |
| T3 | 11-25 | 60 |
| T4 | 26-50 | 80 |
| T5 | 51+ | 100 |

See [docs/features/scoring.md](docs/features/scoring.md) for details.

## Learning System

CodeGate learns from developer responses:

1. When a developer justifies a finding and it is marked `justified`, the finding is auto-added to `dismissed.jsonl`
2. After 3 dismissals of the same pattern, scope escalates from file to module level
3. After 3 module-level dismissals, a learned pattern is auto-generated in `learned-patterns.jsonl`
4. The `cr learn` CLI provides manual management: `--list`, `--add`, `--remove`, `--analyze`, `--stats`

See [docs/features/learning-system.md](docs/features/learning-system.md) for the full specification.

## Rules Engine

Deterministic rules in `.codereview-rules.yml` run before the LLM review:

```yaml
rules:
  - id: no-eval
    type: forbidden_pattern
    pattern: "eval\\("
    severity: critical
    message: "eval() is forbidden — use ast.literal_eval() for safe parsing"
    glob: "**/*.py"
```

See [docs/features/rules-engine.md](docs/features/rules-engine.md) for all rule types.

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | System design, findings.json schema, trade-offs |
| [Scoring](docs/features/scoring.md) | Penalty matrix, mode multipliers, size normalization |
| [Post Findings](docs/features/post-findings.md) | Phase 2 processing pipeline |
| [Fix Verification](docs/features/fix-verification.md) | Re-push flow, justified/deferred statuses |
| [Review Modes](docs/features/review-modes.md) | Mode detection and checklists |
| [Learning System](docs/features/learning-system.md) | Dismissed findings, auto-patterns, cr learn CLI |
| [Rules Engine](docs/features/rules-engine.md) | Deterministic rules (.codereview-rules.yml) |
| [Duplicate Detection](docs/features/duplicate-detection.md) | Signature extraction and copy-paste detection |
| [CI Integration](docs/features/ci-integration.md) | GitHub Actions and Azure Pipelines setup |
| [VCS CLI](docs/features/vcs-cli.md) | Agent-facing VCS command interface |

## License

MIT
