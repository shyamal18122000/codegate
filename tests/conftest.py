"""
Shared pytest fixtures for Phase 2 unit tests.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure src/ is on the path
_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


# ---------------------------------------------------------------------------
# Raw findings data
# ---------------------------------------------------------------------------

SAMPLE_FINDINGS_WITH_RULES = {
    "schema_version": "1.0",
    "pr_id": 99,
    "repo": "MyOrg/RulesRepo",
    "vcs": "github",
    "review_modes": ["standard"],
    "tool_calls": 5,
    "agent": "claude",
    "findings": [
        {
            "id": "cr-001",
            "file": "src/auth/login.py",
            "line": 12,
            "severity": "critical",
            "category": "security",
            "title": "PRJ-001: No eval() usage",
            "message": "eval() found in file",
            "confidence": 1.0,
            "suggestion": None,
        }
    ],
    "fix_verifications": [],
    "rules_checked": [
        {"id": "PRJ-001", "applied_to": 3, "findings_generated": 1},
        {"id": "PRJ-002", "applied_to": 3, "findings_generated": 0},
    ],
}


SAMPLE_FINDINGS_RAW = {
    "schema_version": "1.0",
    "pr_id": 42,
    "repo": "MyOrg/MyRepo",
    "vcs": "ado",
    "review_modes": ["standard", "security"],
    "tool_calls": 12,
    "agent": "codex",
    "findings": [
        {
            "id": "cr-001",
            "file": "src/auth/login.py",
            "line": 45,
            "severity": "critical",
            "category": "security",
            "title": "SQL injection",
            "message": "Unsanitised input in SQL query",
            "confidence": 0.95,
            "suggestion": "Use parameterised queries",
        },
        {
            "id": "cr-002",
            "file": "src/auth/login.py",
            "line": 78,
            "severity": "warning",
            "category": "security",
            "title": "Hardcoded secret",
            "message": "SECRET_KEY is hardcoded",
            "confidence": 0.90,
            "suggestion": None,
        },
        {
            "id": "cr-003",
            "file": "src/api/users.py",
            "line": 120,
            "severity": "warning",
            "category": "performance",
            "title": "N+1 query",
            "message": "Separate DB query per user",
            "confidence": 0.85,
            "suggestion": None,
        },
        {
            "id": "cr-004",
            "file": "src/api/users.py",
            "line": 55,
            "severity": "suggestion",
            "category": "best_practices",
            "title": "Missing type annotation",
            "message": "Add return type hint",
            "confidence": 0.80,
            "suggestion": None,
        },
        {
            "id": "cr-005",
            "file": "src/utils/helpers.py",
            "line": 10,
            "severity": "suggestion",
            "category": "code_style",
            "title": "Unused import",
            "message": "os is imported but never used",
            "confidence": 0.99,
            "suggestion": None,
        },
        {
            "id": "cr-006",
            "file": "src/auth/login.py",
            "line": 15,
            "severity": "suggestion",
            "category": "best_practices",
            "title": "Low confidence — should be filtered",
            "message": "Confidence < 0.7; will be dropped",
            "confidence": 0.60,
            "suggestion": None,
        },
    ],
    "fix_verifications": [],
}


@pytest.fixture
def sample_raw():
    """Return a deep copy of the raw findings dict."""
    import copy
    return copy.deepcopy(SAMPLE_FINDINGS_RAW)


@pytest.fixture
def sample_findings_file():
    """Return a parsed FindingsFile from sample data."""
    from models.review_models import Finding, FindingsFile

    raw = SAMPLE_FINDINGS_RAW
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
        for f in raw["findings"]
    ]
    return FindingsFile(
        pr_id=raw["pr_id"],
        repo=raw["repo"],
        vcs=raw["vcs"],
        review_modes=raw["review_modes"],
        findings=findings,
        fix_verifications=[],
        tool_calls=raw["tool_calls"],
        agent=raw["agent"],
    )


SAMPLE_SIGNATURE_MAP = [
    {
        "file": "src/utils/helpers.py",
        "name": "process_data",
        "line": 10,
        "params": [{"name": "data"}],
        "body_hash": "abc12345",
    },
    {
        "file": "src/api/views.py",
        "name": "process_data",
        "line": 55,
        "params": [{"name": "data"}],
        "body_hash": "abc12345",
    },
    {
        "file": "src/services/worker.py",
        "name": "unique_function",
        "line": 20,
        "params": [],
        "body_hash": "deadbeef",
    },
]

SAMPLE_FINDINGS_WITH_DUPLICATES = {
    "schema_version": "1.0",
    "pr_id": 99,
    "repo": "MyOrg/MyRepo",
    "vcs": "github",
    "review_modes": ["standard"],
    "tool_calls": 5,
    "agent": "claude",
    "findings": [
        {
            "id": "cr-001",
            "file": "src/utils/helpers.py",
            "line": 10,
            "severity": "warning",
            "category": "best_practices",
            "title": "Duplicated function: process_data",
            "message": "Function 'process_data' (body_hash: abc12345) also exists in src/api/views.py:55. Consider extracting to a shared utility.",
            "confidence": 0.8,
            "suggestion": "Extract to a shared module and import from both call sites.",
        }
    ],
    "fix_verifications": [],
}


@pytest.fixture
def sample_signature_map():
    """Return the sample signature map list."""
    import copy
    return copy.deepcopy(SAMPLE_SIGNATURE_MAP)


@pytest.fixture
def sample_findings_with_duplicates():
    """Return findings dict containing a duplicate-detection finding."""
    import copy
    return copy.deepcopy(SAMPLE_FINDINGS_WITH_DUPLICATES)


@pytest.fixture
def default_penalty_matrix():
    return {
        "security": {"critical": 5.0, "warning": 4.0, "suggestion": 2.0, "good": 0.0},
        "performance": {"critical": 3.0, "warning": 2.0, "suggestion": 1.0, "good": 0.0},
        "best_practices": {"critical": 2.0, "warning": 1.0, "suggestion": 0.5, "good": 0.0},
        "code_style": {"critical": 0.0, "warning": 0.0, "suggestion": 0.0, "good": 0.0},
        "documentation": {"critical": 0.0, "warning": 0.0, "suggestion": 0.0, "good": 0.0},
    }


@pytest.fixture
def default_star_thresholds():
    return [0.0, 5.0, 15.0, 30.0, 50.0]


@pytest.fixture
def pr_scorer(default_penalty_matrix, default_star_thresholds):
    from pr_scorer import PRScorer
    return PRScorer(
        penalty_matrix=default_penalty_matrix,
        star_thresholds=default_star_thresholds,
    )


@pytest.fixture
def sample_findings_path(tmp_path):
    """Write sample_findings.json to a temp file and return its path."""
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(SAMPLE_FINDINGS_RAW), encoding="utf-8")
    return str(path)


@pytest.fixture
def sample_findings_with_rules():
    """Return a deep copy of the raw findings dict that includes rules_checked."""
    import copy
    return copy.deepcopy(SAMPLE_FINDINGS_WITH_RULES)


@pytest.fixture
def mock_ado_activities(mocker):
    """
    Patch all ADO activities so they don't make real network calls.

    Returns a namespace with the mocked activity class instances.
    """
    import types
    ns = types.SimpleNamespace()

    ns.fetch_comments = mocker.patch(
        "activities.fetch_pr_comments_activity.FetchPRCommentsActivity.execute",
        return_value=[],
    )
    ns.post_comment = mocker.patch(
        "activities.post_pr_comment_activity.PostPRCommentActivity.execute",
        return_value=None,
    )
    ns.post_fix_reply = mocker.patch(
        "activities.post_fix_reply_activity.PostFixReplyActivity.execute",
        return_value=True,
    )
    ns.update_summary = mocker.patch(
        "activities.update_summary_activity.UpdateSummaryActivity.execute",
        return_value=None,
    )
    return ns
