"""
Unit tests for src/cli.py — cr learn CLI.

Covers: list, add, remove, analyze, stats subcommands.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/ is on path (conftest.py already does this, but be explicit)
_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dismissed(tmp_path, entries):
    path = cli._dismissed_path(str(tmp_path))
    cli._write_jsonl(path, entries)


def _make_learned(tmp_path, entries):
    path = cli._learned_path(str(tmp_path))
    cli._write_jsonl(path, entries)


def _args(**kwargs):
    """Build a minimal Namespace with workspace defaulting to '.'."""
    import argparse
    kwargs.setdefault("workspace", ".")
    ns = argparse.Namespace(**kwargs)
    return ns


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCrLearnCLI:
    def test_list_empty(self, tmp_path, capsys):
        """No files → prints 'No dismissed/learned patterns'."""
        args = _args(workspace=str(tmp_path))
        cli.cmd_list(args)
        out = capsys.readouterr().out
        assert "No dismissed/learned patterns" in out

    def test_list_with_entries(self, tmp_path, capsys):
        """Both files populated → all entries shown."""
        _make_dismissed(tmp_path, [
            {"id": "d-001", "category": "security", "title": "SQL injection",
             "file": "app.py", "line": 10},
        ])
        _make_learned(tmp_path, [
            {"pattern_id": "lp-abcd1234", "category": "security",
             "pattern": "SQL injection", "confidence_modifier": -0.3},
        ])
        args = _args(workspace=str(tmp_path))
        cli.cmd_list(args)
        out = capsys.readouterr().out
        assert "d-001" in out
        assert "lp-abcd1234" in out

    def test_add_creates_pattern(self, tmp_path):
        """cmd_add writes an entry to learned-patterns.jsonl."""
        args = _args(
            workspace=str(tmp_path),
            pattern="SQL injection",
            category="security",
            confidence_modifier=-0.3,
        )
        cli.cmd_add(args)
        entries = cli._read_jsonl(cli._learned_path(str(tmp_path)))
        assert len(entries) == 1
        assert entries[0]["pattern"] == "SQL injection"
        assert entries[0]["category"] == "security"
        assert entries[0]["confidence_modifier"] == -0.3
        assert entries[0]["pattern_id"].startswith("lp-")

    def test_add_validates_modifier_range(self, tmp_path, capsys):
        """Confidence modifier outside [-1.0, 0.0] → exits with error."""
        args = _args(
            workspace=str(tmp_path),
            pattern="test",
            category="security",
            confidence_modifier=0.5,  # invalid: positive
        )
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_add(args)
        assert exc_info.value.code == 1

    def test_remove_dismissed(self, tmp_path, capsys):
        """cmd_remove with d-xxx removes the entry from dismissed.jsonl."""
        _make_dismissed(tmp_path, [
            {"id": "d-001", "category": "security", "title": "t", "file": "f.py", "line": 1},
            {"id": "d-002", "category": "performance", "title": "t2", "file": "f.py", "line": 2},
        ])
        args = _args(workspace=str(tmp_path), id="d-001")
        cli.cmd_remove(args)
        entries = cli._read_jsonl(cli._dismissed_path(str(tmp_path)))
        ids = [e["id"] for e in entries]
        assert "d-001" not in ids
        assert "d-002" in ids

    def test_remove_learned(self, tmp_path, capsys):
        """cmd_remove with lp-xxx removes the entry from learned-patterns.jsonl."""
        _make_learned(tmp_path, [
            {"pattern_id": "lp-aabbccdd", "pattern": "p1", "category": "security",
             "confidence_modifier": -0.3},
            {"pattern_id": "lp-11223344", "pattern": "p2", "category": "performance",
             "confidence_modifier": -0.2},
        ])
        args = _args(workspace=str(tmp_path), id="lp-aabbccdd")
        cli.cmd_remove(args)
        entries = cli._read_jsonl(cli._learned_path(str(tmp_path)))
        pids = [e["pattern_id"] for e in entries]
        assert "lp-aabbccdd" not in pids
        assert "lp-11223344" in pids

    def test_remove_nonexistent(self, tmp_path):
        """Removing a non-existent ID prints an error and exits."""
        _make_dismissed(tmp_path, [
            {"id": "d-001", "category": "security", "title": "t", "file": "f.py", "line": 1},
        ])
        args = _args(workspace=str(tmp_path), id="d-999")
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_remove(args)
        assert exc_info.value.code == 1

    def test_analyze_suggests(self, tmp_path, capsys):
        """3+ dismissals of same category/title → suggestion printed."""
        entries = [
            {"id": f"d-{i:03d}", "category": "security", "title": "SQL injection",
             "file": "app.py", "line": i}
            for i in range(3)
        ]
        _make_dismissed(tmp_path, entries)
        args = _args(workspace=str(tmp_path))
        cli.cmd_analyze(args)
        out = capsys.readouterr().out
        assert "SQL injection" in out
        assert "--add" in out

    def test_analyze_skips_existing(self, tmp_path, capsys):
        """Existing pattern with same title → not suggested again."""
        entries = [
            {"id": f"d-{i:03d}", "category": "security", "title": "SQL injection",
             "file": "app.py", "line": i}
            for i in range(3)
        ]
        _make_dismissed(tmp_path, entries)
        _make_learned(tmp_path, [
            {"pattern_id": "lp-existing", "pattern": "SQL injection",
             "category": "security", "confidence_modifier": -0.3},
        ])
        args = _args(workspace=str(tmp_path))
        cli.cmd_analyze(args)
        out = capsys.readouterr().out
        # Suggestion for SQL injection should NOT appear (already learned)
        assert "SQL injection" not in out or "No new pattern" in out

    def test_stats_format(self, tmp_path, capsys):
        """cmd_stats output contains expected section headers."""
        _make_dismissed(tmp_path, [
            {"id": "d-001", "category": "security", "title": "t", "file": "src/app.py", "line": 1},
            {"id": "d-002", "category": "performance", "title": "t2", "file": "src/db.py", "line": 2},
        ])
        args = _args(workspace=str(tmp_path))
        cli.cmd_stats(args)
        out = capsys.readouterr().out
        assert "Dismissal stats" in out
        assert "category" in out.lower()
        assert "security" in out
