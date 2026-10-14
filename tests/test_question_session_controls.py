from __future__ import annotations

import pytest

from test_collaboration_analysis import setup
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.mcp_auth import MCPAuth, REDIRECT
from projects_hub.mcp_events import MCPEvents
from projects_hub.store import DurableStore, StoreError


async def package(tmp_path):
    store, collaboration, service, bridge, owner, actor, workspace, project = setup(tmp_path)
    note = await collaboration.create_note(
        actor_id=owner, workspace_id=workspace, project_id=project,
        command_id="note.session.controls", title="Session source", body="Two independent decisions.",
    )
    auth = MCPAuth(store, "https://projects-hub.kenigevents.ru")
    events = MCPEvents(store, auth, "s" * 48)
    credential = auth.provision_service(owner, "ph_service_session_controls", [REDIRECT])
    producer = auth.service_client(credential["client_id"])["actor_id"]
    binding = events.bindings.bind(
        owner, project, note["id"], credential["client_id"],
        "T0123456789", "C0123456789", "1791900001.000001", allow_questions=True,
    )
    service.question_events = events
    analysis = service.publish_dot_package(
        initiating_actor_id=owner, producer_actor_id=producer,
        workspace_id=workspace, project_id=project, note_id=note["id"],
        addressed_to_actor_id=actor, command_id="dot.session.controls",
        external_analysis_id="external.session.controls", origin="slack",
        source_kind="project_note", source_id=note["id"], source_revision=note["revision"],
        summary="Choose both", purpose="requirements",
        binding_id=binding["id"], binding_revision=binding["revision"],
        slack_workspace_id=binding["slack_workspace_id"],
        slack_channel_id=binding["slack_channel_id"], slack_thread_ts=binding["slack_thread_ts"],
        questions=[
            dict(external_question_id="essential", prompt="Required choice?", context="First decision",
                 reason="Blocks design", blocking=True, priority="high"),
            dict(external_question_id="optional", prompt="Optional choice?", context="Second decision",
                 reason="Improves design", blocking=False, priority="low"),
        ],
    )
    args = dict(actor_id=actor, workspace_id=workspace, analysis_id=analysis["id"])
    return store, service, events, bridge, owner, binding, note, analysis, args


