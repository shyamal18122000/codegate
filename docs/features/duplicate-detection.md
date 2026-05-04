# Feature: Duplicate Detection

## Overview

Duplicate detection identifies copy-paste code in the PR before the LLM agent runs. It uses AST-based signature extraction to find functions with identical or near-identical bodies across different files. This runs as a preprocessor in the Docker entrypoint, producing a signature map that the agent consumes.

## Architecture

```
entrypoint.sh
  |
  v
tools/extract_signatures.py    -->  /workspace/.cr/signature_map.json
  |
  v
Agent Step 5b-duplication      -->  Findings for duplicated code
```

## Signature Extraction (`tools/extract_signatures.py`)

The extractor uses Python's `ast` module (and equivalent parsers for other languages) to:

1. Parse each changed file into an AST
2. Extract function and method definitions
3. For each function, compute a `body_hash`:
   - Strip whitespace normalization
   - Normalize variable names (replace local variable names with positional placeholders)
   - Hash the normalized body with SHA-1 (first 8 hex chars)
4. Record the function signature: name, file, line, parameters, `body_hash`
5. Write all signatures to `/workspace/.cr/signature_map.json` (via stdout redirection)

### Signature Map Format

The output is a flat JSON array of signature objects:

```json
[
  {
    "file": "src/auth/login.py",
    "name": "validate_input",
    "line": 42,
    "params": [{"name": "username"}, {"name": "password"}],
    "body_hash": "a1b2c3d4"
  },
  {
    "file": "src/api/register.py",
    "name": "validate_user_input",
    "line": 15,
    "params": [{"name": "user"}, {"name": "pass"}],
    "body_hash": "a1b2c3d4"
  }
]
```

Functions with the same `body_hash` are duplicates -- candidates for consolidation.

### Body Hash Normalization

The normalization process ensures that trivially different copies are detected:

| Difference | Normalized? | Example |
|-----------|-------------|---------|
| Different variable names | Yes | `x = 1` vs `y = 1` produce same hash |
| Different whitespace | Yes | Extra blank lines stripped |
| Different comments | Yes | Comments stripped before hashing |
| Different parameter names | Yes | Parameters replaced with positional placeholders |
| Different function names | Yes | Function name excluded from hash |
| Different logic | No | Different control flow produces different hash |
| Different string literals | Yes | All string constants replaced with `""` before hashing |

## Agent Step 5b-duplication

After the agent reads a file (Step 5a), it checks for duplicates:

1. If `/workspace/.cr/signature_map.json` exists, load it
2. For the current file, look up any functions whose `body_hash` matches another entry in the signature map
3. If duplicates are found across files, create a finding:
   - Severity: `suggestion` (or `warning` if >2 copies)
   - Category: `best_practices`
   - Message identifies all locations of the duplicate
   - Suggestion recommends extracting to a shared utility

### Fallback: `rg`-based Search

If the signature map is unavailable (e.g., the file language is not supported by the AST extractor), the agent falls back to `rg` (ripgrep) to search for similar function names or code blocks across the workspace. This is less precise than AST-based matching but still catches obvious copies.

```bash
# Agent fallback: search for similar function bodies
rg "def validate_input" /workspace/src --type py -l
```

## Docker Entrypoint Integration

The signature extraction runs in `entrypoint.sh` before Phase 1:

```bash
# Step 0: Extract function signatures for duplicate detection
python3 /app/tools/extract_signatures.py /workspace/src > /workspace/.cr/signature_map.json
```

The `tools/` directory is copied into the Docker image alongside `commands/`, `src/`, and `templates/`.

## Limitations

- AST extraction currently supports Python. Other languages fall back to `rg`-based search.
- Only function/method bodies are compared. Class-level or module-level duplicate code is not detected.
- Very small functions (< 3 statements) are excluded to avoid false positives on trivial helpers.
- The body hash is sensitive to string literal content -- two copies with different log messages will not match.
