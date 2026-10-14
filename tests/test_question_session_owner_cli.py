from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from test_question_session_controls import package


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["enable", "preserve", "stale", "wrong_route"])
async def test_owner_cli_question_approval_is_explicit_and_scoped(tmp_path, monkeypatch, capsys, case):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    close = store.close
    try:
        if case != "preserve":
            binding = events.bindings.bind(
                owner, binding["project_id"], binding["note_id"], binding["client_id"],
                binding["slack_workspace_id"], binding["slack_channel_id"], binding["slack_thread_ts"],
                expected_revision=binding["revision"], allow_questions=False,
            )
        before = dict(binding)
        main = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/mcp_test_request.py"))["main"]
        monkeypatch.setitem(main.__globals__, "DurableStore", lambda _: store)
        monkeypatch.setitem(main.__globals__, "Settings", SimpleNamespace(from_env=lambda: SimpleNamespace(
            data_dir=store.data_dir, public_origin="https://projects-hub.kenigevents.ru",
            session_secret="s" * 48, release_sha="test")))
        monkeypatch.setattr(store, "close", lambda: None)
        argv = ["mcp_test_request.py", "bind-discussion",
                "--project-id", binding["project_id"], "--note-id", binding["note_id"],
                "--client-id", binding["client_id"], "--workspace-id", binding["slack_workspace_id"],
                "--channel-id", binding["slack_channel_id"], "--thread-ts",
                "1791900002.000001" if case == "wrong_route" else binding["slack_thread_ts"],
                "--expected-revision", str(binding["revision"] - (case == "stale"))]
        if case != "preserve":
            argv.append("--allow-questions")
        monkeypatch.setattr(sys, "argv", argv)
        result = main()
        output = json.loads(capsys.readouterr().out)
        saved = dict(store.db.execute("SELECT * FROM mcp_discussion_bindings WHERE id=?", (binding["id"],)).fetchone())
        if case in ("stale", "wrong_route"):
            assert result == 1
            assert output["error"] == "BINDING_CONFLICT"
            assert saved == before
        else:
            assert result == 0
            assert saved["questions_allowed"] == 1
            assert saved["revision"] == before["revision"] + (case == "enable")
            for key in ("id", "project_id", "note_id", "owner_actor_id", "client_id",
                        "slack_workspace_id", "slack_channel_id", "slack_thread_ts", "mira_note_id"):
                assert saved[key] == before[key]
            audit = store.db.execute("SELECT route_json FROM mcp_binding_history WHERE binding_id=? AND revision=?",
                                     (saved["id"], saved["revision"])).fetchone()
            assert json.loads(audit["route_json"])["questions_allowed"] == 1
    finally:
        await service.close()
        close()
