#!/usr/bin/env python3
"""Real owner needs_owner resume acceptance for deployed Projects Hub.

The script requires a pre-created read-only Codex quality task id. It creates one
bounded acceptance-only execution in the existing durable store, then answers its
owner-addressed collaboration question through the public product API. The
deployed DevelopmentService must continue the same quality task and execution;
no implementation task is created.

Run only on loopback/dev-auth deployment under the explicit owner-authorized
implementation acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


def request_json(
    opener: urllib.request.OpenerDirector,
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(req, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(8192).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {body[:800]}") from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"{method} {path} failed: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return value


def db_open(data_dir: Path) -> sqlite3.Connection:
    db = sqlite3.connect(data_dir / "projects-hub.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def owner_identity(data_dir: Path) -> tuple[str, str]:
    db = db_open(data_dir)
    try:
        row = db.execute(
            """SELECT a.id,a.display_name
               FROM platform_owner o
               JOIN actors a ON a.id=o.actor_id
               WHERE o.slot=1"""
        ).fetchone()
        if not row:
            raise RuntimeError("platform owner is absent")
        return str(row["id"]), str(row["display_name"])
    finally:
        db.close()


def seed_execution(
    data_dir: Path,
    *,
    actor_id: str,
    workspace_id: str,
    project_id: str,
    quality_task_id: str,
    candidate_sha: str,
) -> str:
    execution_id = "te_accept_" + uuid.uuid4().hex
    now = round(time.time() * 1000)
    prompt = (
        "Acceptance-only owner resume execution. No implementation scope. "
        f"Candidate {candidate_sha}. Continue only the existing read-only quality thread."
    )
    db = db_open(data_dir)
    try:
        active = db.execute(
            """SELECT id FROM task_executions
               WHERE actor_id=? AND status IN ('starting','running')
               ORDER BY created_at_ms DESC LIMIT 1""",
            (actor_id,),
        ).fetchone()
        if active:
            raise RuntimeError(
                "another owner development execution is active; acceptance seed refused"
            )
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO task_executions(
                   id,actor_id,workspace_id,project_id,task_ids_json,project_hint,
                   provider,model_profile,prompt,prompt_sha256,status,phase,phase_detail,
                   devcoveer_task_id,quota_remaining_percent,result_summary,error_code,
                   created_at_ms,updated_at_ms,started_at_ms,finished_at_ms,
                   quality_task_id,implementation_task_id,review_cycle,spec_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                execution_id,
                actor_id,
                workspace_id,
                project_id,
                "[]",
                "projects-hub-owner",
                "codex",
                "gpt-6-astra:high",
                prompt,
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "blocked",
                "needs_owner",
                "Acceptance requires an explicit owner decision before the existing quality review may continue",
                quality_task_id,
                None,
                (
                    "Acceptance-only bounded execution. Preserve exact collaboration "
                    f"candidate {candidate_sha}; no implementation task or new scope is authorized."
                ),
                "REVIEW_VERDICT_MISSING",
                now,
                now,
                now,
                now,
                quality_task_id,
                None,
                0,
                "docs/prompts/basic-collaboration-prototype-20261006.md",
            ),
        )
        db.commit()
        return execution_id
    finally:
        db.close()


def execution_row(data_dir: Path, execution_id: str) -> dict[str, Any]:
    db = db_open(data_dir)
    try:
        row = db.execute(
            """SELECT id,status,phase,error_code,quality_task_id,
                      implementation_task_id,project_hint,review_cycle
               FROM task_executions WHERE id=?""",
            (execution_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("acceptance execution disappeared")
        return dict(row)
    finally:
        db.close()


def resume_receipt_count(data_dir: Path, execution_id: str) -> tuple[int, str | None]:
    db = db_open(data_dir)
    try:
        rows = db.execute(
            """SELECT status FROM development_owner_resumes
               WHERE execution_id=? ORDER BY created_at_ms""",
            (execution_id,),
        ).fetchall()
        return len(rows), str(rows[-1]["status"]) if rows else None
    finally:
        db.close()


def run(
    *,
    base: str,
    data_dir: Path,
    project_name: str,
    quality_task_id: str,
    candidate_sha: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    owner_actor_id, owner_name = owner_identity(data_dir)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    health = request_json(opener, base, "GET", "/healthz")
    if health.get("release_sha") != candidate_sha:
        raise RuntimeError(
            "deployed release SHA does not match owner-resume acceptance candidate"
        )
    login = request_json(
        opener,
        base,
        "POST",
        "/api/dev/login",
        {"display_name": owner_name},
    )
    if str((login.get("actor") or {}).get("id") or "") != owner_actor_id:
        raise RuntimeError("dev login did not resolve the platform owner actor")
    workspace_id = str((login.get("workspace") or {}).get("id") or "")
    project = next(
        (
            item for item in login.get("projects") or []
            if isinstance(item, dict) and item.get("name") == project_name
        ),
        None,
    )
    if not workspace_id or not project:
        raise RuntimeError("owner workspace/target project is unavailable")
    project_id = str(project["id"])

    execution_id = seed_execution(
        data_dir,
        actor_id=owner_actor_id,
        workspace_id=workspace_id,
        project_id=project_id,
        quality_task_id=quality_task_id,
        candidate_sha=candidate_sha,
    )

    inbox = request_json(
        opener,
        base,
        "GET",
        "/api/collaboration/questions/inbox?"
        + urllib.parse.urlencode({"workspace_id": workspace_id, "limit": 50}),
    ).get("items") or []
    question = next(
        (
            item for item in inbox
            if isinstance(item, dict)
            and item.get("source_kind") == "owner_development"
            and item.get("execution_id") == execution_id
        ),
        None,
    )
    if not question:
        raise RuntimeError("needs_owner execution did not surface an owner collaboration question")

    command_id = "accept.owner." + uuid.uuid4().hex
    answer = request_json(
        opener,
        base,
        "POST",
        f"/api/collaboration/questions/{question['id']}/respond",
        {
            "workspace_id": workspace_id,
            "command_id": command_id,
            "disposition": "answer",
            "body": (
                "Acceptance decision: preserve the exact candidate scope and DoD. "
                "Do not create or modify implementation work; continue only this "
                "existing read-only quality review thread and decide whether the "
                "already implemented candidate can be accepted."
            ),
        },
        timeout=90.0,
    )
    execution = answer.get("execution") or {}
    if (
        answer.get("continuation") != "resumed"
        or execution.get("id") != execution_id
        or execution.get("quality_task_id") != quality_task_id
        or execution.get("implementation_task_id") not in {None, ""}
        or execution.get("status") != "running"
        or execution.get("phase") != "reviewing"
    ):
        raise RuntimeError("owner answer did not resume the same bounded execution/thread")

    deadline = time.monotonic() + timeout_seconds
    final = execution_row(data_dir, execution_id)
    while time.monotonic() < deadline:
        final = execution_row(data_dir, execution_id)
        if final["status"] in {"completed", "failed", "cancelled", "blocked"}:
            break
        time.sleep(2.0)

    count, receipt_status = resume_receipt_count(data_dir, execution_id)
    if count != 1 or receipt_status != "applied":
        raise RuntimeError("owner resume receipt is not exactly-once/applied")
    if final["quality_task_id"] != quality_task_id:
        raise RuntimeError("quality task identity changed during resume")
    if final["implementation_task_id"] not in {None, ""}:
        raise RuntimeError("owner resume unexpectedly created an implementation task")

    return {
        "ok": final["status"] == "completed",
        "release_sha": str(health.get("release_sha") or ""),
        "execution_id": execution_id,
        "question_id": str(question["id"]),
        "quality_task_id": quality_task_id,
        "same_execution_resumed": True,
        "same_quality_thread_resumed": True,
        "implementation_task_created": False,
        "resume_receipt_count": count,
        "resume_receipt_status": receipt_status,
        "final_status": final["status"],
        "final_phase": final["phase"],
        "final_error_code": final["error_code"],
        "review_cycle": final["review_cycle"],
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8196")
    parser.add_argument(
        "--data-dir",
        default="/home/dev/.local/state/projects-hub/data",
    )
    parser.add_argument("--project", default="Projects Hub")
    parser.add_argument("--quality-task-id", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    result = run(
        base=args.base,
        data_dir=Path(args.data_dir),
        project_name=args.project,
        quality_task_id=args.quality_task_id,
        candidate_sha=args.candidate_sha,
        timeout_seconds=max(30.0, min(float(args.timeout_seconds), 300.0)),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