@pytest.mark.asyncio
async def test_explicit_complete_only_once_and_receipts_survive_restart(tmp_path):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        answers = service.answer_questions(**args, command_id="answer.both", responses=[
            dict(question_id=q["id"], disposition="answer", body="Chosen") for q in analysis["questions"]
        ])
        assert answers["continuation"] == "ready_to_complete"
        assert answers["receipt_id"] == "answer.both"
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 0
        complete = service.control_question_session(**args, command_id="complete.once", action="complete")
        assert complete["continuation"] == "waiting_event_transport"
        assert complete["continuation_request_id"]
        assert service.control_question_session(**args, command_id="complete.once", action="complete") == complete
        again = service.control_question_session(**args, command_id="complete.again", action="complete")
        assert again == dict(complete, receipt_id="complete.again")
        with pytest.raises(StoreError) as alias_changed:
            service.control_question_session(**args, command_id="complete.again", action="pause")
        assert alias_changed.value.code == "COLLABORATION_COMMAND_CONFLICT"
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 1
        with pytest.raises(StoreError) as changed:
            service.control_question_session(**args, command_id="complete.once", action="pause")
        assert changed.value.code == "COLLABORATION_COMMAND_CONFLICT"
        public = service.get_analysis(**args)
        assert public["receipt_ids"] == ["answer.both"]
        assert public["answer_receipts"] == [answers]
        assert {r["receipt_id"] for r in public["session_receipts"]} == {"complete.once", "complete.again"}
        reopened = DurableStore(store.data_dir)
        try:
            restarted = CollaborationAnalysisService(reopened, service.collaboration, bridge=bridge)
            assert restarted.control_question_session(
                **args, command_id="complete.once", action="complete"
            ) == complete
            assert restarted.get_analysis(**args)["answer_receipts"] == [answers]
        finally:
            reopened.close()
        assert not bridge.consults
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_pause_filter_single_answer_never_completes_batch(tmp_path):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        for action in ["offer", "accept", "pause"]:
            receipt = service.control_question_session(**args, command_id="control." + action, action=action)
            assert service.control_question_session(**args, command_id="control." + action, action=action) == receipt
        q = next(q for q in analysis["questions"] if q["blocking"])
        answer = service.answer_questions(
            **args, command_id="answer.single", responses=[dict(question_id=q["id"], disposition="answer", body="Yes")]
        )
        assert answer["continuation"] == "paused"
        assert service.get_analysis(**args)["package_state"] == "paused"
        filtered = service.control_question_session(
            **args, command_id="control.filter", action="important_only", important_only=True,
        )
        assert filtered["important_only"] and len(filtered["questions"]) == 1
        resumed = service.control_question_session(**args, command_id="control.resume", action="resume")
        assert resumed["package_state"] == "active"
        assert resumed["important_only"]
        unfiltered = service.control_question_session(
            **args, command_id="control.unfilter", action="resume", important_only=False,
        )
        assert not unfiltered["important_only"]
        inbox = service.inbox(actor_id=args["actor_id"], workspace_id=args["workspace_id"])
        assert all(q["package_last_offered_at_ms"] and q["package_state"] == "active" for q in inbox)
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 0
        assert not bridge.consults
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["unknown", "skip", "later"])
async def test_unresolved_blockers_need_explicit_partial_completion(tmp_path, disposition):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        blocker = next(q for q in analysis["questions"] if q["blocking"])
        answer = service.answer_questions(
            **args, command_id="answer.unresolved", responses=[dict(question_id=blocker["id"], disposition=disposition)]
        )
        saved = next(q for q in answer["questions"] if q["id"] == blocker["id"])
        assert saved["state"] == ("deferred" if disposition == "later" else "open")
        assert saved["disposition"] == disposition
        with pytest.raises(StoreError) as unresolved:
            service.control_question_session(**args, command_id="complete.full", action="complete")
        assert unresolved.value.code == "QUESTION_SESSION_UNRESOLVED"
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 0
        complete = service.control_question_session(
            **args, command_id="complete.partial", action="complete", allow_partial=True,
        )
        assert complete["package_state"] == "partially_completed"
        assert complete["allow_partial"] is True
        assert complete["unresolved_blocking_question_ids"] == [blocker["id"]]
        assert len(complete["unresolved_question_ids"]) == 2
        assert service.get_analysis(**args)["package_state"] == "partially_completed"
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 1
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actor", "grant", "source", "binding"])
async def test_control_rechecks_current_authority_even_for_replay(tmp_path, change):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        service.control_question_session(**args, command_id="offer.authorized", action="offer")
        if change == "actor":
            args["actor_id"] = owner
        elif change == "grant":
            store.db.execute("UPDATE project_grants SET revoked_at_ms=1 WHERE actor_id=?", (args["actor_id"],))
        elif change == "source":
            store.db.execute("UPDATE project_notes SET revision=revision+1 WHERE id=?", (note["id"],))
        else:
            store.db.execute("UPDATE mcp_discussion_bindings SET revision=revision+1 WHERE id=?", (binding["id"],))
        with pytest.raises(StoreError):
            service.control_question_session(**args, command_id="offer.authorized", action="offer")
        with pytest.raises(StoreError):
            service.control_question_session(**args, command_id="offer.new", action="offer")
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_completion_and_outbox_rollback_together(tmp_path, monkeypatch):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        enqueue = events.enqueue_question_ready
        def fail_after_enqueue(row):
            enqueue(row)
            raise RuntimeError("Simulated crash before receipt")
        monkeypatch.setattr(events, "enqueue_question_ready", fail_after_enqueue)
        with pytest.raises(RuntimeError):
            service.control_question_session(**args, command_id="complete.atomic", action="complete", allow_partial=True)
        assert service.get_analysis(**args)["package_state"] == "waiting_answers"
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM collaboration_answer_commands").fetchone()[0] == 0
        monkeypatch.setattr(events, "enqueue_question_ready", enqueue)
        receipt = service.control_question_session(
            **args, command_id="complete.atomic", action="complete", allow_partial=True,
        )
        assert receipt["continuation_request_id"]
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_one_question_answer_and_empty_package_still_require_complete(tmp_path):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        store.db.execute("DELETE FROM collaboration_questions WHERE analysis_id=? AND blocking=0", (analysis["id"],))
        question = next(q for q in analysis["questions"] if q["blocking"])
        answer = service.answer_questions(
            **args, command_id="answer.only", responses=[
                dict(question_id=question["id"], disposition="answer", body="Only answer")
            ],
        )
        assert answer["continuation"] == "ready_to_complete"
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 0
        service.control_question_session(**args, command_id="complete.only", action="complete")
        with pytest.raises(StoreError) as closed:
            service.answer_questions(
                **args, command_id="answer.after.complete", responses=[
                    dict(question_id=question["id"], disposition="answer", body="Changed")
                ],
            )
        assert closed.value.code == "QUESTION_SESSION_COMPLETED"
        with pytest.raises(StoreError) as collision:
            service.control_question_session(**args, command_id="answer.only", action="pause")
        assert collision.value.code == "COLLABORATION_COMMAND_CONFLICT"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["binding_disabled", "binding_revision", "binding_revoked",
                                    "source_revision", "source_status", "addressee_grant", "requester_grant"])
