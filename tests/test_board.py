from pathlib import Path

import pytest

from projects_hub.board import BoardService
from projects_hub.store import DurableStore, StoreError


def _owner(tmp_path: Path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Board owner")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    return store, actor, workspace, project


def test_one_board_per_project_and_viewer_does_not_hidden_create(tmp_path: Path):
    store, actor, workspace, project = _owner(tmp_path)
    try:
        board = BoardService(store)
        first = board.open_board(actor, workspace, project)
        second = board.open_board(actor, workspace, project)
        assert first["id"] == second["id"]

        # Explicitly create a second workspace member without an implicit project grant.
        other = "usr_viewer"
        with store._lock:
            store.db.execute(
                "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,1)",
                (other, "Viewer"),
            )
            store.db.execute(
                "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                (other, workspace, "member"),
            )
            store.db.execute(
                """INSERT INTO project_grants
                   (actor_id,project_id,role,can_analyze,can_manage_share,created_at_ms,revoked_at_ms)
                   VALUES(?,?,?,0,0,1,NULL)""",
                (other, project, "viewer"),
            )
        assert board.open_board(other, workspace, project, create_if_allowed=False)["id"] == first["id"]

        other_project = store.list_projects(actor, workspace)[1]["id"]
        store.grant_project_access(
            actor_id=actor, workspace_id=workspace, project_id=other_project,
            role="owner", can_analyze=True, can_manage_share=True,
        )
        # Owner grant exists, but prove a viewer cannot create a board when none exists.
        with store._lock:
            store.db.execute(
                """INSERT INTO project_grants
                   (actor_id,project_id,role,can_analyze,can_manage_share,created_at_ms,revoked_at_ms)
                   VALUES(?,?,?,0,0,1,NULL)
                   ON CONFLICT(actor_id,project_id) DO UPDATE SET role='viewer'""",
                (other, other_project, "viewer"),
            )
        with pytest.raises(StoreError) as exc:
            board.open_board(other, workspace, other_project)
        assert exc.value.code == "BOARD_NOT_FOUND"
        assert store.db.execute("SELECT COUNT(*) FROM boards WHERE project_id=?", (other_project,)).fetchone()[0] == 0
    finally:
        store.close()


def test_different_objects_do_not_conflict_same_object_does(tmp_path: Path):
    store, actor, workspace, project = _owner(tmp_path)
    try:
        board = BoardService(store)
        board_id = board.open_board(actor, workspace, project)["id"]
        a = board.apply_command(
            actor_id=actor, workspace_id=workspace, board_id=board_id,
            command_id="cmd_create_a", operation="create", object_id="obj_a01",
            expected_object_revision=None,
            payload={"type": "sticky", "text": "A", "style": {"color": "yellow"}},
        )
        b = board.apply_command(
            actor_id=actor, workspace_id=workspace, board_id=board_id,
            command_id="cmd_create_b", operation="create", object_id="obj_b01",
            expected_object_revision=None,
            payload={"type": "sticky", "text": "B", "style": {"color": "blue"}},
        )
        assert (a["board_seq"], b["board_seq"]) == (1, 2)

        moved = board.apply_command(
            actor_id=actor, workspace_id=workspace, board_id=board_id,
            command_id="cmd_move_a01", operation="move", object_id="obj_a01",
            expected_object_revision=1,
            payload={"geometry": {"x": 100, "y": 20}},
        )
        edited = board.apply_command(
            actor_id=actor, workspace_id=workspace, board_id=board_id,
            command_id="cmd_edit_b01", operation="update", object_id="obj_b01",
            expected_object_revision=1,
            payload={"text": "B2"},
        )
        assert moved["object_revision"] == edited["object_revision"] == 2

        with pytest.raises(StoreError) as exc:
            board.apply_command(
                actor_id=actor, workspace_id=workspace, board_id=board_id,
                command_id="cmd_stale_a1", operation="update", object_id="obj_a01",
                expected_object_revision=1, payload={"text": "stale"},
            )
        assert exc.value.code == "OBJECT_CONFLICT"
        assert board.snapshot(actor, workspace, board_id)["objects"][0]["text"] == "A"
    finally:
        store.close()


def test_command_idempotency_tail_search_history_and_ticket(tmp_path: Path):
    store, actor, workspace, project = _owner(tmp_path)
    try:
        board = BoardService(store)
        board_id = board.open_board(actor, workspace, project)["id"]
        kwargs = dict(
            actor_id=actor, workspace_id=workspace, board_id=board_id,
            command_id="cmd_idem_001", operation="create", object_id="obj_note1",
            expected_object_revision=None,
            payload={"text": "Критический риск API", "style": {"color": "pink"}},
        )
        first = board.apply_command(**kwargs)
        again = board.apply_command(**kwargs)
        assert first == again
        assert board.snapshot(actor, workspace, board_id)["board"]["seq"] == 1
        with pytest.raises(StoreError) as exc:
            board.apply_command(**{**kwargs, "payload": {"text": "other"}})
        assert exc.value.code == "COMMAND_CONFLICT"

        tail = board.tail(actor, workspace, board_id, after_seq=0)
        assert [e["board_seq"] for e in tail["events"]] == [1]
        assert board.search(actor, workspace, board_id, "риск")[0]["object_id"] == "obj_note1"
        assert board.history(actor, workspace, board_id, "obj_note1")[0]["initiating_actor_id"] == actor

        ticket = board.issue_socket_ticket(
            actor_id=actor, workspace_id=workspace, board_id=board_id,
            client_instance_id="client-instance-1",
        )
        scope = board.consume_socket_ticket(
            ticket=ticket["ticket"], board_id=board_id, client_instance_id="client-instance-1"
        )
        assert scope["actor_id"] == actor
        with pytest.raises(StoreError) as exc:
            board.consume_socket_ticket(
                ticket=ticket["ticket"], board_id=board_id, client_instance_id="client-instance-1"
            )
        assert exc.value.code == "BOARD_TICKET_INVALID"
    finally:
        store.close()


def test_project_grants_filter_bootstrap_and_revoke(tmp_path: Path):
    store, actor, workspace, project = _owner(tmp_path)
    try:
        board = BoardService(store)
        viewer = "usr_plain"
        with store._lock:
            store.db.execute(
                "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,1)",
                (viewer, "Plain"),
            )
            store.db.execute(
                "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                (viewer, workspace, "member"),
            )
        assert store.bootstrap(viewer, workspace)["projects"] == []
        with pytest.raises(StoreError) as exc:
            store.project_access(viewer, workspace, project)
        assert exc.value.code == "PROJECT_FORBIDDEN"

        with store._lock:
            store.db.execute(
                """INSERT INTO project_grants
                   (actor_id,project_id,role,can_analyze,can_manage_share,created_at_ms,revoked_at_ms)
                   VALUES(?,?,?,0,0,1,NULL)""",
                (viewer, project, "viewer"),
            )
        assert store.bootstrap(viewer, workspace)["projects"][0]["id"] == project
        board_id = board.open_board(actor, workspace, project)["id"]
        assert board.snapshot(viewer, workspace, board_id)["board"]["id"] == board_id
        store.revoke_project_access(
            owner_actor_id=actor, workspace_id=workspace,
            target_actor_id=viewer, project_id=project,
        )
        with pytest.raises(StoreError) as exc:
            board.snapshot(viewer, workspace, board_id)
        assert exc.value.code == "PROJECT_FORBIDDEN"
    finally:
        store.close()
