"""
Tests for tools/extract_signatures.py — Phase 20 signature map preprocessor.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure tools/ is importable
TOOLS_DIR = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from extract_signatures import extract_signatures, _body_hash, _normalize_body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_py(tmp_path, name, source):
    f = tmp_path / name
    f.write_text(source, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_simple_function_extraction(tmp_path):
    _write_py(tmp_path, "mod.py", "def hello(x: int) -> str:\n    return str(x)\n")
    results = extract_signatures(str(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r["name"] == "hello"
    assert r["line"] == 1
    assert any(p["name"] == "x" for p in r["params"])
    assert len(r["body_hash"]) == 8


def test_body_hash_ignores_variable_names(tmp_path):
    src_a = "def f(x):\n    result = x + 1\n    return result\n"
    src_b = "def f(x):\n    total = x + 1\n    return total\n"
    _write_py(tmp_path, "a.py", src_a)
    _write_py(tmp_path, "b.py", src_b)
    results = extract_signatures(str(tmp_path))
    hashes = {r["name"] + r["file"]: r["body_hash"] for r in results}
    hash_a = next(r["body_hash"] for r in results if "a.py" in r["file"])
    hash_b = next(r["body_hash"] for r in results if "b.py" in r["file"])
    assert hash_a == hash_b


def test_body_hash_ignores_whitespace_and_comments(tmp_path):
    # Inline comments are stripped during parsing, so same AST structure → same hash
    src_a = "def f():\n    x = 1\n    return x\n"
    src_b = "def f():\n    x = 1\n    return x  # trailing comment\n"
    _write_py(tmp_path, "a.py", src_a)
    _write_py(tmp_path, "b.py", src_b)
    results = extract_signatures(str(tmp_path))
    hash_a = next(r["body_hash"] for r in results if "a.py" in r["file"])
    hash_b = next(r["body_hash"] for r in results if "b.py" in r["file"])
    assert hash_a == hash_b


def test_body_hash_differs_for_different_logic(tmp_path):
    src_a = "def f(x):\n    return x + 1\n"
    src_b = "def f(x):\n    return x * 2\n"
    _write_py(tmp_path, "a.py", src_a)
    _write_py(tmp_path, "b.py", src_b)
    results = extract_signatures(str(tmp_path))
    hash_a = next(r["body_hash"] for r in results if "a.py" in r["file"])
    hash_b = next(r["body_hash"] for r in results if "b.py" in r["file"])
    assert hash_a != hash_b


def test_class_methods_extracted(tmp_path):
    src = (
        "class MyClass:\n"
        "    def method_one(self, a):\n"
        "        return a\n"
        "    def method_two(self):\n"
        "        pass\n"
    )
    _write_py(tmp_path, "cls.py", src)
    results = extract_signatures(str(tmp_path))
    names = {r["name"] for r in results}
    assert "method_one" in names
    assert "method_two" in names


def test_invalid_file_skipped(tmp_path, capsys):
    _write_py(tmp_path, "bad.py", "def f(\n    broken syntax here !!!\n")
    _write_py(tmp_path, "good.py", "def g():\n    pass\n")
    results = extract_signatures(str(tmp_path))
    names = {r["name"] for r in results}
    assert "g" in names
    assert "f" not in names
    captured = capsys.readouterr()
    assert "bad.py" in captured.err


def test_non_python_files_ignored(tmp_path):
    _write_py(tmp_path, "script.py", "def py_func():\n    pass\n")
    (tmp_path / "app.js").write_text("function jsFunc() {}", encoding="utf-8")
    (tmp_path / "README.md").write_text("# docs", encoding="utf-8")
    results = extract_signatures(str(tmp_path))
    names = {r["name"] for r in results}
    assert "py_func" in names
    assert len(results) == 1


def test_output_is_valid_json(tmp_path):
    _write_py(tmp_path, "mod.py", "def foo(x):\n    return x\n")
    script = Path(__file__).parent.parent / "tools" / "extract_signatures.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "foo"


def test_relative_paths(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write_py(sub, "mod.py", "def bar():\n    pass\n")
    results = extract_signatures(str(sub))
    # path should be relative to parent of scanned root (tmp_path)
    assert any("sub/mod.py" in r["file"] or "sub\\mod.py" in r["file"] for r in results)
    # must NOT be an absolute path
    for r in results:
        assert not Path(r["file"]).is_absolute()