async def test_dot_reads_recheck_binding_source_and_both_project_grants(tmp_path, change):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        assert len(service.inbox(actor_id=args["actor_id"], workspace_id=args["workspace_id"])) == 2
        assert service.get_analysis(**args)["id"] == analysis["id"]
        # The requester may read a package addressed to a participant.
        owner_args = dict(args, actor_id=owner)
        assert service.get_analysis(**owner_args)["id"] == analysis["id"]
        if change == "binding_disabled":
            store.db.execute("UPDATE mcp_discussion_bindings SET questions_allowed=0 WHERE id=?", (binding["id"],))
        elif change == "binding_revision":
            store.db.execute("UPDATE mcp_discussion_bindings SET revision=revision+1 WHERE id=?", (binding["id"],))
        elif change == "binding_revoked":
            store.db.execute("UPDATE mcp_discussion_bindings SET state='revoked' WHERE id=?", (binding["id"],))
        elif change == "source_revision":
            store.db.execute("UPDATE project_notes SET revision=revision+1 WHERE id=?", (note["id"],))
        elif change == "source_status":
            store.db.execute("UPDATE project_notes SET status='failed' WHERE id=?", (note["id"],))
        else:
            actor = args["actor_id"] if change == "addressee_grant" else owner
            store.db.execute("UPDATE project_grants SET revoked_at_ms=1 WHERE actor_id=?", (actor,))
        assert service.inbox(actor_id=args["actor_id"], workspace_id=args["workspace_id"]) == []
        assert service.inbox(**args) == []
        for reader in [args, owner_args]:
            with pytest.raises(StoreError):
                service.get_analysis(**reader)
        assert not bridge.consults
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_state", ["awaiting_delivery", "waiting_answers"])
async def test_complete_intent_freezes_answers_during_result_delivery(tmp_path, result_state):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    try:
        blocker = next(q for q in analysis["questions"] if q["blocking"])
        response = [dict(question_id=blocker["id"], disposition="unknown")]
        saved = service.answer_questions(**args, command_id="answer.before.partial", responses=response)
        service.control_question_session(
            **args, command_id="complete.before.result", action="complete", allow_partial=True,
        )
        # Result persistence moves the package to awaiting_delivery. Even a legacy
        # state repair must not erase the durable completion boundary.
        store.db.execute("UPDATE collaboration_analyses SET package_state=? WHERE id=?",
                         (result_state, analysis["id"]))
        with pytest.raises(StoreError) as rejected:
            service.answer_questions(
                **args, command_id="answer.after.result",
                responses=[dict(question_id=blocker["id"], disposition="answer", body="Too late")],
            )
        assert rejected.value.code == "QUESTION_SESSION_COMPLETED"
        assert service.answer_questions(
            **args, command_id="answer.before.partial", responses=response
        ) == saved
        current = service.get_analysis(**args)
        assert current["package_state"] == result_state
        assert current["receipt_ids"] == ["answer.before.partial"]
        assert next(q for q in current["questions"] if q["id"] == blocker["id"])["disposition"] == "unknown"
    finally:
        await service.close()
        store.close()
