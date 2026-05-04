"""
extract_signatures.py — Preprocessor for CODEGATE duplicate detection.

Walks a directory for *.py files, extracts function/method signatures and
body hashes, and writes a JSON array to stdout. Runs before the review agent
so it doesn't consume any of the agent's 40 tool-call budget.

Usage:
    python3 tools/extract_signatures.py /path/to/src > .cr/signature_map.json
"""

import ast
import hashlib
import json
import os
import sys
from pathlib import Path


def _normalize_body(body_stmts):
    """
    Return a stable string representation of a function body.

    Normalisations applied:
    - Replace all Name.id with "_" (variable-name-agnostic)
    - Replace all string literal values with "" (constant-agnostic)
    """

    class _Normaliser(ast.NodeTransformer):
        def visit_Name(self, node):
            node.id = "_"
            return node

        def visit_Constant(self, node):
            if isinstance(node.value, str):
                node.value = ""
            return node

        # Python ≤ 3.7 compat — ast.Str / ast.Num still present
        def visit_Str(self, node):
            node.s = ""
            return node

    module = ast.Module(body=list(body_stmts), type_ignores=[])
    normalised = _Normaliser().visit(module)
    return ast.dump(normalised)


def _body_hash(body_stmts) -> str:
    """Return SHA-1[:8] of the normalised body."""
    text = _normalize_body(body_stmts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _param_list(args: ast.arguments) -> list:
    """Return list of parameter dicts with name and optional annotation."""
    params = []
    all_args = args.posonlyargs + args.args + args.kwonlyargs
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)
    for arg in all_args:
        entry = {"name": arg.arg}
        if arg.annotation is not None:
            try:
                entry["annotation"] = ast.unparse(arg.annotation)
            except AttributeError:
                # ast.unparse not available in Python < 3.9
                entry["annotation"] = ast.dump(arg.annotation)
        params.append(entry)
    return params


def _extract_from_tree(tree: ast.AST, rel_path: str) -> list:
    """Walk AST and collect all function/method definitions."""
    entries = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries.append({
                "file": rel_path,
                "name": node.name,
                "line": node.lineno,
                "params": _param_list(node.args),
                "body_hash": _body_hash(node.body),
            })
    return entries


def extract_signatures(root: str) -> list:
    """
    Walk root for *.py files and return list of signature dicts.

    File paths in entries are relative to the *parent* of root.
    Syntax errors in individual files are logged to stderr and skipped.
    """
    root_path = Path(root).resolve()
    base = root_path.parent
    results = []

    for dirpath, _dirnames, filenames in os.walk(root_path):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            abs_path = Path(dirpath) / fname
            rel_path = str(abs_path.relative_to(base)).replace("\\", "/")
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(abs_path))
                results.extend(_extract_from_tree(tree, rel_path))
            except SyntaxError as exc:
                print(f"Warning: skipping {rel_path}: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: error processing {rel_path}: {exc}", file=sys.stderr)

    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: extract_signatures.py <root_dir>", file=sys.stderr)
        sys.exit(1)

    root = sys.argv[1]
    signatures = extract_signatures(root)
    print(json.dumps(signatures, indent=2))


if __name__ == "__main__":
    main()
