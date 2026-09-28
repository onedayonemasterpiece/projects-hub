"""Offline documentation integrity only; NOT product/security/provider acceptance."""
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "product"


def load_contract():
    return json.loads((DOCS / "contract.json").read_text(encoding="utf-8"))


def test_source_registry_is_unique_pinned_and_traceable():
    spec = load_contract()
    assert spec["schema_version"] == 2
    assert re.fullmatch(r"[0-9a-f]{40}", spec["source_snapshot"])
    sources = spec["sources"]
    assert len(sources) == len({s["id"] for s in sources})
    assert len(sources) == len({s["voice_id"] for s in sources})
    assert {s["group"] for s in sources} == {"core", "retrospective"}
    narrative = (DOCS / "01-evidence.md").read_text(encoding="utf-8")
    for source in sources:
        match = re.fullmatch(r"voice-(\d{4})(\d{2})(\d{2})-\d{6}-[0-9a-f]{8}", source["voice_id"])
        assert match, source
        year, month, _ = match.groups()
        expected = (f"https://github.com/{spec['source_repository']}/blob/"
                    f"{spec['source_snapshot']}/inbox/voice/{year}/{month}/{source['voice_id']}.md")
        assert source["url"] == expected
        assert source["voice_id"] in narrative


def test_integration_registry_has_unique_owners_and_explicit_status():
    spec = load_contract()
    entries = spec["integrations"]
    assert len(entries) == len({entry["id"] for entry in entries})
    assert len(entries) == len({entry["repository"] for entry in entries})
    for entry in entries:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repository"])
        assert entry["owner_of"].strip() and entry["status"].strip()
    assert {"idea-hub", "record-idea-hub", "live-interaction", "ai-resource-control"} <= {e["id"] for e in entries}


def test_local_markdown_links_resolve_without_leaving_repository():
    for document in DOCS.glob("*.md"):
        text = document.read_text(encoding="utf-8")
        assert text.startswith("# "), document
        for raw in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            url = urlsplit(raw)
            if url.scheme or url.netloc or not url.path:
                continue
            target = (document.parent / unquote(url.path)).resolve()
            assert target.is_relative_to(ROOT), (document, raw)
            assert target.is_file(), (document, raw)


def test_release_gates_cannot_claim_pass_without_evidence():
    spec = load_contract()
    gates = spec["release_gates"]
    assert len(gates) == len({g["id"] for g in gates})
    narrative = (DOCS / "08-reliability.md").read_text(encoding="utf-8")
    for gate in gates:
        assert gate["status"] in {"not_run", "blocked", "failed", "passed"}
        assert gate["id"] in narrative
        assert isinstance(gate["evidence"], list)
        if gate["status"] == "passed":
            assert gate["evidence"], gate
    if spec["production_product_accepted"]:
        assert all(g["status"] == "passed" and g["evidence"] for g in gates)