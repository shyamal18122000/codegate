"""Schema validation tests for findings-schema.json, focused on suppressed_findings."""
import json
import os
import pytest
import jsonschema

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "commands", "findings-schema.json")


@pytest.fixture(scope="module")
def schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def _base_findings_doc():
    return {
        "schema_version": "1.0",
        "pr_id": 1,
        "repo": "test-repo",
        "vcs": "github",
        "review_modes": ["standard"],
        "findings": [],
    }


def test_schema_valid_with_suppressed_findings(schema):
    doc = _base_findings_doc()
    doc["suppressed_findings"] = [
        {
            "id": "cr-001",
            "file": "src/auth/login.py",
            "line": 42,
            "category": "security",
            "title": "SQL injection via unsanitized input",
            "reason": "matched dismissed pattern dismiss-001",
            "dismissed_id": "dismiss-001",
        }
    ]
    jsonschema.validate(doc, schema)


def test_schema_valid_without_suppressed_findings(schema):
    doc = _base_findings_doc()
    jsonschema.validate(doc, schema)


def test_suppressed_finding_requires_all_fields(schema):
    doc = _base_findings_doc()
    # Missing required field 'reason'
    doc["suppressed_findings"] = [
        {
            "id": "cr-001",
            "file": "src/auth/login.py",
            "line": 42,
            "category": "security",
            "title": "SQL injection via unsanitized input",
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, schema)
