"""Internal capability created solely from an approved server-owned voice binding."""
from __future__ import annotations
import re
from .store import StoreError

_FIELDS = ('owner_id','project_id','client_id','source_repository_id','source_repository','source_prefix','source_branch')


class VoiceSourceCapability:
    def __init__(self, bridge):
        self._bridge = bridge
        self._binding = tuple(bridge.config.get(key) for key in _FIELDS)

    def authorize(self, source):
        bridge = self._bridge
        if tuple(bridge.config.get(key) for key in _FIELDS) != self._binding:
            raise StoreError('VOICE_SOURCE_CONFLICT','Voice binding changed')
        workspace = bridge._access()
        config = bridge.config
        if source is not None:
            path = source.get('path')
            if (source.get('repository') != config['source_repository'] or not isinstance(path,str)
                    or len(path)>500 or path != path.strip() or '%' in path or '\\' in path
                    or any(ord(c)<32 for c in path) or any(p in ('','.','..') for p in path.split('/'))
                    or not path.startswith(config['source_prefix'].rstrip('/')+'/')
                    or not re.fullmatch(r'[a-f0-9]{40}', str(source.get('commit_sha','')))
                    or not re.fullmatch(r'[a-f0-9]{64}', str(source.get('transcript_sha256','')))):
                raise StoreError('VOICE_SOURCE_CONFLICT','Source outside immutable approved namespace')
        store = bridge.store
        connection = store.get_repository_connection(config['owner_id'],workspace,int(config['source_repository_id']))
        if (connection.get('repository_id') != int(config['source_repository_id'])
                or connection.get('full_name') != config['source_repository']
                or connection.get('state') != 'available' or connection.get('installation_state') != 'active'):
            raise StoreError('VOICE_SOURCE_UNAVAILABLE','Configured source unavailable')
        if connection.get('project_id'):
            store.project_access(config['owner_id'],workspace,connection['project_id'])
        installation = store.github_installation(connection['installation_id'])
        return {key:connection.get(key) for key in ('repository_id','full_name','installation_id','project_id',
            'role','access_mode','allowed_paths','state','installation_state')}, {
            key:(installation or {}).get(key) for key in ('installation_id','account_id','account_login','account_type','state')}
