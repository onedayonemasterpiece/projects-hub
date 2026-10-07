from __future__ import annotations

from pathlib import Path


SOURCE = Path("scripts/basic_collaboration_live_canary.py").read_text(encoding="utf-8")


def test_basic_collaboration_live_canary_compiles() -> None:
    compile(SOURCE, "scripts/basic_collaboration_live_canary.py", "exec")


def test_canary_uses_fresh_first_party_a_and_b_for_project_collaboration() -> None:
    assert 'Collaboration canary A {run_id}' in SOURCE
    assert 'Collaboration canary B {run_id}' in SOURCE
    assert '_assert_no_github_identity(data_dir, actor_a)' in SOURCE
    assert '_assert_no_github_identity(data_dir, actor_b)' in SOURCE
    assert 'participant_a_no_external_identity' in SOURCE
    assert 'participant_b_no_external_identity' in SOURCE


def test_canary_checks_a_brief_without_reusing_owner_attention_cursor() -> None:
    assert 'brief_a = _request(' in SOURCE
    assert 'A personal brief did not surface B\'s addressed reply' in SOURCE
    assert 'continuation_event_visible_to_owner' in SOURCE
    # Strong analysis remains owner-authorized while A/B stay ordinary editors.
    assert 'started_analysis = _request(\n                owner,' in SOURCE
