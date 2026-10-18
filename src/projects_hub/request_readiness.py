"""Readiness for one already-authorized request; no discovery or repair API."""
from __future__ import annotations

import asyncio
from .store import StoreError, _now_ms


_FIELDS = ('repository_id', 'full_name', 'private', 'role', 'access_mode', 'allowed_paths',
           'state', 'installation_state', 'default_branch')


def snapshot(events, grant_id, project_id, request_id):
    with events.store._lock:
        row, _ = events._request(grant_id, request_id, project_id)
        if row['mode'] != 'live':
            return None
        binding = events.bindings._binding(grant_id, row)
        owner = binding['owner_actor_id']
        events.auth.project_owner(owner, project_id)
        note = events.bindings._note(owner, project_id, binding['note_id'])
        workspace = note['workspace_id']
        only = not events.store.db.execute(
            'SELECT 1 FROM project_grants WHERE project_id=? AND actor_id!=? AND revoked_at_ms IS NULL',
            (project_id, owner)).fetchone()
        selected = [('request_repository', note['repository_id'])]
        voice = getattr(events, 'voice_analysis', None)
        config = voice.config if voice else {}
        if config.get('owner_id') == owner and config.get('project_id') == project_id and config.get('source_repository_id'):
            selected.append(('configured_voice_source', int(config['source_repository_id'])))
        repositories = []
        installation_ids = []
        for purpose, repo_id in selected:
            if type(repo_id) is not int or repo_id <= 0:
                repositories.append(dict(purpose=purpose, repository_id=None, state='unavailable'))
                continue
            try:
                connection = events.store.get_repository_connection(owner, workspace, repo_id)
                if connection.get('project_id'):
                    events.store.project_access(owner, workspace, connection['project_id'])
                if purpose == 'request_repository' and connection.get('full_name') != note['repository_full_name']:
                    raise StoreError('GITHUB_REPOSITORY_UNAVAILABLE', 'Repository identity changed')
                if purpose == 'configured_voice_source' and connection.get('full_name') != config.get('source_repository'):
                    raise StoreError('GITHUB_REPOSITORY_UNAVAILABLE', 'Repository identity changed')
                installation_ids.append(connection.get('installation_id'))
                repositories.append(dict(purpose=purpose, **{key:connection.get(key) for key in _FIELDS}))
            except StoreError:
                repositories.append(dict(purpose=purpose, repository_id=repo_id, state='unavailable'))
        return dict(owner=owner, workspace=workspace, binding_id=binding['id'],
                    binding_revision=binding['revision'], owner_only=only, repositories=repositories, installation_ids=installation_ids)


async def read_request_with_readiness(events, grant_id, project_id, request_id):
    # Use the existing complete authorization path before any GitHub request.
    data = events.get_project_request(grant_id, project_id, request_id)
    if data.get('mode') != 'live':
        return data
    before = snapshot(events, grant_id, project_id, request_id)
    service = getattr(events, 'analysis_service', None)
    github = getattr(getattr(service, 'collaboration', None), 'github', None)

    async def check(repo):
        if repo.get('state') != 'available' or not hasattr(github, 'repository_audience'):
            return dict(owner_only=None, status='unavailable')
        try:
            voice = getattr(events, 'voice_analysis', None)
            if (repo['purpose']=='configured_voice_source' and voice is not None
                    and hasattr(github,'voice_source_audience')):
                return await github.voice_source_audience(capability=voice.source_capability)
            return await asyncio.wait_for(github.repository_audience(actor_id=before['owner'],workspace_id=before['workspace'],
                                               repository_id=repo['repository_id']), timeout=8)
        except (TimeoutError, StoreError):
            return dict(owner_only=None, status='unavailable')

    audiences = await asyncio.gather(*(check(repo) for repo in before['repositories']))
    # No stale readiness or source content is returned after authorization changes.
    data = events.get_project_request(grant_id, project_id, request_id)
    after = snapshot(events, grant_id, project_id, request_id)
    if before != after:
        data['binding_readiness'] = dict(status='changed_during_check', owner_only=None, repositories=[])
        return data
    data['binding_readiness'] = dict(status='checked', checked_at_ms=_now_ms(),
        owner_only=before['owner_only'],
        repositories=[dict(repo, audience=audience) for repo,audience in zip(before['repositories'],audiences)],
        scope='current_request_binding_only', automatic_changes=False)
    return data
