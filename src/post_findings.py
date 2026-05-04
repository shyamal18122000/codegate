"""
post_findings.py — Phase 2 engine for CODEGATE.

Reads a findings.json produced by the review agent, validates it against
findings-schema.json, filters/caps/deduplicates findings, scores the PR,
posts inline comments and a summary to the VCS, and outputs structured JSON
for CI gating.

Usage:
    python src/post_findings.py --findings /workspace/.cr/findings.json [--dry-run]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent.parent / "commands" / "findings-schema.json"
MIN_CONFIDENCE = 0.7
MAX_TOTAL_FINDINGS = 30
MAX_PER_FILE = 5
CODEREVIEW_YML = ".codereview.yml"

# Advisory pricing table (USD per 1M tokens). Update periodically.
# Last updated: 2026-05-02 — update when model pricing changes
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "codex":   {"input": 3.00,  "output": 12.00},
    "claude":  {"input": 3.00,  "output": 15.00},
    "gemini":  {"input": 1.25,  "output": 5.00},
    "default": {"input": 3.00,  "output": 15.00},
}


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def _compute_cost_estimate(token_usage, agent: Optional[str]) -> Optional[str]:
    """Return advisory cost string like '$0.1234', or None when token_usage is absent."""
    if token_usage is None:
        return None
    pricing = MODEL_PRICING.get(agent or "", MODEL_PRICING["default"])
    cost = (
        token_usage.input_tokens / 1_000_000 * pricing["input"]
        + token_usage.output_tokens / 1_000_000 * pricing["output"]
    )
    return f"${cost:.4f}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: Path) -> List[dict]:
    """Read a JSONL file, returning list of dicts. Empty list if missing/empty."""
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                _eprint(f"Warning: skipping malformed JSONL line {lineno} in {path}")
    return entries


def _write_jsonl(path: Path, entries: List[dict]) -> None:
    """Write a list of dicts as JSONL, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _append_jsonl(path: Path, entry: dict) -> None:
    """Append a single dict as a JSON line, creating the file if missing.

    Public utility for future use by the ``cr learn accept`` CLI (Phase 21).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _compute_dismissed_id(file_path: str, category: str, title: str) -> str:
    """Return a stable, deterministic dismissed_id: 'd-' + sha1(file:category:title)[:8]."""
    digest = hashlib.sha1(f"{file_path}:{category}:{title}".encode()).hexdigest()
    return f"d-{digest[:8]}"


def _common_ancestor_glob(file_patterns: List[str]) -> str:
    """Return the common ancestor directory glob for a list of file paths."""
    if not file_patterns:
        return "**"
    # Normalise to forward slashes so globs are portable
    normalised = [p.replace("\\", "/") for p in file_patterns]
    if len(normalised) == 1:
        parts = normalised[0].rsplit("/", 1)
        parent = parts[0] if len(parts) > 1 else "."
        return f"{parent}/**" if parent != "." else "**"
    parts_list = [p.split("/") for p in normalised]
    common: List[str] = []
    for level in zip(*parts_list):
        if len(set(level)) == 1:
            common.append(level[0])
        else:
            break
    if common:
        return "/".join(common) + "/**"
    return "**"


def _generate_learned_patterns(
    dismissed_entries: List[dict], existing_patterns: List[dict]
) -> List[dict]:
    """
    Auto-generate learned patterns from dismissed_entries.

    Groups by (category, title_pattern). If total dismiss_count >= 3
    and no existing pattern_id matches, create a new LearnedPattern.
    """
    existing_ids = {p["pattern_id"] for p in existing_patterns}

    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for entry in dismissed_entries:
        key = (entry["category"], entry["title_pattern"])
        groups[key].append(entry)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_patterns: List[dict] = []

    for (category, title_pattern), entries in groups.items():
        total_count = sum(e.get("dismiss_count", 1) for e in entries)
        if total_count < 3:
            continue
        pattern_id = "lp-" + hashlib.sha1(f"{category}:{title_pattern}".encode()).hexdigest()[:8]
        if pattern_id in existing_ids:
            continue
        file_patterns = [e["file_pattern"] for e in entries]
        module = _common_ancestor_glob(file_patterns)
        evidence = [e["dismissed_id"] for e in entries]
        new_patterns.append({
            "pattern_id": pattern_id,
            "module": module,
            "category": category,
            "description": f"Auto-learned: '{title_pattern}' dismissed {total_count}x",
            "source": "dismissed_3x",
            "evidence": evidence,
            "created_at": now,
            "confidence_modifier": -0.3,
        })

    return new_patterns


def _merge_dismissals(
    existing: List[dict], new_dismissals: List[dict]
) -> Tuple[List[dict], List[dict]]:
    """
    Merge new dismissals into existing list.

    Returns (full_updated_list, changed_entries_only).
    Escalation: scope "file" with dismiss_count >= 3 → widened to "module"
    (file_pattern becomes parent-dir glob, e.g. src/auth/login.py → src/auth/**).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index = {e["dismissed_id"]: e for e in existing}
    changed: List[dict] = []

    for nd in new_dismissals:
        did = nd["dismissed_id"]
        if did in index:
            entry = index[did]
            entry["dismiss_count"] = entry.get("dismiss_count", 1) + 1
            entry["dismissed_at"] = now
            if entry.get("scope", "file") == "file" and entry["dismiss_count"] >= 3:
                fp = entry["file_pattern"].replace("\\", "/")
                parts = fp.rsplit("/", 1)
                parent = parts[0] if len(parts) > 1 else "."
                entry["file_pattern"] = f"{parent}/**" if parent != "." else "**"
                entry["scope"] = "module"
            changed.append(entry)
        else:
            entry = dict(nd)
            index[did] = entry
            changed.append(entry)

    return list(index.values()), changed


def _detect_dismissals(findings_file) -> List[dict]:
    """
    Detect dismissals from fix_verifications with status == "justified".

    Builds the finding lookup from ALL findings_file.findings (not just capped)
    to avoid missing pruned findings.
    """
    all_findings_by_id = {f.id: f for f in findings_file.findings}
    dismissals = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for fv in findings_file.fix_verifications:
        if fv.status != "justified":
            continue
        finding = all_findings_by_id.get(fv.cr_id)
        if finding is None:
            _eprint(f"Warning: justified fix_verification cr_id '{fv.cr_id}' not found in findings; skipping")
            continue
        dismissed_id = _compute_dismissed_id(finding.file, finding.category, finding.title)
        dismissals.append({
            "dismissed_id": dismissed_id,
            "file_pattern": finding.file,
            "category": finding.category,
            "title_pattern": finding.title,
            "reason": fv.reason,
            "dismissed_by": "auto:phase2",
            "dismissed_at": now,
            "dismiss_count": 1,
            "scope": "file",
        })
    return dismissals


def _eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def _assign_cr_ids(findings: list) -> None:
    """Assign stable 8-char hex cr-ids based on file:line:category hash.

    Findings that already have a non-null id are left unchanged for backwards
    compatibility.  All others get a deterministic sha1-based id so that the
    same finding always produces the same cr-id regardless of position.
    """
    for finding in findings:
        if finding.id is not None:
            continue
        raw = f"{finding.file}:{finding.line}:{finding.category}".encode()
        finding.id = hashlib.sha1(raw).hexdigest()[:8]


def _gh_run_with_retry(cmd, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
    """
    subprocess.run wrapper with exponential backoff on GitHub rate-limit errors.

    Retries on CalledProcessError when stderr indicates a rate limit (HTTP 429
    or secondary rate limit). All other errors are re-raised immediately.
    """
    import time

    last_exc: Optional[subprocess.CalledProcessError] = None
    for attempt in range(max_retries):
        try:
            return subprocess.run(cmd, **kwargs)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").lower()
            is_rate_limit = (
                "rate limit" in stderr
                or "429" in stderr
                or "secondary rate" in stderr
                or "api rate" in stderr
            )
            if is_rate_limit and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                _eprint(
                    f"GitHub rate limit hit; retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                last_exc = exc
            else:
                raise
    raise last_exc  # type: ignore[misc]


def _validate_schema(data: dict) -> List[str]:
    """
    Validate findings.json against findings-schema.json.

    Returns a list of error messages (empty list = valid).
    Uses jsonschema if available; otherwise falls back to manual required-field check.
    """
    try:
        import jsonschema
        schema = _load_json(str(SCHEMA_PATH))
        try:
            jsonschema.validate(data, schema)
            return []
        except jsonschema.ValidationError as exc:
            return [str(exc.message)]
    except ImportError:
        errors = []
        required = ["schema_version", "pr_id", "repo", "vcs", "review_modes", "findings"]
        for field in required:
            if field not in data:
                errors.append(f"Missing required field: '{field}'")
        if "vcs" in data and data["vcs"] not in ("ado", "github"):
            errors.append(f"Invalid vcs value: '{data['vcs']}'. Must be 'ado' or 'github'.")
        if "findings" in data:
            for i, f in enumerate(data["findings"]):
                for req in ("id", "file", "line", "severity", "category", "title", "message", "confidence"):
                    if req not in f:
                        errors.append(f"findings[{i}] missing required field '{req}'")
        return errors


def _parse_findings_file(data: dict):
    """
    Parse raw dict into FindingsFile dataclass.
    """
    from models.review_models import Finding, FindingsFile, FixVerification, TokenUsage, SuppressedFinding, RuleChecked

    findings = [
        Finding(
            id=f["id"],
            file=f["file"],
            line=f["line"],
            severity=f["severity"],
            category=f["category"],
            title=f["title"],
            message=f["message"],
            confidence=f["confidence"],
            suggestion=f.get("suggestion"),
        )
        for f in data.get("findings", [])
    ]

    fix_verifications = [
        FixVerification(
            cr_id=fv["cr_id"],
            status=fv["status"],
            reason=fv["reason"],
            counter_reason=fv.get("counter_reason"),
            developer_reply=fv.get("developer_reply"),
        )
        for fv in data.get("fix_verifications", [])
    ]

    raw_tu = data.get("token_usage")
    token_usage = (
        TokenUsage(
            input_tokens=raw_tu["input_tokens"],
            output_tokens=raw_tu["output_tokens"],
        )
        if raw_tu
        else None
    )

    suppressed_findings = [
        SuppressedFinding(
            id=sf["id"],
            file=sf["file"],
            line=sf["line"],
            category=sf["category"],
            title=sf["title"],
            reason=sf["reason"],
            dismissed_id=sf.get("dismissed_id"),
            severity=sf.get("severity"),
        )
        for sf in data.get("suppressed_findings", [])
    ]

    rules_checked = [
        RuleChecked.from_dict(rc)
        for rc in data.get("rules_checked", [])
    ]

    return FindingsFile(
        pr_id=data["pr_id"],
        repo=data["repo"],
        vcs=data["vcs"],
        review_modes=data.get("review_modes", []),
        findings=findings,
        fix_verifications=fix_verifications,
        suppressed_findings=suppressed_findings,
        rules_checked=rules_checked,
        tool_calls=data.get("tool_calls", 0),
        agent=data.get("agent"),
        token_usage=token_usage,
    )


# ---------------------------------------------------------------------------
# Filtering and capping
# ---------------------------------------------------------------------------

def filter_by_confidence(findings, min_confidence: float = MIN_CONFIDENCE):
    """Drop findings below min_confidence threshold."""
    return [f for f in findings if f.confidence >= min_confidence]


def cap_findings(findings, max_total: int = MAX_TOTAL_FINDINGS, max_per_file: int = MAX_PER_FILE):
    """
    Cap findings to max_per_file per file (highest severity first) and
    max_total overall (highest severity first).

    Severity order: critical > warning > suggestion
    """
    severity_order = {"critical": 0, "warning": 1, "suggestion": 2}

    sorted_findings = sorted(findings, key=lambda f: (severity_order.get(f.severity, 9), f.file, f.line))

    per_file: Dict[str, int] = defaultdict(int)
    capped: list = []

    for f in sorted_findings:
        if per_file[f.file] >= max_per_file:
            continue
        if len(capped) >= max_total:
            break
        per_file[f.file] += 1
        capped.append(f)

    return capped


# ---------------------------------------------------------------------------
# Filter pipeline
# ---------------------------------------------------------------------------

def _run_filter_pipeline(
    findings: list,
    suppressed_count: int = 0,
    min_confidence: float = MIN_CONFIDENCE,
    max_total: int = MAX_TOTAL_FINDINGS,
    max_per_file: int = MAX_PER_FILE,
) -> Tuple[list, Dict[str, Any]]:
    """
    Run confidence filter + per-file cap + total cap and return (kept, drop_stats).

    drop_stats keys: total_produced, dropped_confidence, dropped_per_file_cap,
                     dropped_total_cap, suppressed, posted.

    Original filter_by_confidence and cap_findings are retained for direct test use.
    """
    total_produced = len(findings) + suppressed_count

    after_confidence = filter_by_confidence(findings, min_confidence)
    dropped_confidence = len(findings) - len(after_confidence)

    severity_order = {"critical": 0, "warning": 1, "suggestion": 2}
    sorted_findings = sorted(
        after_confidence,
        key=lambda f: (severity_order.get(f.severity, 9), f.file, f.line),
    )

    per_file: Dict[str, int] = defaultdict(int)
    kept: list = []
    dropped_per_file_cap = 0
    dropped_total_cap = 0

    for f in sorted_findings:
        if len(kept) >= max_total:
            dropped_total_cap += 1
            continue
        if per_file[f.file] >= max_per_file:
            dropped_per_file_cap += 1
            continue
        per_file[f.file] += 1
        kept.append(f)

    drop_stats: Dict[str, Any] = {
        "total_produced": total_produced,
        "dropped_confidence": dropped_confidence,
        "dropped_per_file_cap": dropped_per_file_cap,
        "dropped_total_cap": dropped_total_cap,
        "suppressed": suppressed_count,
        "posted": len(kept),
    }

    return kept, drop_stats


# ---------------------------------------------------------------------------
# Suppressed findings classification
# ---------------------------------------------------------------------------

def _classify_suppressed_by_source(suppressed_findings: list) -> Dict[str, list]:
    """
    Group suppressed findings into 3 buckets.

    Primary classifier: dismissed_id field.
      "intent-marker"  → intent_marker
      "never-flag"     → never_flag
      anything else    → dismissed_pattern

    Fallback (when dismissed_id is missing/empty): reason string matching.

    Returns dict with keys: intent_marker, dismissed_pattern, never_flag.
    """
    buckets: Dict[str, list] = {"intent_marker": [], "dismissed_pattern": [], "never_flag": []}
    for sf in suppressed_findings:
        did = (sf.dismissed_id or "").strip()
        if did:
            if did == "intent-marker":
                buckets["intent_marker"].append(sf)
            elif did == "never-flag":
                buckets["never_flag"].append(sf)
            else:
                buckets["dismissed_pattern"].append(sf)
        else:
            reason_lower = (sf.reason or "").lower()
            if "intentional" in reason_lower or "intent" in reason_lower:
                buckets["intent_marker"].append(sf)
            elif "never_flag" in reason_lower or "never flag" in reason_lower:
                buckets["never_flag"].append(sf)
            else:
                buckets["dismissed_pattern"].append(sf)
    return buckets


# ---------------------------------------------------------------------------
# .codereview.yml gate thresholds
# ---------------------------------------------------------------------------

def _load_codereview_yml(workspace: str) -> Dict[str, Any]:
    """
    Read .codereview.yml from workspace directory.

    Expected keys (all optional):
        min_star_rating: int  (1-5, default 3)
        fail_on_critical: bool  (default true)
    """
    path = Path(workspace) / CODEREVIEW_YML
    if not path.exists():
        return {}

    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        # Minimal YAML parser: only handle simple key: value lines
        config: Dict[str, Any] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if val.lower() == "true":
                        config[key] = True
                    elif val.lower() == "false":
                        config[key] = False
                    elif val.isdigit():
                        config[key] = int(val)
                    else:
                        try:
                            config[key] = float(val)
                        except ValueError:
                            config[key] = val
        return config
    except Exception as exc:
        _eprint(f"Warning: failed to parse .codereview.yml: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Posted journal — crash-safe recovery for partial failures
# ---------------------------------------------------------------------------

def _load_posted_journal(workspace: str) -> Set[str]:
    """Read .cr/posted.jsonl; return set of cr_ids. Skips corrupt lines."""
    journal_path = Path(workspace) / ".cr" / "posted.jsonl"
    if not journal_path.exists():
        return set()
    cr_ids: Set[str] = set()
    with open(journal_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "cr_id" in entry:
                    cr_ids.add(entry["cr_id"])
            except json.JSONDecodeError:
                pass
    return cr_ids


def _append_posted_journal(workspace: str, cr_id: str, comment_id: Optional[str] = None) -> None:
    """Append a successfully-posted cr_id entry to .cr/posted.jsonl."""
    from datetime import datetime, timezone
    journal_path = Path(workspace) / ".cr" / "posted.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"cr_id": cr_id, "ts": datetime.now(timezone.utc).isoformat()}
    if comment_id is not None:
        entry["comment_id"] = comment_id
    with open(journal_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _cleanup_posted_journal(workspace: str) -> None:
    """Delete .cr/posted.jsonl after all posts succeed."""
    journal_path = Path(workspace) / ".cr" / "posted.jsonl"
    try:
        journal_path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Fetching posted cr-ids from existing threads
# ---------------------------------------------------------------------------

def _fetch_posted_cr_ids_ado(pr_id: int, repo: str) -> Set[str]:
    """Fetch already-posted cr-ids from Azure DevOps threads."""
    from activities.fetch_pr_comments_activity import FetchPRCommentsActivity
    from config import get_settings

    try:
        settings = get_settings()
        activity = FetchPRCommentsActivity(settings=settings)
        threads = activity.execute(pr_id=pr_id, repository_id=repo or None)
        return {t.cr_id for t in threads if t.cr_id}
    except Exception as exc:
        _eprint(f"Warning: failed to fetch existing threads (will post all): {exc}")
        return set()


def _fetch_posted_cr_ids_github(pr_id: int, repo: str) -> Set[str]:
    """Fetch already-posted cr-ids from GitHub PR review comments."""
    import re

    try:
        result = _gh_run_with_retry(
            ["gh", "api", f"repos/{repo}/pulls/{pr_id}/comments", "--jq", ".[].body"],
            capture_output=True, text=True, check=True
        )
        cr_ids: Set[str] = set()
        for body in result.stdout.splitlines():
            match = re.search(r"<!--\s*cr-id:\s*(\S+)\s*-->", body)
            if match:
                cr_ids.add(match.group(1))
        return cr_ids
    except Exception as exc:
        _eprint(f"Warning: failed to fetch GitHub comments (will post all): {exc}")
        return set()


# ---------------------------------------------------------------------------
# Posting comments
# ---------------------------------------------------------------------------

def _post_inline_ado(finding, pr_id: int, repo: str, dry_run: bool) -> bool:
    """Post a single inline comment to Azure DevOps. Returns True on success."""
    if dry_run:
        return True

    from activities.post_pr_comment_activity import PostPRCommentActivity, PostPRCommentInput
    from config import get_settings

    severity_icons = {"critical": "🔴", "warning": "⚠️", "suggestion": "💡"}
    icon = severity_icons.get(finding.severity, "📝")
    body = (
        f"## {icon} {finding.severity.upper()}: {finding.category.replace('_', ' ').title()}\n\n"
        f"**{finding.title}**\n\n"
        f"{finding.message}"
    )
    if finding.suggestion:
        body += f"\n\n**Suggestion:** {finding.suggestion}"
    body += f"\n\n*Confidence: {int(finding.confidence * 100)}%*"
    body += f"\n\n<!-- cr-id: {finding.id} -->"

    settings = get_settings()
    activity = PostPRCommentActivity(settings=settings)
    inp = PostPRCommentInput(
        pr_id=pr_id,
        comment_text=body,
        file_path=finding.file,
        line_number=finding.line,
        repository_id=repo or None,
    )
    try:
        activity.execute(inp)
        return True
    except Exception as exc:
        _eprint(f"Warning: failed to post ADO comment for {finding.id}: {exc}")
        return False


def _post_inline_github(finding, pr_id: int, repo: str, commit_id: str, dry_run: bool) -> bool:
    """Post a single inline comment to GitHub via gh CLI. Returns True on success."""
    if dry_run:
        return True

    severity_icons = {"critical": "🔴", "warning": "⚠️", "suggestion": "💡"}
    icon = severity_icons.get(finding.severity, "📝")
    body = (
        f"## {icon} {finding.severity.upper()}: {finding.category.replace('_', ' ').title()}\n\n"
        f"**{finding.title}**\n\n"
        f"{finding.message}"
    )
    if finding.suggestion:
        body += f"\n\n**Suggestion:** {finding.suggestion}"
    body += f"\n\n*Confidence: {int(finding.confidence * 100)}%*"
    body += f"\n\n<!-- cr-id: {finding.id} -->"

    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": finding.file,
        "line": finding.line,
        "side": "RIGHT",
    }

    try:
        _gh_run_with_retry(
            ["gh", "api", f"repos/{repo}/pulls/{pr_id}/comments",
             "--method", "POST", "--input", "-"],
            input=json.dumps(payload),
            text=True, check=True, capture_output=True
        )
        return True
    except subprocess.CalledProcessError as exc:
        _eprint(f"Warning: failed to post GitHub comment for {finding.id}: {exc.stderr}")
        return False


# ---------------------------------------------------------------------------
# Fix verifications
# ---------------------------------------------------------------------------

def _handle_fix_verifications_ado(fix_verifications, pr_id: int, repo: str, dry_run: bool, gate_config: Optional[Dict[str, Any]] = None):
    """Resolve or reply on ADO threads based on fix verification status."""
    if not fix_verifications or dry_run:
        return

    if gate_config is None:
        gate_config = {}

    allow_deferred = gate_config.get("allow_deferred", True)

    from activities.fetch_pr_comments_activity import FetchPRCommentsActivity
    from activities.post_fix_reply_activity import PostFixReplyActivity
    from config import get_settings

    try:
        settings = get_settings()
        fetch_activity = FetchPRCommentsActivity(settings=settings)
        resolve_activity = PostFixReplyActivity(settings=settings)

        threads = fetch_activity.execute(pr_id=pr_id, repository_id=repo or None)
        thread_by_cr_id = {t.cr_id: t for t in threads if t.cr_id}

        for fv in fix_verifications:
            thread = thread_by_cr_id.get(fv.cr_id)
            if not thread:
                continue

            effective_status = fv.status
            if fv.status == "deferred" and not allow_deferred:
                effective_status = "still_present"

            if effective_status in ("fixed", "justified"):
                msg = (
                    "✅ **Issue Fixed** — Resolved in the latest changes."
                    if effective_status == "fixed"
                    else "✅ **Justified** — Developer justification accepted. Closing."
                )
                try:
                    resolve_activity.execute({
                        "thread_id": thread.thread_id,
                        "pr_id": pr_id,
                        "repository_id": repo or None,
                        "message": msg,
                    })
                except Exception as exc:
                    _eprint(f"Warning: failed to resolve thread for {fv.cr_id}: {exc}")

            elif effective_status == "deferred":
                msg = f"🔖 **Acknowledged — Deferred.** {fv.reason}"
                try:
                    resolve_activity.execute({
                        "thread_id": thread.thread_id,
                        "pr_id": pr_id,
                        "repository_id": repo or None,
                        "message": msg,
                        "resolve": False,
                    })
                except Exception as exc:
                    _eprint(f"Warning: failed to post deferred reply for {fv.cr_id}: {exc}")

            elif effective_status == "still_present" and fv.counter_reason:
                try:
                    resolve_activity.execute({
                        "thread_id": thread.thread_id,
                        "pr_id": pr_id,
                        "repository_id": repo or None,
                        "message": f"❌ **Still present.** {fv.counter_reason}",
                        "resolve": False,
                    })
                except Exception as exc:
                    _eprint(f"Warning: failed to post counter_reason reply for {fv.cr_id}: {exc}")

    except Exception as exc:
        _eprint(f"Warning: fix verification resolution failed: {exc}")


def _handle_fix_verifications_github(fix_verifications, pr_id: int, repo: str, dry_run: bool, gate_config: Optional[Dict[str, Any]] = None):
    """Reply to GitHub PR review comments based on fix verification status."""
    if not fix_verifications or dry_run:
        return

    if gate_config is None:
        gate_config = {}

    allow_deferred = gate_config.get("allow_deferred", True)

    import re

    fv_by_cr_id = {fv.cr_id: fv for fv in fix_verifications}
    actionable_ids = set(fv_by_cr_id.keys())
    if not actionable_ids:
        return

    try:
        result = _gh_run_with_retry(
            ["gh", "api", f"repos/{repo}/pulls/{pr_id}/comments",
             "--jq", "[.[] | {id: .id, body: .body}]"],
            capture_output=True, text=True, check=True
        )
        comments = json.loads(result.stdout)

        for comment in comments:
            body = comment.get("body", "")
            match = re.search(r"<!--\s*cr-id:\s*(\S+)\s*-->", body)
            if not match:
                continue

            cr_id = match.group(1)
            fv = fv_by_cr_id.get(cr_id)
            if not fv:
                continue

            comment_id = comment["id"]
            effective_status = fv.status
            if fv.status == "deferred" and not allow_deferred:
                effective_status = "still_present"

            if effective_status == "fixed":
                reply_body = "✅ **Issue Fixed** — Resolved in the latest changes."
            elif effective_status == "justified":
                reply_body = "✅ **Verified** — Developer justification accepted."
            elif effective_status == "deferred":
                reply_body = f"🔖 **Acknowledged — Deferred.** {fv.reason}"
            elif effective_status == "still_present" and fv.counter_reason:
                reply_body = f"❌ **Still present.** {fv.counter_reason}"
            else:
                continue

            try:
                _gh_run_with_retry(
                    ["gh", "api",
                     f"repos/{repo}/pulls/comments/{comment_id}/replies",
                     "--method", "POST",
                     "-f", f"body={reply_body}"],
                    capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as exc:
                _eprint(f"Warning: failed to reply to GitHub comment {comment_id}: {exc.stderr}")
    except Exception as exc:
        _eprint(f"Warning: GitHub fix verification failed: {exc}")


def _load_prior_score(workspace: str):
    """Load prior score from .cr/prior_score.json, returning a minimal PRScore or None."""
    from models.review_models import PRScore
    prior_path = Path(workspace) / ".cr" / "prior_score.json"
    if not prior_path.exists():
        return None
    try:
        data = json.loads(prior_path.read_text())
        return PRScore(
            total_penalty=float(data["total_penalty"]),
            overall_stars=data.get("star_rating", ""),
            category_penalties=data.get("category_penalties", {}),
            category_stars={},
            issues_by_severity={},
            scoring_breakdown=[],
            quality_level=data.get("quality_level", ""),
        )
    except Exception as exc:
        _eprint(f"Warning: failed to load prior_score.json: {exc}")
        return None


def _write_prior_score(workspace: str, score) -> None:
    """Persist current score to .cr/prior_score.json for before/after comparison."""
    import datetime
    try:
        cr_dir = Path(workspace) / ".cr"
        cr_dir.mkdir(parents=True, exist_ok=True)
        prior = {
            "star_rating": score.overall_stars,
            "total_penalty": score.total_penalty,
            "quality_level": score.quality_level,
            "category_penalties": score.category_penalties,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }
        (cr_dir / "prior_score.json").write_text(json.dumps(prior, indent=2))
    except Exception as exc:
        _eprint(f"Warning: failed to write prior_score.json: {exc}")


def _generate_comparison_md(score, fix_verifications, pr_id: int, old_score=None) -> str:
    """Generate before/after score comparison markdown using ScoreComparisonService."""
    try:
        from score_comparison import ScoreComparisonService
        svc = ScoreComparisonService()
        return svc.format_as_markdown(
            old_score=old_score,
            new_score=score,
            fix_verifications=fix_verifications,
            pr_title=f"PR #{pr_id}",
        )
    except Exception as exc:
        _eprint(f"Warning: failed to generate score comparison: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------

def _build_summary_markdown(
    findings_file,
    filtered_findings: list,
    score,
    gate_result: Dict[str, Any],
    fix_verifications: list,
    comparison_md: str = "",
    cost_estimate: Optional[str] = None,
    drop_stats: Optional[Dict[str, Any]] = None,
    rules_checked: Optional[list] = None,
) -> str:
    severity_counts = {"critical": 0, "warning": 0, "suggestion": 0}
    for f in filtered_findings:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    lines = [
        "<!-- CODEGATE-summary -->",
        "# 🤖 CODEGATE Code Review",
        "",
        f"**PR #{findings_file.pr_id}** · `{findings_file.repo}` · modes: `{', '.join(findings_file.review_modes)}`",
    ]
    if cost_estimate is not None and cost_estimate != "unavailable":
        lines.append(f"*Estimated cost: {cost_estimate}*")
    lines.append("")

    if score:
        lines += [
            "## 📈 PR Quality Score",
            "",
            f"### Overall Rating: {score.overall_stars} ({score.quality_level})",
            f"**Total Penalty: {score.total_penalty:.1f} points** _(Lower is better!)_",
            "",
        ]

    lines += [
        "## 📊 Findings Summary",
        "",
        f"- 🔴 Critical: {severity_counts.get('critical', 0)}",
        f"- ⚠️ Warning: {severity_counts.get('warning', 0)}",
        f"- 💡 Suggestion: {severity_counts.get('suggestion', 0)}",
        f"- Total posted: {len(filtered_findings)} / {MAX_TOTAL_FINDINGS} max",
        "",
    ]

    suppressed_findings = findings_file.suppressed_findings
    if suppressed_findings:
        buckets = _classify_suppressed_by_source(suppressed_findings)
        lines += [
            "## 🔕 Suppressed Findings",
            "",
            f"- Intent markers: {len(buckets['intent_marker'])}",
            f"- Dismissed patterns: {len(buckets['dismissed_pattern'])}",
            f"- Never-flag rules: {len(buckets['never_flag'])}",
            f"- Total suppressed: {len(suppressed_findings)}",
            "",
        ]

    if drop_stats:
        lines += [
            "## 🔬 Finding Pipeline",
            "",
            f"- Total produced (incl. suppressed): {drop_stats.get('total_produced', 0)}",
            f"- Dropped by confidence filter: {drop_stats.get('dropped_confidence', 0)}",
            f"- Dropped by per-file cap: {drop_stats.get('dropped_per_file_cap', 0)}",
            f"- Dropped by total cap: {drop_stats.get('dropped_total_cap', 0)}",
            f"- Suppressed: {drop_stats.get('suppressed', 0)}",
            f"- Posted: {drop_stats.get('posted', 0)}",
            "",
        ]

    if comparison_md:
        lines += [
            "---",
            "",
            comparison_md,
            "",
        ]
    elif fix_verifications:
        fixed = sum(1 for fv in fix_verifications if fv.status == "fixed")
        still = sum(1 for fv in fix_verifications if fv.status == "still_present")
        justified = sum(1 for fv in fix_verifications if fv.status == "justified")
        deferred = sum(1 for fv in fix_verifications if fv.status == "deferred")
        not_relevant = sum(1 for fv in fix_verifications if fv.status == "not_relevant")
        lines += [
            "## 🔄 Fix Verification",
            "",
            f"- ✅ Fixed: {fixed}",
            f"- ❌ Still present: {still}",
            f"- ✔️ Justified: {justified}",
            f"- 🔖 Deferred: {deferred}",
            f"- ➖ Not relevant: {not_relevant}",
            "",
        ]
        if deferred > 0:
            lines += [
                "_Note: Deferred findings carry ~50% reduced penalty._",
                "",
            ]

    if rules_checked:
        lines += ["## 📋 Rules Compliance", ""]
        for rc in rules_checked:
            if rc.findings_generated == 0:
                lines.append(f"- ✅ `{rc.id}` — passed ({rc.applied_to} files checked)")
            else:
                lines.append(f"- ❌ `{rc.id}` — {rc.findings_generated} violation(s) in {rc.applied_to} files")
        lines.append("")

    gate_passed = gate_result.get("passed", True)
    gate_icon = "✅" if gate_passed else "🚨"
    lines += [
        "## 🚦 CI Gate",
        "",
        f"{gate_icon} Gate: **{'PASSED' if gate_passed else 'FAILED'}**",
        "",
    ]

    if gate_result.get("reasons"):
        for reason in gate_result["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by [CODEGATE](https://github.com/shyamal18122000/codegate)*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub summary update-in-place
# ---------------------------------------------------------------------------

_CODEGATE_SUMMARY_MARKER = "<!-- CODEGATE-summary -->"


def _post_or_update_summary_github(
    summary_md: str,
    pr_id: int,
    repo: str,
    dry_run: bool = False,
) -> None:
    """Post a new summary comment or PATCH an existing one in-place.

    Searches existing PR issue comments for the CODEGATE-summary marker.
    If found, updates that comment via PATCH so the PR timeline stays clean.
    If not found, creates a new comment via POST.
    """
    if dry_run:
        return

    import json as _json

    # Fetch existing comments to look for the marker
    list_result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_id}/comments", "--paginate"],
        capture_output=True, text=True,
    )

    existing_comment_id: Optional[int] = None
    if list_result.returncode == 0:
        try:
            comments = _json.loads(list_result.stdout)
            for comment in comments:
                if _CODEGATE_SUMMARY_MARKER in comment.get("body", ""):
                    existing_comment_id = comment["id"]
                    break
        except (ValueError, KeyError):
            pass

    if existing_comment_id is not None:
        # Update the existing comment in place
        _gh_run_with_retry(
            [
                "gh", "api",
                f"repos/{repo}/issues/comments/{existing_comment_id}",
                "--method", "PATCH",
                "--field", f"body={summary_md}",
            ],
            capture_output=True, text=True, check=True,
        )
    else:
        # No existing summary — create a new comment
        _gh_run_with_retry(
            [
                "gh", "api",
                f"repos/{repo}/issues/{pr_id}/comments",
                "--method", "POST",
                "--field", f"body={summary_md}",
            ],
            capture_output=True, text=True, check=True,
        )


# ---------------------------------------------------------------------------
# CI gate
# ---------------------------------------------------------------------------

def _downgrade_severity(severity: str) -> str:
    """Downgrade severity by one level for deferred findings (~50% penalty reduction)."""
    return {"critical": "warning", "warning": "suggestion", "suggestion": "suggestion"}.get(severity, severity)


def _evaluate_gate(score, filtered_findings: list, gate_config: Dict[str, Any], suppressed_findings: list = None) -> Dict[str, Any]:
    """
    Evaluate CI gate conditions against .codereview.yml thresholds.

    Returns dict: { "passed": bool, "reasons": List[str] }
    """
    passed = True
    reasons = []

    # fail_on_critical (default: True) — justified findings are excluded
    fail_on_critical = gate_config.get("fail_on_critical", True)
    if fail_on_critical:
        critical_count = sum(
            1 for f in filtered_findings
            if f.severity == "critical"
        )
        if critical_count > 0:
            passed = False
            reasons.append(f"Gate failed: {critical_count} critical finding(s) present")

    # fail_on_suppressed_security (default: False)
    if gate_config.get("fail_on_suppressed_security", False) and (suppressed_findings or []):
        suppressed_security = [
            sf for sf in suppressed_findings
            if sf.category == "security"
        ]
        if suppressed_security:
            passed = False
            reasons.append(
                f"Gate failed: {len(suppressed_security)} suppressed security finding(s) present"
            )

    # min_star_rating (default: 0 = disabled)
    min_stars = gate_config.get("min_star_rating", 0)
    if min_stars and score:
        actual_stars = score.star_count
        if actual_stars < min_stars:
            passed = False
            reasons.append(
                f"Gate failed: star rating {actual_stars} below minimum {min_stars}"
            )

    return {"passed": passed, "reasons": reasons}


# ---------------------------------------------------------------------------
# Learning comment helpers (Tasks 10 & 11 — implemented below)
# ---------------------------------------------------------------------------

def _build_learning_comment_md(
    new_dismissals: List[dict],
    updated_dismissals: List[dict],
    new_patterns: List[dict],
) -> str:
    """Build markdown for the learning update PR comment. Returns '' if nothing to report."""
    if not new_dismissals and not updated_dismissals and not new_patterns:
        return ""

    lines = [
        "<!-- CODEGATE-learning-update -->",
        "## 📚 Learning Update",
        "",
        "_CODEGATE detected justified dismissals and has proposed the following learning updates. "
        "Copy the JSONL blocks below into your `.cr/dismissed.jsonl` and `.cr/learned-patterns.jsonl` files._",
        "",
    ]

    escalations = [e for e in new_dismissals + updated_dismissals if e.get("scope") == "module" and e.get("dismiss_count") == 3]

    if new_dismissals:
        lines += ["### New Dismissed Patterns", ""]
        for entry in new_dismissals:
            lines += [
                f"**`{entry['file_pattern']}`** — `{entry['category']}`: {entry['title_pattern']}",
                "```jsonl",
                json.dumps(entry, ensure_ascii=False),
                "```",
                "",
            ]

    if updated_dismissals:
        lines += ["### Updated Dismissed Patterns", ""]
        for entry in updated_dismissals:
            lines += [
                f"**`{entry['file_pattern']}`** — dismiss_count → `{entry['dismiss_count']}`",
                "```jsonl",
                json.dumps(entry, ensure_ascii=False),
                "```",
                "",
            ]

    if escalations:
        lines += ["### Scope Escalations", ""]
        for entry in escalations:
            lines += [
                f"- `{entry['dismissed_id']}` widened to module scope: `{entry['file_pattern']}`",
            ]
        lines.append("")

    if new_patterns:
        lines += ["### New Learned Patterns", ""]
        for pattern in new_patterns:
            lines += [
                f"**`{pattern['pattern_id']}`** — {pattern['description']}",
                "```jsonl",
                json.dumps(pattern, ensure_ascii=False),
                "```",
                "",
            ]

    lines += [
        "---",
        "_To apply: copy each JSONL line into the corresponding file in `.cr/`. "
        "CODEGATE will read these on the next review run._",
    ]
    return "\n".join(lines)


def _post_learning_comment(
    markdown: str, pr_id: int, repo: str, vcs: str, dry_run: bool
) -> None:
    """Post the learning update comment to the PR. No-op if markdown is empty or dry_run."""
    if dry_run or not markdown:
        return
    try:
        if vcs == "ado":
            from activities.post_pr_comment_activity import PostPRCommentActivity, PostPRCommentInput
            from config import get_settings
            settings = get_settings()
            activity = PostPRCommentActivity(settings=settings)
            inp = PostPRCommentInput(
                pr_id=pr_id,
                comment_text=markdown,
                file_path=None,
                line_number=None,
                repository_id=repo or None,
            )
            activity.execute(inp)
        else:
            _gh_run_with_retry(
                ["gh", "pr", "comment", str(pr_id), "--body", markdown, "--repo", repo],
                capture_output=True, text=True, check=True
            )
    except Exception as exc:
        _eprint(f"Warning: failed to post learning comment: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    findings_path: str,
    dry_run: bool = False,
    workspace: str = ".",
    commit_id: str = "",
) -> Dict[str, Any]:
    """
    Core post_findings logic.

    Args:
        findings_path: Path to findings.json
        dry_run: If True, skip all VCS writes
        workspace: Workspace directory (for .codereview.yml lookup)
        commit_id: Source commit SHA (needed for GitHub inline comments)

    Returns:
        Structured output dict for CI gating
    """
    # 1. Load + validate
    raw = _load_json(findings_path)
    EXPECTED_SCHEMA_VERSION = "1.0"
    actual_version = raw.get("schema_version")
    if actual_version != EXPECTED_SCHEMA_VERSION:
        _eprint(f"Schema version mismatch: expected {EXPECTED_SCHEMA_VERSION}, got {actual_version}. Update your agent container.")
        raise SystemExit(1)

    errors = _validate_schema(raw)
    if errors:
        _eprint("ERROR: findings.json failed schema validation:")
        for e in errors:
            _eprint(f"  - {e}")
        raise SystemExit(1)

    findings_file = _parse_findings_file(raw)

    # 2+4. Filter + cap via pipeline (replaces separate filter_by_confidence + cap_findings calls)
    suppressed_count = len(findings_file.suppressed_findings)
    capped, drop_stats = _run_filter_pipeline(
        findings_file.findings,
        suppressed_count=suppressed_count,
        min_confidence=MIN_CONFIDENCE,
        max_total=MAX_TOTAL_FINDINGS,
        max_per_file=MAX_PER_FILE,
    )
    filtered_count = drop_stats["dropped_confidence"]
    after_confidence_count = len(findings_file.findings) - filtered_count

    # 3. Apply mode multipliers (adjusts severity for scoring)
    from pr_scorer import PRScorer
    from config import get_settings

    try:
        settings = get_settings()
        penalty_matrix = settings.get_penalty_matrix()
        star_thresholds = settings.get_star_thresholds()
    except Exception:
        # Fallback defaults if env not configured (dry-run / test scenarios)
        penalty_matrix = {
            "security": {"critical": 5.0, "warning": 4.0, "suggestion": 2.0, "good": 0.0},
            "performance": {"critical": 3.0, "warning": 2.0, "suggestion": 1.0, "good": 0.0},
            "best_practices": {"critical": 2.0, "warning": 1.0, "suggestion": 0.5, "good": 0.0},
            "code_style": {"critical": 0.0, "warning": 0.0, "suggestion": 0.0, "good": 0.0},
            "documentation": {"critical": 0.0, "warning": 0.0, "suggestion": 0.0, "good": 0.0},
        }
        star_thresholds = [0.0, 5.0, 15.0, 30.0, 50.0]
        settings = None

    scorer = PRScorer(penalty_matrix=penalty_matrix, star_thresholds=star_thresholds)

    # 5. Fetch existing cr-ids for dedup
    vcs = findings_file.vcs
    repo = findings_file.repo
    pr_id = findings_file.pr_id

    if dry_run:
        posted_cr_ids: Set[str] = set()
    elif vcs == "ado":
        posted_cr_ids = _fetch_posted_cr_ids_ado(pr_id, repo)
    else:
        posted_cr_ids = _fetch_posted_cr_ids_github(pr_id, repo)

    # Merge with journal (surviving from a prior partial-failure run)
    if not dry_run:
        posted_cr_ids |= _load_posted_journal(workspace)

    # 6. Dedup: skip already-posted cr-ids
    new_findings = [f for f in capped if f.id not in posted_cr_ids]
    deduped_count = len(capped) - len(new_findings)

    # 7. Score (exclude justified, downgrade deferred for ~50% penalty)
    justified_ids: Set[str] = {fv.cr_id for fv in findings_file.fix_verifications if fv.status == "justified"}
    deferred_ids: Set[str] = {fv.cr_id for fv in findings_file.fix_verifications if fv.status == "deferred"}

    scoring_findings = []
    for f in capped:
        if f.id in justified_ids:
            continue  # zero penalty
        if f.id in deferred_ids:
            f_downgraded = copy.copy(f)
            f_downgraded.severity = _downgrade_severity(f.severity)
            scoring_findings.append(f_downgraded)
        else:
            scoring_findings.append(f)

    all_adjusted = scorer.apply_mode_multipliers(scoring_findings, findings_file.review_modes)
    score = scorer.calculate_pr_score(all_adjusted)

    # 7b. Score comparison (load old before persisting new)
    old_score = _load_prior_score(workspace)
    comparison_md = _generate_comparison_md(score, findings_file.fix_verifications, pr_id, old_score) if findings_file.fix_verifications else ""
    _write_prior_score(workspace, score)

    # 7c. Evaluate CI gate
    gate_config = _load_codereview_yml(workspace)
    gate_result = _evaluate_gate(score, all_adjusted, gate_config, findings_file.suppressed_findings)

    # 8. Post inline comments
    posted_count = 0
    post_errors = []
    for finding in new_findings:
        if vcs == "ado":
            ok = _post_inline_ado(finding, pr_id, repo, dry_run)
        else:
            ok = _post_inline_github(finding, pr_id, repo, commit_id, dry_run)

        if ok:
            posted_count += 1
            if not dry_run:
                _append_posted_journal(workspace, finding.id)
        else:
            post_errors.append(finding.id)

    # Clean up journal when all posts succeeded (no errors)
    if not dry_run and not post_errors:
        _cleanup_posted_journal(workspace)

    # 9. Handle fix verifications
    if findings_file.fix_verifications:
        if vcs == "ado":
            _handle_fix_verifications_ado(findings_file.fix_verifications, pr_id, repo, dry_run, gate_config)
        else:
            _handle_fix_verifications_github(findings_file.fix_verifications, pr_id, repo, dry_run)

    # 9b. Learning write-back: detect dismissals + generate patterns
    learning_updates: Dict[str, Any] = {}
    _new_dismissals_list: List[dict] = []
    _updated_dismissals_list: List[dict] = []
    _all_dismissed: List[dict] = []
    _new_patterns: List[dict] = []

    _codereview_cfg = _load_codereview_yml(workspace)
    learning_delivery = _codereview_cfg.get("learning_delivery", "comment")

    if findings_file.fix_verifications:
        detected = _detect_dismissals(findings_file)
        if detected:
            dismissed_path = Path(workspace) / ".cr" / "dismissed.jsonl"
            existing_dismissed = _read_jsonl(dismissed_path)
            existing_ids_set = {e["dismissed_id"] for e in existing_dismissed}
            _all_dismissed, changed = _merge_dismissals(existing_dismissed, detected)
            _new_dismissals_list = [e for e in changed if e["dismissed_id"] not in existing_ids_set]
            _updated_dismissals_list = [e for e in changed if e["dismissed_id"] in existing_ids_set]
            _scope_escalations = sum(1 for e in changed if e.get("scope") == "module" and e.get("dismiss_count") == 3)

    # Compute advisory cost
    cost_estimate = _compute_cost_estimate(findings_file.token_usage, findings_file.agent)

    # 12. Post/update summary
    summary_md = _build_summary_markdown(
        findings_file=findings_file,
        filtered_findings=all_adjusted,
        score=score,
        gate_result=gate_result,
        fix_verifications=findings_file.fix_verifications,
        comparison_md=comparison_md,
        cost_estimate=cost_estimate,
        drop_stats=drop_stats,
        rules_checked=findings_file.rules_checked,
    )

    if not dry_run and settings:
        try:
            if vcs == "ado":
                from activities.update_summary_activity import UpdateSummaryActivity, UpdateSummaryInput
                summary_activity = UpdateSummaryActivity(settings=settings)
                summary_activity.execute(UpdateSummaryInput(
                    pr_id=pr_id,
                    new_content=summary_md,
                    repository_id=repo or None,
                ))
            else:
                _post_or_update_summary_github(summary_md, pr_id, repo, dry_run=False)
        except Exception as exc:
            _eprint(f"Warning: failed to post summary: {exc}")

    # Build output
    output = {
        "pr_id": pr_id,
        "repo": repo,
        "vcs": vcs,
        "review_modes": findings_file.review_modes,
        "agent": findings_file.agent,
        "tool_calls": findings_file.tool_calls,
        "filtering": {
            "total_raw": len(findings_file.findings),
            "after_confidence_filter": after_confidence_count,
            "filtered_low_confidence": filtered_count,
            "after_cap": len(capped),
            "dropped_per_file_cap": drop_stats["dropped_per_file_cap"],
            "dropped_total_cap": drop_stats["dropped_total_cap"],
            "deduped_already_posted": deduped_count,
            "new_findings_posted": posted_count,
            "post_errors": post_errors,
            "suppressed_count": suppressed_count,
        },
        "score": {
            "total_penalty": score.total_penalty,
            "overall_stars": score.overall_stars,
            "quality_level": score.quality_level,
            "issues_by_severity": score.issues_by_severity,
            "category_penalties": score.category_penalties,
        },
        "gate": gate_result,
        "dry_run": dry_run,
        "findings": [
            {
                "id": f.id,
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "confidence": f.confidence,
            }
            for f in capped
        ],
        "fix_verifications": [
            {
                "cr_id": fv.cr_id,
                "status": fv.status,
                "reason": fv.reason,
                "counter_reason": fv.counter_reason,
                "developer_reply": fv.developer_reply,
            }
            for fv in findings_file.fix_verifications
        ],
        "rules_checked": [rc.to_dict() for rc in findings_file.rules_checked],
        "has_comparison": bool(comparison_md),
        "cost_estimate": cost_estimate,
        "token_usage": (
            {
                "input_tokens": findings_file.token_usage.input_tokens,
                "output_tokens": findings_file.token_usage.output_tokens,
            }
            if findings_file.token_usage
            else None
        ),
    }

    if dry_run:
        output["summary_md"] = summary_md

    return output


def _redirect_logging_to_stderr():
    """
    Redirect all logging output to stderr so stdout stays clean JSON.

    Must be called before any logging setup occurs. Installs a root handler
    on stderr, then patches utils.logger.setup_logger to also use stderr.
    """
    import logging

    # Install a root-level stderr handler that will catch any logger that
    # propagates to root (most do by default).
    root = logging.getLogger()
    if not any(
        hasattr(h, "stream") and h.stream is sys.stderr
        for h in root.handlers
    ):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.DEBUG)
        root.addHandler(stderr_handler)

    # Monkey-patch utils.logger so future loggers also write to stderr
    try:
        import utils.logger as _ul
        import functools

        _orig_setup = _ul.setup_logger

        @functools.wraps(_orig_setup)
        def _patched_setup(name="CODEGATE", level="INFO", log_file=None, log_format="json", force=False):
            logger = _orig_setup(name=name, level=level, log_file=log_file, log_format=log_format, force=force)
            for handler in logger.handlers:
                if hasattr(handler, "stream") and handler.stream is sys.stdout:
                    handler.stream = sys.stderr
            return logger

        _ul.setup_logger = _patched_setup
    except ImportError:
        pass


def main():
    _redirect_logging_to_stderr()
    parser = argparse.ArgumentParser(
        prog="post_findings.py",
        description="CODEGATE Phase 2 engine — post findings from findings.json to VCS"
    )
    parser.add_argument(
        "--findings",
        required=True,
        help="Path to findings.json produced by the review agent"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Read, filter, score — but skip all VCS writes. Outputs scored JSON to stdout."
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root directory (for .codereview.yml lookup)"
    )
    parser.add_argument(
        "--commit-id",
        default="",
        help="Source commit SHA (required for GitHub inline comments)"
    )

    args = parser.parse_args()

    try:
        output = run(
            findings_path=args.findings,
            dry_run=args.dry_run,
            workspace=args.workspace,
            commit_id=args.commit_id,
        )
        print(json.dumps(output, indent=2))
        if not output["gate"]["passed"]:
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        _eprint(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
