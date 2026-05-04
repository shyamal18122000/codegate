"""
cli.py — CODEGATE `cr` CLI entry point.

Usage:
    python src/cli.py learn --list
    python src/cli.py learn --add --pattern "..." --category security --confidence-modifier -0.3
    python src/cli.py learn --remove <id>
    python src/cli.py learn --analyze
    python src/cli.py learn --stats
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# JSONL I/O helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a JSONL file; return list of dicts. Returns [] if file missing."""
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> None:
    """Overwrite a JSONL file with the given entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _append_jsonl(path: Path, entry: Dict[str, Any]) -> None:
    """Append a single entry to a JSONL file, creating it if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

def _dismissed_path(workspace: str) -> Path:
    return Path(workspace) / ".codereview" / "dismissed.jsonl"


def _learned_path(workspace: str) -> Path:
    return Path(workspace) / ".codereview" / "learned-patterns.jsonl"


# ---------------------------------------------------------------------------
# Subcommand stubs — filled in by Tasks 21.3b and 21.3c
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    dismissed = _read_jsonl(_dismissed_path(args.workspace))
    learned = _read_jsonl(_learned_path(args.workspace))
    if not dismissed and not learned:
        print("No dismissed/learned patterns found.")
        return
    _print_list(dismissed, learned)


def _print_list(dismissed: list, learned: list) -> None:
    if dismissed:
        print(f"=== Dismissed findings ({len(dismissed)}) ===")
        for entry in dismissed:
            print(
                f"  [{entry.get('id', '?')}] {entry.get('category', '?')} / "
                f"{entry.get('title', '?')} — {entry.get('file', '?')}:{entry.get('line', '?')}"
            )
    if learned:
        print(f"=== Learned patterns ({len(learned)}) ===")
        for entry in learned:
            print(
                f"  [{entry.get('pattern_id', '?')}] {entry.get('category', '?')} "
                f"conf_mod={entry.get('confidence_modifier', '?')} — {entry.get('pattern', '?')}"
            )


def cmd_add(args: argparse.Namespace) -> None:
    import hashlib
    from datetime import datetime, timezone

    if not args.pattern or not args.pattern.strip():
        print("Error: --pattern must not be empty.", file=sys.stderr)
        sys.exit(1)

    if not args.category or not args.category.strip():
        print("Error: --category must not be empty.", file=sys.stderr)
        sys.exit(1)

    if not (-1.0 <= args.confidence_modifier <= 0.0):
        print(
            f"Error: --confidence-modifier must be in range [-1.0, 0.0], "
            f"got {args.confidence_modifier}",
            file=sys.stderr,
        )
        sys.exit(1)

    pattern_id = "lp-" + hashlib.sha1(args.pattern.encode()).hexdigest()[:8]
    entry = {
        "pattern_id": pattern_id,
        "pattern": args.pattern,
        "category": args.category,
        "confidence_modifier": args.confidence_modifier,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_jsonl(_learned_path(args.workspace), entry)
    print(f"Added pattern {pattern_id}: {args.pattern}")


def cmd_remove(args: argparse.Namespace) -> None:
    id_to_remove = args.id
    if id_to_remove.startswith("d-"):
        path = _dismissed_path(args.workspace)
        entries = _read_jsonl(path)
        filtered = [e for e in entries if e.get("id") != id_to_remove]
    elif id_to_remove.startswith("lp-"):
        path = _learned_path(args.workspace)
        entries = _read_jsonl(path)
        filtered = [e for e in entries if e.get("pattern_id") != id_to_remove]
    else:
        print(f"Error: ID must start with 'd-' or 'lp-', got: {id_to_remove}", file=sys.stderr)
        sys.exit(1)

    if len(filtered) == len(entries):
        print(f"Error: ID '{id_to_remove}' not found.", file=sys.stderr)
        sys.exit(1)

    _write_jsonl(path, filtered)
    print(f"Removed {id_to_remove}.")


def cmd_analyze(args: argparse.Namespace) -> None:
    from collections import defaultdict

    dismissed = _read_jsonl(_dismissed_path(args.workspace))
    learned = _read_jsonl(_learned_path(args.workspace))
    existing_patterns = {e.get("pattern") for e in learned}

    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for entry in dismissed:
        key = (entry.get("category", ""), entry.get("title", ""))
        groups[key].append(entry)

    suggestions = []
    for (category, title), items in groups.items():
        if len(items) >= 3 and title not in existing_patterns:
            suggestions.append((category, title, len(items)))

    if not suggestions:
        print("No new pattern suggestions (need 3+ dismissals of the same category/title).")
        return

    print(f"=== Pattern suggestions ({len(suggestions)}) ===")
    for category, title, count in suggestions:
        print(
            f"  [{count}x] {category} / {title}\n"
            f"    → python src/cli.py learn --add --pattern \"{title}\" "
            f"--category {category} --confidence-modifier -0.3"
        )


def cmd_stats(args: argparse.Namespace) -> None:
    from collections import defaultdict

    dismissed = _read_jsonl(_dismissed_path(args.workspace))
    learned = _read_jsonl(_learned_path(args.workspace))

    by_module: Dict[str, int] = defaultdict(int)
    by_category: Dict[str, int] = defaultdict(int)
    for entry in dismissed:
        file_path = entry.get("file", "unknown")
        module = file_path.split("/")[0] if "/" in file_path else file_path
        by_module[module] += 1
        by_category[entry.get("category", "unknown")] += 1

    print("=== Dismissal stats ===")
    print(f"Total dismissed: {len(dismissed)}")
    print(f"Total learned patterns: {len(learned)}")
    if by_category:
        print("\nBy category:")
        for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {count}")
    if by_module:
        print("\nBy module (top-level directory):")
        for mod, count in sorted(by_module.items(), key=lambda x: -x[1]):
            print(f"  {mod}: {count}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cr",
        description="CODEGATE CLI — manage learned/dismissed review patterns",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root directory (default: current directory)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- learn subcommand ---
    learn = subparsers.add_parser("learn", help="Manage dismissed/learned patterns")
    group = learn.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List all dismissed/learned patterns")
    group.add_argument("--add", action="store_true", help="Add a new learned pattern")
    group.add_argument("--remove", dest="remove_id", metavar="ID", help="Remove entry by ID (d-xxx or lp-xxx)")
    group.add_argument("--analyze", action="store_true", help="Suggest patterns from dismissal history")
    group.add_argument("--stats", action="store_true", help="Show dismissal statistics")

    learn.add_argument("--pattern", default="", help="Pattern text (required with --add)")
    learn.add_argument("--category", default="best_practices", help="Finding category (required with --add)")
    learn.add_argument(
        "--confidence-modifier",
        type=float,
        default=-0.3,
        dest="confidence_modifier",
        help="Confidence adjustment [-1.0, 0.0] (default: -0.3)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "learn":
        if args.list:
            cmd_list(args)
        elif args.add:
            cmd_add(args)
        elif args.remove_id is not None:
            args.id = args.remove_id
            cmd_remove(args)
        elif args.analyze:
            cmd_analyze(args)
        elif args.stats:
            cmd_stats(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
