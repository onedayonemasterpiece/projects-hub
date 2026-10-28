from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

import httpx
import jwt


API_BASE = "https://api.github.com"
API_VERSION = "2026-03-10"


class GitHubAppError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class GitHubAppClient:
    def __init__(
        self,
        *,
        app_id: int,
        slug: str,
        private_key: str,
        webhook_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.app_id = int(app_id)
        self.slug = slug
        self.private_key = private_key
        self.webhook_secret = webhook_secret.encode("utf-8")
        self.transport = transport
        self.now = now or time.time
        self._http = httpx.AsyncClient(
            base_url=API_BASE, transport=transport, timeout=15.0, follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def install_url(self, state: str) -> str:
        return (
            f"https://github.com/apps/{quote(self.slug, safe='-')}/installations/new?"
            + urlencode({"state": state})
        )

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        if not signature or not signature.startswith("sha256="):
            return False
        supplied = signature.removeprefix("sha256=").strip().lower()
        if len(supplied) != 64:
            return False
        expected = hmac.new(self.webhook_secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, supplied)

    def _app_jwt(self) -> str:
        now = int(self.now())
        return jwt.encode(
            {
                "iat": now - 60,
                "exp": now + 8 * 60,
                "iss": str(self.app_id),
            },
            self.private_key,
            algorithm="RS256",
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "projects-hub",
        }

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(
                method,
                path,
                headers=self._headers(token),
                json=body,
            )
        except httpx.HTTPError as exc:
            raise GitHubAppError(
                "GITHUB_UNAVAILABLE",
                "GitHub request failed",
            ) from exc
        if response.status_code in {401, 403}:
            raise GitHubAppError(
                "GITHUB_FORBIDDEN",
                "GitHub App authentication or permission was rejected",
                status=response.status_code,
            )
        if response.status_code == 404:
            raise GitHubAppError(
                "GITHUB_NOT_FOUND",
                "GitHub installation or repository is unavailable",
                status=404,
            )
        if response.status_code >= 400:
            raise GitHubAppError(
                "GITHUB_ERROR",
                f"GitHub request failed with HTTP {response.status_code}",
                status=response.status_code,
            )
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub returned invalid JSON",
                status=response.status_code,
            ) from exc

    async def repository_contents(
        self,
        *,
        token: str,
        full_name: str,
        path: str = "",
        ref: str = "",
    ) -> dict[str, Any]:
        owner, separator, repo = str(full_name or "").partition("/")
        if (
            not separator
            or not owner
            or not repo
            or "/" in repo
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._" for ch in owner + repo)
        ):
            raise GitHubAppError("INVALID_ARGUMENT", "Repository name is invalid")
        clean_path = str(path or "").strip().strip("/")
        if clean_path:
            parts = clean_path.split("/")
            if any(part in {"", ".", ".."} for part in parts) or "\\" in clean_path:
                raise GitHubAppError("INVALID_ARGUMENT", "Repository path is invalid")
            if len(clean_path) > 500:
                raise GitHubAppError("INVALID_ARGUMENT", "Repository path is too long")
        endpoint = f"/repos/{quote(owner, safe='-._')}/{quote(repo, safe='-._')}/contents"
        if clean_path:
            endpoint += "/" + "/".join(quote(part, safe="-._") for part in clean_path.split("/"))
        if ref:
            endpoint += "?" + urlencode({"ref": str(ref)})

        payload = await self._json_request("GET", endpoint, token=token)
        if isinstance(payload, list):
            entries = []
            for item in payload[:100]:
                if not isinstance(item, dict):
                    continue
                entries.append({
                    "name": str(item.get("name") or "")[:240],
                    "path": str(item.get("path") or "")[:500],
                    "type": str(item.get("type") or "")[:40],
                    "size": int(item.get("size") or 0),
                })
            return {
                "kind": "directory",
                "path": clean_path,
                "entries": entries,
                "truncated": len(payload) > len(entries),
            }

        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubAppError("GITHUB_INVALID_RESPONSE", "GitHub content response is invalid")
        size = int(payload.get("size") or 0)
        if size > 120_000:
            raise GitHubAppError("GITHUB_ERROR", "Repository file is too large for Live context")
        encoded = str(payload.get("content") or "").replace("\n", "")
        if str(payload.get("encoding") or "") != "base64" or not encoded:
            raise GitHubAppError("GITHUB_INVALID_RESPONSE", "GitHub file content is unavailable")
        try:
            raw = base64.b64decode(encoded, validate=True)
            text = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubAppError("GITHUB_ERROR", "Repository file is not readable text") from exc
        if len(text) > 80_000:
            raise GitHubAppError("GITHUB_ERROR", "Repository file is too large for Live context")
        return {
            "kind": "file",
            "path": clean_path,
            "size": size,
            "sha": str(payload.get("sha") or "")[:80],
            "text": text,
        }

    async def repository_immutable_text(self, *, token: str, full_name: str, path: str, commit_sha: str):
        """Read the exact Git tree/blob, rejecting symlinks including parent paths."""
        import re
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',full_name)
                or not re.fullmatch(r'[a-f0-9]{40}',commit_sha)
                or any(p in ('','.','..') for p in path.split('/')) or '%' in path or '\\' in path
                or len(path.split('/'))>12):
            raise GitHubAppError('INVALID_ARGUMENT','Immutable source reference invalid')
        base=f'/repos/{full_name}/git'
        commit=await self._json_request('GET',base+'/commits/'+commit_sha,token=token)
        if not isinstance(commit,dict) or commit.get('sha')!=commit_sha:
            raise GitHubAppError('GITHUB_INVALID_RESPONSE','Commit identity mismatch')
        tree_sha=(commit.get('tree') or {}).get('sha','')
        parts=path.split('/')
        blob_sha=None
        for index,part in enumerate(parts):
            if not re.fullmatch(r'[a-f0-9]{40}',str(tree_sha)):
                raise GitHubAppError('GITHUB_INVALID_RESPONSE','Tree identity unavailable')
            tree=await self._json_request('GET',base+'/trees/'+tree_sha,token=token)
            if (not isinstance(tree,dict) or tree.get('sha')!=tree_sha or tree.get('truncated') is not False
                    or not isinstance(tree.get('tree'),list)):
                raise GitHubAppError('GITHUB_INVALID_RESPONSE','Complete tree required')
            matches=[item for item in tree['tree'] if isinstance(item,dict) and item.get('path')==part]
            if len(matches)!=1:
                raise GitHubAppError('GITHUB_INVALID_RESPONSE','Source path unavailable')
            entry=matches[0]
            if index<len(parts)-1:
                if entry.get('type')!='tree' or entry.get('mode')!='040000':
                    raise GitHubAppError('GITHUB_INVALID_RESPONSE','Source parent must be a real directory')
                tree_sha=entry.get('sha')
            else:
                if entry.get('type')!='blob' or entry.get('mode') not in ('100644','100755'):
                    raise GitHubAppError('GITHUB_INVALID_RESPONSE','Source must be a regular file')
                blob_sha=entry.get('sha')
        if not re.fullmatch(r'[a-f0-9]{40}',str(blob_sha)):
            raise GitHubAppError('GITHUB_INVALID_RESPONSE','Blob identity unavailable')
        blob=await self._json_request('GET',base+'/blobs/'+blob_sha,token=token)
        if (not isinstance(blob,dict) or blob.get('sha')!=blob_sha or blob.get('encoding')!='base64'
                or type(blob.get('size')) is not int or not 0<blob['size']<=120000):
            raise GitHubAppError('GITHUB_INVALID_RESPONSE','Text blob unavailable')
        try:
            raw=base64.b64decode(str(blob.get('content','')).replace('\n',''),validate=True)
            if len(raw)!=blob['size'] or hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=blob_sha:
                raise ValueError('blob mismatch')
            text=raw.decode('utf-8')
        except (ValueError,UnicodeDecodeError) as exc:
            raise GitHubAppError('GITHUB_INVALID_RESPONSE','Text blob mismatch') from exc
        return dict(kind='file',path=path,sha=blob_sha,commit_sha=commit_sha,text=text,size=len(raw))

    async def put_repository_content(
        self,
        *,
        token: str,
        full_name: str,
        path: str,
        text: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        owner, separator, repo = str(full_name or "").partition("/")
        clean_path = str(path or "").strip().strip("/")
        if (
            not separator
            or not owner
            or not repo
            or "/" in repo
            or not clean_path
            or len(clean_path) > 500
            or "\\" in clean_path
            or any(part in {"", ".", ".."} for part in clean_path.split("/"))
            or any(
                ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._"
                for ch in owner + repo
            )
        ):
            raise GitHubAppError("INVALID_ARGUMENT", "Repository target is invalid")
        clean_message = str(message or "").strip()[:240]
        if not clean_message:
            raise GitHubAppError("INVALID_ARGUMENT", "Commit message is required")
        if len(text.encode("utf-8")) > 120_000:
            raise GitHubAppError("INVALID_ARGUMENT", "Repository text is too large")
        endpoint = (
            f"/repos/{quote(owner, safe='-._')}/{quote(repo, safe='-._')}/contents/"
            + "/".join(quote(part, safe="-._") for part in clean_path.split("/"))
        )
        body: dict[str, Any] = {
            "message": clean_message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": str(branch),
        }
        if sha:
            body["sha"] = str(sha)
        payload = await self._json_request("PUT", endpoint, token=token, body=body)
        if not isinstance(payload, dict):
            raise GitHubAppError("GITHUB_INVALID_RESPONSE", "GitHub write response is invalid")
        content = payload.get("content")
        commit = payload.get("commit")
        if not isinstance(content, dict) or not isinstance(commit, dict):
            raise GitHubAppError("GITHUB_INVALID_RESPONSE", "GitHub write receipt is incomplete")
        return {
            "path": str(content.get("path") or clean_path),
            "sha": str(content.get("sha") or ""),
            "commit_sha": str(commit.get("sha") or ""),
        }

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        payload = await self._json_request(
            "GET",
            f"/app/installations/{int(installation_id)}",
            token=self._app_jwt(),
        )
        if not isinstance(payload, dict) or int(payload.get("id") or 0) != int(installation_id):
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub installation response is invalid",
            )
        if int(payload.get("app_id") or 0) != self.app_id:
            raise GitHubAppError(
                "GITHUB_INSTALLATION_MISMATCH",
                "Installation belongs to another GitHub App",
            )
        return payload

    async def repository_metadata(self, *, token: str, repository_id: int):
        return await self._json_request('GET', f"/repositories/{int(repository_id)}", token=token)

    async def repository_collaborator_page(self, *, token: str, full_name: str, page: int):
        return await self._json_request('GET',
            f"/repos/{full_name}/collaborators?affiliation=all&per_page=100&page={page}", token=token)

    async def installation_token(
        self,
        installation_id: int,
        *,
        repository_ids: list[int] | None = None,
        permissions: Mapping[str, str] | None = None,
    ) -> str:
        body: dict[str, Any] = {}
        if repository_ids is not None:
            ids = [int(item) for item in repository_ids]
            if not ids or len(ids) > 500 or any(item <= 0 for item in ids):
                raise GitHubAppError(
                    "INVALID_ARGUMENT",
                    "repository_ids are invalid",
                )
            body["repository_ids"] = ids
        if permissions is not None:
            allowed = {"read", "write"}
            clean = {
                str(key): str(value)
                for key, value in permissions.items()
                if str(value) in allowed
            }
            if clean != dict(permissions):
                raise GitHubAppError(
                    "INVALID_ARGUMENT",
                    "GitHub permission narrowing is invalid",
                )
            body["permissions"] = clean

        payload = await self._json_request(
            "POST",
            f"/app/installations/{int(installation_id)}/access_tokens",
            token=self._app_jwt(),
            body=body,
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or len(token) < 20:
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub installation token is absent",
            )
        return token

    async def list_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        token = await self.installation_token(
            installation_id,
            permissions={"contents": "read"},
        )
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            payload = await self._json_request(
                "GET",
                f"/installation/repositories?per_page=100&page={page}",
                token=token,
            )
            rows = payload.get("repositories") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise GitHubAppError(
                    "GITHUB_INVALID_RESPONSE",
                    "GitHub repository catalogue is invalid",
                )
            for row in rows:
                if isinstance(row, dict):
                    items.append(row)
            if len(rows) < 100:
                break
            page += 1
        return items

    async def get_repository(
        self,
        installation_id: int,
        repository_id: int,
        *,
        permissions: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        token = await self.installation_token(
            installation_id,
            repository_ids=[repository_id],
            permissions=permissions or {"contents": "read"},
        )
        payload = await self._json_request(
            "GET",
            f"/repositories/{int(repository_id)}",
            token=token,
        )
        if not isinstance(payload, dict) or int(payload.get("id") or 0) != int(repository_id):
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub repository response is invalid",
            )
        return payload
