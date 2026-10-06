#!/usr/bin/env python3
"""Real two-actor collaboration acceptance against the deployed Projects Hub.

The canary uses the running HTTP product surface. It never prints session/invite
tokens or GitHub credentials. A temporary participant is granted only the target
project and is revoked after the proof; the accepted Note/Reply/Analysis remain
as durable evidence.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import hashlib
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def _request(
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
    parsed_base = urllib.parse.urlsplit(base)
    if (
        method.upper() not in {"GET", "HEAD", "OPTIONS"}
        and parsed_base.scheme.lower() == "https"
        and parsed_base.netloc
    ):
        # Public acceptance calls emulate a browser same-origin mutation.
        headers["Origin"] = f"{parsed_base.scheme.lower()}://{parsed_base.netloc}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(8192).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {body[:800]}") from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"{method} {path} failed: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return value


def _request_status(
    opener: urllib.request.OpenerDirector,
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> int:
    data = None
    headers = {"Accept": "application/json"}
    parsed_base = urllib.parse.urlsplit(base)
    if (
        method.upper() not in {"GET", "HEAD", "OPTIONS"}
        and parsed_base.scheme.lower() == "https"
        and parsed_base.netloc
    ):
        # Public acceptance calls emulate a browser same-origin mutation.
        headers["Origin"] = f"{parsed_base.scheme.lower()}://{parsed_base.netloc}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=20.0) as response:
            response.read(4096)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.read(4096)
        return int(exc.code)


def _owner_display_name(data_dir: Path) -> str:
    db = sqlite3.connect(data_dir / "projects-hub.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(
            """SELECT a.display_name
               FROM platform_owner o JOIN actors a ON a.id=o.actor_id
               WHERE o.slot=1"""
        ).fetchone()
        if not row:
            raise RuntimeError("platform owner is absent")
        return str(row["display_name"])
    finally:
        db.close()


def _assert_no_github_identity(data_dir: Path, actor_id: str) -> None:
    db = sqlite3.connect(data_dir / "projects-hub.sqlite3")
    try:
        row = db.execute(
            "SELECT 1 FROM external_identities WHERE actor_id=? LIMIT 1",
            (actor_id,),
        ).fetchone()
        if row is not None:
            raise RuntimeError("temporary participant unexpectedly has an external identity")
    finally:
        db.close()


def _create_same_workspace_actor_without_project(
    data_dir: Path,
    workspace_id: str,
    display_name: str,
) -> tuple[str, str]:
    actor_id = "usr_canary_" + uuid.uuid4().hex
    invite_token = secrets.token_urlsafe(32)
    token_sha256 = hashlib.sha256(invite_token.encode("utf-8")).hexdigest()
    now = round(time.time() * 1000)
    db = sqlite3.connect(data_dir / "projects-hub.sqlite3")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (actor_id, display_name, now),
        )
        db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (actor_id, workspace_id, "member"),
        )
        db.execute(
            """INSERT INTO login_invites(
                   token_sha256,actor_id,expires_at_ms,used_at_ms,created_at_ms)
               VALUES(?,?,?,?,?)""",
            (token_sha256, actor_id, now + 3_600_000, None, now),
        )
        db.commit()
        return actor_id, invite_token
    finally:
        db.close()


def _cleanup_actor(
    data_dir: Path,
    *,
    actor_id: str,
    project_id: str | None,
    delete_actor: bool,
) -> None:
    db = sqlite3.connect(data_dir / "projects-hub.sqlite3")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM login_invites WHERE actor_id=?", (actor_id,))
        if project_id:
            db.execute(
                """UPDATE project_grants
                   SET revoked_at_ms=COALESCE(revoked_at_ms,?)
                   WHERE actor_id=? AND project_id=?""",
                (round(time.time() * 1000), actor_id, project_id),
            )
        db.execute("DELETE FROM memberships WHERE actor_id=?", (actor_id,))
        if delete_actor:
            db.execute("DELETE FROM actors WHERE id=?", (actor_id,))
        db.commit()
    finally:
        db.close()


def _project(login: dict[str, Any], name: str) -> dict[str, Any]:
    for item in login.get("projects") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    raise RuntimeError(f"project {name!r} is absent from owner bootstrap")


def _poll_analysis(
    opener: urllib.request.OpenerDirector,
    base: str,
    *,
    workspace_id: str,
    analysis_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    current: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = _request(
            opener,
            base,
            "POST",
            f"/api/collaboration/analyses/{analysis_id}/refresh?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
            timeout=45.0,
        )
        if current.get("status") in {"completed", "failed", "cancelled"}:
            return current
        time.sleep(2.0)
    raise RuntimeError(f"analysis did not become terminal: {current.get('status')}")


def _poll_job(
    opener: urllib.request.OpenerDirector,
    base: str,
    *,
    workspace_id: str,
    job_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    current: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = _request(
            opener,
            base,
            "GET",
            f"/api/collaboration/jobs/{job_id}?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
        )
        if current.get("status") in {"completed", "failed"}:
            return current
        time.sleep(2.0)
    raise RuntimeError(f"continuation did not become terminal: {current.get('status')}")


def run(
    *,
    base: str,
    public_base: str,
    data_dir: Path,
    project_name: str,
    with_analysis: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    actor_b = ""
    actor_c = ""
    project_id = ""
    owner = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    participant = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    outsider = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    health = _request(owner, base, "GET", "/healthz")
    owner_name = _owner_display_name(data_dir)
    login_a = _request(
        owner,
        base,
        "POST",
        "/api/dev/login",
        {"display_name": owner_name},
    )
    workspace_id = str((login_a.get("workspace") or {}).get("id") or "")
    if not workspace_id:
        raise RuntimeError("owner login returned no workspace")
    target = _project(login_a, project_name)
    project_id = str(target["id"])

    note_id = ""
    analysis_id = ""
    continuation_job_id = ""
    try:
        invited = _request(
            owner,
            base,
            "POST",
            f"/api/projects/{project_id}/participants/invite",
            {
                "workspace_id": workspace_id,
                "display_name": f"Collaboration canary B {run_id}",
                "role": "editor",
                "ttl_seconds": 3600,
            },
        )
        actor_b = str(invited.get("actor_id") or "")
        if not actor_b:
            raise RuntimeError("participant invite returned no actor")
        _assert_no_github_identity(data_dir, actor_b)

        login_b = _request(
            participant,
            public_base,
            "POST",
            "/api/auth/invite",
            {"token": str(invited.get("invite_token") or "")},
        )
        b_projects = login_b.get("projects") or []
        if [item.get("id") for item in b_projects if isinstance(item, dict)] != [project_id]:
            raise RuntimeError("participant B received projects outside the explicit grant")

        note = _request(
            owner,
            base,
            "POST",
            "/api/collaboration/notes",
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "command_id": f"canary.note.{run_id}",
                "title": f"Collaboration acceptance {run_id}",
                "body": (
                    "Проверяем базовую коллаборацию Projects Hub. "
                    "Одна личная лента должна сохраняться при смене проектов. "
                    "Проектная заметка обязана пройти Gemini-структурирование, "
                    "запись Markdown в привязанный репозиторий и authoritative readback."
                ),
            },
            timeout=240.0,
        )
        note_id = str(note.get("id") or "")
        if note.get("status") != "ready" or not note_id:
            raise RuntimeError(f"note is not ready: {note.get('status')}")
        if not note.get("structured") or not (note.get("processing") or {}).get("model"):
            raise RuntimeError("note has no structured Gemini processing evidence")

        readback = _request(
            owner,
            base,
            "GET",
            f"/api/collaboration/notes/{note_id}/readback?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
            timeout=60.0,
        )
        if readback.get("verified") is not True:
            raise RuntimeError("repository note readback was not verified")

        visible_b = _request(
            participant,
            public_base,
            "GET",
            f"/api/collaboration/notes/{note_id}?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
        )
        if visible_b.get("id") != note_id:
            raise RuntimeError("participant B could not read the note through Projects Hub")

        reply = _request(
            participant,
            public_base,
            "POST",
            f"/api/collaboration/notes/{note_id}/replies",
            {
                "workspace_id": workspace_id,
                "command_id": f"canary.reply.{run_id}",
                "body": "B прочитал заметку внутри Projects Hub и подтверждает связанный ответ.",
            },
        )
        reply_id = str(reply.get("id") or "")
        replies_a = _request(
            owner,
            base,
            "GET",
            f"/api/collaboration/notes/{note_id}/replies?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
        ).get("items") or []
        if not reply_id or not any(
            isinstance(item, dict) and item.get("id") == reply_id for item in replies_a
        ):
            raise RuntimeError("A did not read back B's linked reply")

        actor_c, outsider_token = _create_same_workspace_actor_without_project(
            data_dir,
            workspace_id,
            f"Collaboration canary C {run_id}",
        )
        _request(
            outsider,
            public_base,
            "POST",
            "/api/auth/invite",
            {"token": outsider_token},
        )
        denied_status = _request_status(
            outsider,
            public_base,
            "GET",
            f"/api/collaboration/notes/{note_id}?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
        )
        if denied_status != 403:
            raise RuntimeError(f"actor without project grant got HTTP {denied_status}")

        analysis_result: dict[str, Any] | None = None
        job_result: dict[str, Any] | None = None
        questions_answered = 0
        if with_analysis:
            started_analysis = _request(
                owner,
                base,
                "POST",
                "/api/collaboration/analyses",
                {
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "note_id": note_id,
                    "addressed_to_actor_id": actor_b,
                    "command_id": f"canary.analysis.{run_id}",
                    "model": "kimi_k3",
                    "purpose": "requirements",
                    "question": (
                        "Проверь требования заметки и задай минимальный набор "
                        "контекстных вопросов участнику B. Если без одного ответа "
                        "нельзя уверенно продолжить — отметь его blocking."
                    ),
                },
                timeout=60.0,
            )
            analysis_id = str(started_analysis.get("id") or "")
            if not analysis_id:
                raise RuntimeError("analysis start returned no id")
            analysis_result = _poll_analysis(
                owner,
                base,
                workspace_id=workspace_id,
                analysis_id=analysis_id,
                timeout_seconds=timeout_seconds,
            )
            if analysis_result.get("status") != "completed":
                raise RuntimeError(
                    "strong analysis failed: " + str(analysis_result.get("error_code") or "")
                )

            inbox = _request(
                participant,
                public_base,
                "GET",
                "/api/collaboration/questions/inbox?"
                + urllib.parse.urlencode({"workspace_id": workspace_id, "limit": 50}),
            ).get("items") or []
            questions = [
                item
                for item in inbox
                if isinstance(item, dict)
                and item.get("source_kind") == "analysis"
                and item.get("analysis_id") == analysis_id
                and item.get("state") in {"open", "deferred"}
            ]
            if not questions:
                raise RuntimeError("analysis produced no addressed questions")
            responses = [
                {
                    "question_id": str(item["id"]),
                    "disposition": "answer",
                    "body": (
                        "Для acceptance сохраняем текущий scope: один personal timeline, "
                        "inline widgets и максимум одна отображаемая доска."
                    ),
                }
                for item in questions
            ]
            answered = _request(
                participant,
                public_base,
                "POST",
                f"/api/collaboration/analyses/{analysis_id}/answers",
                {
                    "workspace_id": workspace_id,
                    "command_id": f"canary.answers.{run_id}",
                    "responses": responses,
                },
            )
            questions_answered = len(responses)
            continuation_job_id = str(answered.get("continuation_job_id") or "")
            if answered.get("continuation") != "queued" or not continuation_job_id:
                raise RuntimeError(
                    "answered analysis did not return a durable queued continuation"
                )

            # Close over no client state: the backend worker alone owns progression.
            job_result = _poll_job(
                owner,
                base,
                workspace_id=workspace_id,
                job_id=continuation_job_id,
                timeout_seconds=timeout_seconds,
            )
            if job_result.get("status") != "completed":
                raise RuntimeError(
                    "durable continuation failed: " + str(job_result.get("error_code") or "")
                )

        brief = _request(
            owner,
            base,
            "GET",
            "/api/collaboration/brief?"
            + urllib.parse.urlencode({"workspace_id": workspace_id}),
        )
        personal_objects = {
            str(item.get("object_id") or "")
            for item in brief.get("personal") or []
            if isinstance(item, dict)
        }
        if reply_id not in personal_objects:
            raise RuntimeError("A personal brief did not surface B's addressed reply")
        if with_analysis and analysis_id not in personal_objects:
            # continuation_completed uses analysis id as its object id.
            raise RuntimeError("A personal brief did not surface completed continuation")

        repository = note.get("repository") or {}
        return {
            "ok": True,
            "release_sha": str(health.get("release_sha") or ""),
            "note_id": note_id,
            "note_status": note.get("status"),
            "note_audience": note.get("audience"),
            "note_processing_model": (note.get("processing") or {}).get("model"),
            "repository_full_name": repository.get("full_name"),
            "repository_path": repository.get("path"),
            "repository_sha": repository.get("sha"),
            "repository_commit_sha": repository.get("commit_sha"),
            "repository_readback_verified": True,
            "participant_b_no_external_identity": True,
            "participant_b_project_count": 1,
            "participant_b_read_note": True,
            "participant_b_reply_id": reply_id,
            "author_a_read_reply": True,
            "unauthorized_actor_http": denied_status,
            "analysis_id": analysis_id or None,
            "analysis_status": analysis_result.get("status") if analysis_result else None,
            "questions_answered": questions_answered,
            "continuation_job_id": continuation_job_id or None,
            "continuation_status": job_result.get("status") if job_result else None,
            "personal_brief_contains_reply": True,
            "personal_brief_contains_continuation": bool(
                not with_analysis or analysis_id in personal_objects
            ),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    finally:
        if actor_c:
            try:
                _cleanup_actor(
                    data_dir,
                    actor_id=actor_c,
                    project_id=None,
                    delete_actor=True,
                )
            except Exception:
                pass
        if actor_b:
            try:
                _cleanup_actor(
                    data_dir,
                    actor_id=actor_b,
                    project_id=project_id or None,
                    delete_actor=False,
                )
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8196")
    parser.add_argument(
        "--public-base",
        default="https://projects-hub.kenigevents.ru",
    )
    parser.add_argument(
        "--data-dir",
        default="/home/dev/.local/state/projects-hub/data",
    )
    parser.add_argument("--project", default="Projects Hub")
    parser.add_argument("--with-analysis", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    result = run(
        base=args.base,
        public_base=args.public_base,
        data_dir=Path(args.data_dir),
        project_name=args.project,
        with_analysis=bool(args.with_analysis),
        timeout_seconds=max(30.0, min(float(args.timeout_seconds), 300.0)),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
