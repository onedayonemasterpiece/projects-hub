from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import DurableStore, StoreError


ALLOWED_DEVICE_CAPABILITIES = {
    "calendar.create_event",
}

TERMINAL_COMMAND_STATUSES = {
    "applied",
    "rejected",
    "failed",
    "expired",
    "cancelled",
    "outcome_unknown",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DeviceCommandService:
    def __init__(
        self,
        store: DurableStore,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.now = now or time.time

    @staticmethod
    def _clean_capabilities(capabilities: list[str]) -> list[str]:
        clean = sorted(set(str(value).strip() for value in capabilities if str(value).strip()))
        if (
            not clean
            or len(clean) > 16
            or any(value not in ALLOWED_DEVICE_CAPABILITIES for value in clean)
        ):
            raise StoreError(
                "INVALID_ARGUMENT",
                "Requested device capabilities are not allowlisted",
            )
        return clean

    def register_device(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        display_name: str,
        platform: str,
        capabilities: list[str],
    ) -> dict[str, Any]:
        clean_capabilities = self._clean_capabilities(capabilities)
        secret = secrets.token_urlsafe(32)
        session_id = "dvs_" + secrets.token_urlsafe(24)
        device = self.store.register_device(
            actor_id=actor_id,
            workspace_id=workspace_id,
            display_name=display_name,
            platform=platform,
            capabilities=clean_capabilities,
            credential_hash=_sha256_text(secret),
            session_id=session_id,
        )
        return {
            "device": device,
            "device_token": f"{device['id']}.{secret}",
        }

    def authenticate(self, authorization: str | None) -> dict[str, Any]:
        raw = str(authorization or "")
        if not raw.startswith("Device "):
            raise StoreError("DEVICE_UNAUTHENTICATED", "Device credential is required")
        token = raw.removeprefix("Device ").strip()
        if "." not in token or len(token) > 300:
            raise StoreError("DEVICE_UNAUTHENTICATED", "Device credential is invalid")
        device_id, secret = token.split(".", 1)
        if not device_id.startswith("dev_") or len(secret) < 30:
            raise StoreError("DEVICE_UNAUTHENTICATED", "Device credential is invalid")
        row = self.store.device_auth_record(device_id)
        if (
            not row
            or row["state"] != "active"
            or not hmac.compare_digest(
                str(row["credential_hash"]),
                _sha256_text(secret),
            )
        ):
            raise StoreError("DEVICE_UNAUTHENTICATED", "Device credential is invalid")
        self.store.touch_device(
            device_id=device_id,
            session_id=str(row["session_id"]),
        )
        return {
            "device_id": device_id,
            "actor_id": str(row["actor_id"]),
            "workspace_id": str(row["workspace_id"]),
            "session_id": str(row["session_id"]),
            "platform": str(row["platform"]),
            "capabilities": json.loads(row["capabilities_json"] or "[]"),
        }

    def list_devices(
        self,
        *,
        actor_id: str,
        workspace_id: str,
    ) -> list[dict[str, Any]]:
        return self.store.list_devices(actor_id, workspace_id)

    def disable_device(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        device_id: str,
    ) -> None:
        self.store.disable_device(
            actor_id=actor_id,
            workspace_id=workspace_id,
            device_id=device_id,
        )

    @staticmethod
    def _parse_aware_datetime(value: str, field: str) -> datetime:
        raw = str(value or "").strip()
        if not raw or len(raw) > 80:
            raise StoreError("INVALID_ARGUMENT", f"{field} is invalid")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StoreError("INVALID_ARGUMENT", f"{field} must be RFC3339") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise StoreError("INVALID_ARGUMENT", f"{field} must include timezone offset")
        return parsed

    @staticmethod
    def _calendar_payload(args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "").strip()
        if not title or len(title) > 160:
            raise StoreError("INVALID_ARGUMENT", "Calendar title is required")
        starts_at = DeviceCommandService._parse_aware_datetime(
            str(args.get("starts_at") or ""),
            "starts_at",
        )
        ends_at = DeviceCommandService._parse_aware_datetime(
            str(args.get("ends_at") or ""),
            "ends_at",
        )
        if ends_at <= starts_at:
            raise StoreError("INVALID_ARGUMENT", "Calendar end must be after start")
        timezone = str(args.get("timezone") or "").strip()
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise StoreError("INVALID_ARGUMENT", "Calendar timezone is invalid") from exc
        description = str(args.get("description") or "").strip()
        location = str(args.get("location") or "").strip()
        if len(description) > 2000 or len(location) > 500:
            raise StoreError("INVALID_ARGUMENT", "Calendar event text is too large")
        return {
            "title": title,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "timezone": timezone,
            "description": description,
            "location": location,
            "requires_user_confirmation": True,
        }

    def create_calendar_command(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None,
        command_id: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        capability = "calendar.create_event"
        requested_device = str(args.get("device_id") or "").strip() or None
        devices = self.store.list_devices(
            actor_id,
            workspace_id,
            capability=capability,
        )
        if requested_device:
            devices = [
                item
                for item in devices
                if item["id"] == requested_device
            ]
            if not devices:
                raise StoreError(
                    "DEVICE_NOT_FOUND",
                    "Requested Android device is unavailable for calendar",
                )
        elif len(devices) > 1:
            raise StoreError(
                "DEVICE_SELECTION_REQUIRED",
                "More than one Android device can create calendar events",
            )
        if not devices:
            raise StoreError(
                "DEVICE_CAPABILITY_NOT_AVAILABLE",
                "No bound Android device can create calendar events",
            )
        payload = self._calendar_payload(args)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload_sha = hashlib.sha256(encoded).hexdigest()
        return self.store.create_device_command(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            device_id=str(devices[0]["id"]),
            capability=capability,
            payload=payload,
            payload_sha256=payload_sha,
            command_id=command_id,
            expires_at_ms=round((self.now() + 10 * 60) * 1000),
        )

    async def wait_for_terminal(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        command_id: str,
        timeout_seconds: float = 40.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(float(timeout_seconds), 45.0))
        while True:
            command = self.store.get_device_command(
                actor_id=actor_id,
                workspace_id=workspace_id,
                command_id=command_id,
            )
            if command["status"] in TERMINAL_COMMAND_STATUSES:
                return self._tool_result(command)
            if time.monotonic() >= deadline:
                return self._tool_result(command)
            await asyncio.sleep(0.25)

    @staticmethod
    def _tool_result(command: dict[str, Any]) -> dict[str, Any]:
        result = command.get("result")
        response = {
            "command_id": command["id"],
            "device_id": command["device_id"],
            "capability": command["capability"],
            "status": command["status"],
            "expires_at_ms": command["expires_at_ms"],
        }
        if isinstance(result, dict):
            response["result"] = result
        if command["status"] in {"pending", "claimed"}:
            response["pending_device_confirmation"] = True
        if command["status"] == "outcome_unknown":
            response["reconciliation_required"] = True
        return response

    async def next_command(
        self,
        *,
        authorization: str | None,
        wait_ms: int,
    ) -> dict[str, Any]:
        device = self.authenticate(authorization)
        deadline = time.monotonic() + max(0, min(int(wait_ms), 25_000)) / 1000
        while True:
            claim_secret = secrets.token_urlsafe(24)
            command = self.store.claim_next_device_command(
                device_id=device["device_id"],
                session_id=device["session_id"],
                claim_hash=_sha256_text(claim_secret),
            )
            if command:
                return {
                    "command": {
                        "command_id": command["id"],
                        "device_id": command["device_id"],
                        "device_session_id": command["device_session_id"],
                        "project_id": command["project_id"],
                        "capability": command["capability"],
                        "payload": command["payload"],
                        "payload_sha256": command["payload_sha256"],
                        "expires_at_ms": command["expires_at_ms"],
                    },
                    "claim_token": claim_secret,
                }
            if time.monotonic() >= deadline:
                return {"command": None}
            await asyncio.sleep(0.25)

    def receipt(
        self,
        *,
        authorization: str | None,
        command_id: str,
        claim_token: str,
        payload_sha256: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        device = self.authenticate(authorization)
        if len(claim_token) < 20 or len(claim_token) > 200:
            raise StoreError("DEVICE_COMMAND_CLAIM_INVALID", "Device claim token is invalid")
        if not isinstance(result, dict):
            raise StoreError("INVALID_ARGUMENT", "Device command result must be an object")
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 4096:
            raise StoreError("INVALID_ARGUMENT", "Device command result is too large")
        allowed_result_keys = {
            "readback_verified",
            "event_id",
            "provider_status",
            "error_code",
        }
        if any(key not in allowed_result_keys for key in result):
            raise StoreError("INVALID_ARGUMENT", "Device command result contains unknown fields")
        if status == "applied":
            if result.get("readback_verified") is not True:
                raise StoreError(
                    "DEVICE_READBACK_REQUIRED",
                    "Applied calendar event requires device readback",
                )
            event_id = str(result.get("event_id") or "").strip()
            if not event_id or len(event_id) > 200:
                raise StoreError(
                    "DEVICE_READBACK_REQUIRED",
                    "Applied calendar event requires event id readback",
                )
        command = self.store.complete_device_command(
            device_id=device["device_id"],
            session_id=device["session_id"],
            command_id=command_id,
            claim_hash=_sha256_text(claim_token),
            payload_sha256=payload_sha256,
            status=status,
            result=result,
        )
        return self._tool_result(command)
