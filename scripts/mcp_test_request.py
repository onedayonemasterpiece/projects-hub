#!/usr/bin/env python3
"""Trusted on-host P0 command. Never print the request nonce or any token."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from projects_hub.mcp_auth import MCPAuth, REDIRECT
from projects_hub.mcp_events import MCPEvents
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    create = commands.add_parser('create')
    create.add_argument('--project-id', required=True)
    create.add_argument('--command-id', required=True, help='Reuse this id after a lost response')
    create.add_argument('--ttl-seconds', type=int, default=900)
    create.add_argument('--context-actor-id', help='Existing project participant whose context dot should read; defaults to the operator')
    status = commands.add_parser('status')
    status.add_argument('--request-id', required=True)
    commands.add_parser('subscriptions')
    provision = commands.add_parser('provision-service', help='Create one service client; write its secret to a private file, never stdout')
    provision.add_argument('--client-id', required=True)
    provision.add_argument('--redirect-uri', action='append', help='Exact official ChatGPT callback, defaults to stable callback')
    provision.add_argument('--credential-file', type=Path, required=True, help='New file in an existing private directory')
    tunnel = commands.add_parser('provision-tunnel', help='Write a private local service bearer for tunnel-client; never stdout')
    tunnel.add_argument('--client-id', required=True)
    tunnel.add_argument('--credential-file', type=Path, required=True)
    disable = commands.add_parser('disable-service', help='Disable a client and all its MCP grants')
    disable.add_argument('--client-id', required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    store = DurableStore(settings.data_dir)
    try:
        # Read the designated owner; do not create an identity or grant here.
        owner = store.db.execute('SELECT actor_id FROM platform_owner WHERE slot=1').fetchone()
        if not owner:
            raise StoreError('OWNER_UNAVAILABLE', 'Existing platform owner required')
        auth = MCPAuth(store, settings.public_origin)
        events = MCPEvents(store, auth, settings.session_secret, release_sha=settings.release_sha)
        if args.operation == 'create':
            result = events.create_request(owner['actor_id'], args.project_id, args.command_id, args.ttl_seconds, context_actor_id=args.context_actor_id)
        elif args.operation == 'status':
            result = events.status(owner['actor_id'], args.request_id)
        elif args.operation in ('provision-service', 'provision-tunnel'):
            # Create exclusively before changing the DB: a retry cannot silently
            # rotate a configured secret or overwrite an existing private file.
            fd = os.open(args.credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                if args.operation == 'provision-tunnel':
                    credentials = auth.provision_tunnel(owner['actor_id'], args.client_id)
                else:
                    credentials = auth.provision_service(owner['actor_id'], args.client_id, args.redirect_uri or [REDIRECT])
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    fd = None
                    json.dump({**credentials, 'mcp_url': auth.resource,
                               'authorization_url': auth.origin+'/mcp/oauth/authorize',
                               'token_url': auth.origin+'/mcp/oauth/token', 'scope': 'projects-hub:test'}, handle, indent=2)
                    handle.write('\n')
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                if fd is not None:
                    os.close(fd)
                if 'credentials' in locals():
                    auth.disable_service(owner['actor_id'], args.client_id)
                raise
            result = {'client_id': args.client_id, 'credential_file': str(args.credential_file),
                      'principal_type': 'service', 'project_scope': 'all_active_projects', 'scope': 'projects-hub:test'}
            if args.operation == 'provision-tunnel':
                result.update(grant_id=credentials['grant_id'], expires_at_ms=credentials['expires_at_ms'])
        elif args.operation == 'disable-service':
            auth.disable_service(owner['actor_id'], args.client_id)
            result = {'client_id': args.client_id, 'enabled': False}
        else:
            store.require_platform_owner(owner['actor_id'])
            rows = store.db.execute('''SELECT s.id,s.project_id,s.state,s.verified_ms,s.expires_ms,
                g.revoked_ms FROM mcp_subscriptions s JOIN mcp_grants g ON g.id=s.grant_id''').fetchall()
            result = {'subscriptions': [dict(row) for row in rows]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StoreError as exc:
        print(json.dumps({'error': exc.code, 'message': str(exc)}, ensure_ascii=False))
        return 1
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
