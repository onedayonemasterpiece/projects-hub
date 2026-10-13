#!/usr/bin/env python3
"""Trusted on-host P0 command. Never print the request nonce or any token."""
from __future__ import annotations

import argparse
import json

from projects_hub.mcp_auth import MCPAuth
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
    status = commands.add_parser('status')
    status.add_argument('--request-id', required=True)
    commands.add_parser('subscriptions')
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
            result = events.create_request(owner['actor_id'], args.project_id, args.command_id, args.ttl_seconds)
        elif args.operation == 'status':
            result = events.status(owner['actor_id'], args.request_id)
        else:
            rows = store.db.execute('''SELECT s.id,s.project_id,s.state,s.verified_ms,s.expires_ms,
                g.revoked_ms FROM mcp_subscriptions s JOIN mcp_grants g ON g.id=s.grant_id
                WHERE g.actor_id=?''', (owner['actor_id'],)).fetchall()
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
