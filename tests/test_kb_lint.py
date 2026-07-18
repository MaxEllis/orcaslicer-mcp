# tests/test_kb_lint.py
from orcaslicer_mcp.knowledge_index import load_knowledge
from orcaslicer_mcp import settings_schema

def test_every_file_has_topics():
    for c in load_knowledge():
        assert c.topics, f"{c.relpath} missing topics frontmatter"
        assert c.body.strip(), f"{c.relpath} has empty body"

def test_every_orca_key_exists_in_schema():
    for c in load_knowledge():
        for k in c.orca_keys:
            assert settings_schema.describe(k) is not None, f"{c.relpath}: unknown key {k}"

def test_no_template_bundle_language():
    banned = ("recommended profile:", "apply this profile", "priority slider")
    for c in load_knowledge():
        low = c.body.lower()
        for b in banned:
            assert b not in low, f"{c.relpath} contains template-bundle language: {b}"
