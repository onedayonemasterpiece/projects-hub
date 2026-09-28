from pathlib import Path
import sqlite3

from projects_hub.store import DurableStore


def test_one_voice_source_can_create_distinct_project_memories_without_sharing_full_transcript(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Multi")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        projects = {item["name"]: item["id"] for item in boot["projects"]}
        conversation = store.create_conversation(actor, workspace)
        source = store.create_source(actor, conversation["id"])
        transcript = "Для Projects Hub нужен offline replay. Для Wonderful Lections нужен новый шаблон."
        store.append_source_event(actor, source["id"], "input_transcript", transcript)

        first = store.commit_memory(
            actor_id=actor,
            source_id=source["id"],
            command_id="cmd_projects_hub",
            project_id=projects["Projects Hub"],
            title="Offline replay",
            kind="requirement",
            semantic_notes="Добавить надёжную повторную доставку сохранённой речи.",
            args_sha256="a" * 64,
        )
        second = store.commit_memory(
            actor_id=actor,
            source_id=source["id"],
            command_id="cmd_lections",
            project_id=projects["Wonderful Lections"],
            title="Новый шаблон",
            kind="requirement",
            semantic_notes="Добавить новый шаблон лекции.",
            args_sha256="b" * 64,
        )

        assert first["memory_id"] != second["memory_id"]
        assert first["source_id"] == second["source_id"] == source["id"]
        assert len(store.list_memories(actor, workspace, projects["Projects Hub"])) == 1
        assert len(store.list_memories(actor, workspace, projects["Wonderful Lections"])) == 1

        memory_files = sorted((tmp_path / "memory" / workspace).glob("*.md"))
        assert len(memory_files) == 2
        project_text = "\n".join(path.read_text(encoding="utf-8") for path in memory_files)
        assert transcript not in project_text
        assert "Добавить надёжную повторную доставку сохранённой речи." in project_text
        assert "Добавить новый шаблон лекции." in project_text

        source_files = sorted((tmp_path / "sources" / workspace).glob("*.md"))
        assert len(source_files) == 1
        assert transcript in source_files[0].read_text(encoding="utf-8")
    finally:
        store.close()


def test_same_source_project_title_updates_one_memory_revision(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Revision")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor, workspace, project)
        source = store.create_source(actor, conversation["id"])
        store.append_source_event(actor, source["id"], "input_transcript", "Запомни решение.")
        first = store.commit_memory(actor_id=actor, source_id=source["id"], command_id="cmd_1", project_id=project, title="Решение", kind="decision", semantic_notes="Первая версия.", args_sha256="1" * 64)
        second = store.commit_memory(actor_id=actor, source_id=source["id"], command_id="cmd_2", project_id=project, title="Решение", kind="decision", semantic_notes="Уточнённая версия.", args_sha256="2" * 64)
        assert first["memory_id"] == second["memory_id"]
        assert second["revision"] == 2
        items = store.list_memories(actor, workspace, project)
        assert len(items) == 1
        assert items[0]["semantic_notes"] == "Уточнённая версия."
    finally:
        store.close()

def test_legacy_single_source_memory_migrates_without_project_transcript_leak(tmp_path: Path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Legacy")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    conversation = store.create_conversation(actor, workspace, project)
    source = store.create_source(actor, conversation["id"])
    transcript = "Старый полный transcript не должен остаться в project memory."
    store.append_source_event(actor, source["id"], "input_transcript", transcript)
    result = store.commit_memory(
        actor_id=actor,
        source_id=source["id"],
        command_id="cmd_before_legacy_shape",
        project_id=project,
        title="Legacy decision",
        kind="decision",
        semantic_notes="Сохранить только это решение.",
        args_sha256="c" * 64,
    )
    store.close()

    db_path = tmp_path / "projects-hub.sqlite3"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    row = dict(db.execute("SELECT * FROM memories").fetchone())
    db.execute("DROP INDEX IF EXISTS memories_project_idx")
    db.execute("ALTER TABLE memories RENAME TO memories_v2_fixture")
    db.execute(
        """CREATE TABLE memories(
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            project_id TEXT REFERENCES projects(id),
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            semantic_notes TEXT NOT NULL,
            transcript_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            markdown_path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        )"""
    )
    db.execute(
        """INSERT INTO memories
           (id,source_id,workspace_id,project_id,title,kind,semantic_notes,
            transcript_revision,revision,markdown_path,content_sha256,created_at_ms,updated_at_ms)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(
            row[key]
            for key in (
                "id",
                "source_id",
                "workspace_id",
                "project_id",
                "title",
                "kind",
                "semantic_notes",
                "transcript_revision",
                "revision",
                "markdown_path",
                "content_sha256",
                "created_at_ms",
                "updated_at_ms",
            )
        ),
    )
    db.execute("DROP TABLE memories_v2_fixture")
    db.commit()
    db.close()
    legacy_path = tmp_path / row["markdown_path"]
    legacy_path.write_text("# leaked legacy\n\n" + transcript, encoding="utf-8")

    migrated = DurableStore(tmp_path)
    try:
        columns = {
            row["name"]
            for row in migrated.db.execute("PRAGMA table_info(memories)")
        }
        assert "memory_key" in columns
        item = migrated.list_memories(actor, workspace, project)[0]
        assert item["id"] == result["memory_id"]
        memory_text = next(
            (tmp_path / "memory" / workspace).glob("*.md")
        ).read_text(encoding="utf-8")
        assert transcript not in memory_text
        assert "Сохранить только это решение." in memory_text
        source_text = next(
            (tmp_path / "sources" / workspace).glob("*.md")
        ).read_text(encoding="utf-8")
        assert transcript in source_text
    finally:
        migrated.close()
