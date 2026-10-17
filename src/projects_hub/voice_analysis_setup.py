"""Owner-session control for the already approved voice source, never discovery."""
from __future__ import annotations

import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from .store import StoreError


class VoiceSourceSetup:
    def __init__(self, bridge):
        self.bridge = bridge
        self.store = bridge.store
        self.github = bridge.collaboration.github

    def current(self, actor):
        config = self.bridge.config
        if not config or not config.get('owner_id'):
            raise StoreError('VOICE_SETUP_UNAVAILABLE', 'Voice setup is not configured')
        if actor != config['owner_id']:
            raise StoreError('PROJECT_FORBIDDEN', 'Voice setup is owner-only')
        workspace = self.bridge._access()
        self.store.require_workspace_owner(actor, workspace)
        repo = self.store.get_repository_connection(actor, workspace, int(config['source_repository_id']))
        if (repo.get('full_name') != config['source_repository']
                or repo.get('state') != 'available' or repo.get('installation_state') != 'active'
                or repo.get('project_id') not in (None, config['project_id'])):
            raise StoreError('VOICE_SETUP_SOURCE_CONFLICT', 'Configured source is unavailable')
        prefix = config['source_prefix'].rstrip('/')
        if self.store._normalize_allowed_paths([prefix]) != [prefix]:
            raise StoreError('VOICE_SETUP_SOURCE_CONFLICT', 'Source path is invalid')
        exact = (repo.get('project_id') == config['project_id'] and repo.get('role') == 'source_dataset'
                 and repo.get('access_mode') == 'read_only' and repo.get('allowed_paths') == [prefix])
        if not exact and repo.get('role') != 'unassigned':
            raise StoreError('VOICE_SETUP_SOURCE_CONFLICT', 'Existing source binding differs')
        identity = {key:repo.get(key) for key in ('repository_id','full_name','installation_id','project_id',
            'role','access_mode','allowed_paths','state','installation_state')}
        identity['_binding_config'] = {key:config.get(key) for key in ('owner_id','project_id','client_id',
            'source_repository_id','source_repository','source_prefix','source_branch')}
        return workspace, identity, exact

    def status(self, actor):
        try:
            _, _, exact = self.current(actor)
        except StoreError as error:
            if error.code == 'PROJECT_FORBIDDEN':
                raise
            return dict(state='unavailable',can_connect_source=False,reason=error.code)
        return dict(state='ready' if exact else 'source_binding_required', can_connect_source=not exact,
            source=dict(repository=self.bridge.config['source_repository'],prefix=self.bridge.config['source_prefix'],
                        access_mode='read_only'))

    async def connect(self, actor):
        workspace, before, exact = self.current(actor)
        if exact:
            return self.status(actor)
        try:
            audience = await asyncio.wait_for(self.github.repository_audience(actor_id=actor,
                workspace_id=workspace,repository_id=before['repository_id'],_allow_unassigned=True),timeout=8)
        except (TimeoutError, StoreError):
            raise StoreError('VOICE_SETUP_AUDIENCE_UNAVAILABLE','Source audience could not be verified') from None
        if audience.get('status') != 'verified' or audience.get('owner_only') is not True:
            raise StoreError('VOICE_SETUP_AUDIENCE_UNAVAILABLE','Source audience is not verified owner-only')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                workspace, current, _ = self.current(actor)
                if current != before:
                    raise StoreError('VOICE_SETUP_CHANGED','Binding changed during verification')
                self.github.bind_repository(actor_id=actor, workspace_id=workspace,
                    repository_id=before['repository_id'], project_id=before['_binding_config']['project_id'],
                    role='source_dataset', access_mode='read_only', allowed_paths=[before['_binding_config']['source_prefix'].rstrip('/')])
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise
        return self.status(actor)


def attach_voice_setup_routes(app, bridge, actor_id_from_request, public_origin):
    setup = VoiceSourceSetup(bridge)

    @app.get('/api/voice-analysis/setup')
    async def read_setup(request: Request):
        actor = actor_id_from_request(request)
        try:
            return setup.status(actor)
        except StoreError as error:
            return JSONResponse(dict(error=dict(code=error.code)),status_code=403)

    @app.post('/api/voice-analysis/setup/connect-source')
    async def connect_source(request: Request):
        actor = actor_id_from_request(request)
        if request.headers.get('origin') and request.headers['origin'].rstrip('/') != public_origin.rstrip('/'):
            return JSONResponse(dict(error=dict(code='PROJECT_FORBIDDEN')),status_code=403)
        # No caller-provided owner, project, repository, permission or path.
        async for chunk in request.stream():
            if chunk:
                return JSONResponse(dict(error=dict(code='INVALID_ARGUMENT')),status_code=400)
        try:
            return await setup.connect(actor)
        except StoreError as error:
            status = 403 if error.code == 'PROJECT_FORBIDDEN' else 409
            return JSONResponse(dict(error=dict(code=error.code)),status_code=status)
