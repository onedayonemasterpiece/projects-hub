#!/usr/bin/env python3
"""Install the existing tunnel-client HTTP pattern for an isolated tunnel ID.

Run from the deployed immutable Projects Hub release. Credential values never
enter stdout, source, or evidence. Existing product/tunnel units are preserved.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess

CURRENT = Path('/home/dev/.local/share/projects-hub/current')
CONFIG = Path('/home/dev/.config/projects-hub-tunnel')
STATE = Path('/home/dev/.local/state/projects-hub-tunnel')
UNIT = Path('/home/dev/.config/systemd/user/projects-hub-tunnel.service')
BINARY = Path('/home/dev/.local/bin/tunnel-client')
RUNTIME_KEY = Path('/home/dev/.config/tunnel-client/runtime-api-key')
TUNNEL_ID = re.compile(r'tunnel_[a-f0-9]{32}')


def private_write(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    if result.returncode:
        # Never print underlying stdout/stderr: doctor may inspect secret refs.
        raise RuntimeError(f'{Path(argv[0]).name} {argv[1]} failed with exit {result.returncode}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tunnel-id', required=True)
    args = parser.parse_args()
    if not TUNNEL_ID.fullmatch(args.tunnel_id):
        raise ValueError('Invalid tunnel ID')
    source = CURRENT.resolve() / 'source'
    if Path(__file__).resolve() != source / 'scripts/install_mcp_tunnel.py':
        raise RuntimeError('Run this script from the deployed immutable release')
    if not BINARY.is_file() or not RUNTIME_KEY.is_file() or RUNTIME_KEY.stat().st_mode & 0o077:
        raise RuntimeError('Existing tunnel-client and private runtime API key required')
    receipt = CONFIG / 'installation.json'
    if receipt.is_file():
        installed = json.loads(receipt.read_text())
        if installed['tunnel_id'] != args.tunnel_id:
            raise RuntimeError('A different tunnel is already configured; refusing replacement')
        run([str(BINARY), 'doctor', '--config', str(CONFIG/'config.yaml')])
        run(['systemctl', '--user', 'enable', '--now', UNIT.name])
        print(json.dumps({**installed, 'reused': True}))
        return 0
    for path in (CONFIG, STATE):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise RuntimeError('Tunnel directories must be private and not symlinks')
    with socket.socket() as check:
        check.bind(('127.0.0.1', 18082))
    # A failed prior attempt can resume its provisioned private credential.
    credential_file = CONFIG / 'credential.json'
    if not credential_file.exists():
        run([str(source/'scripts/run_mcp_test_request.sh'), 'provision-tunnel',
             '--client-id', 'ph_service_tunnel_projects_hub', '--credential-file', str(credential_file)])
    if credential_file.is_symlink() or credential_file.stat().st_mode & 0o077:
        raise RuntimeError('Private regular tunnel credential required')
    credential = json.loads(credential_file.read_text())
    header_file = CONFIG / 'authorization-header'
    if not header_file.exists():
        private_write(header_file, 'Bearer ' + credential['access_token'])
    elif header_file.is_symlink() or header_file.stat().st_mode & 0o077 or header_file.read_text() != 'Bearer '+credential['access_token']:
        raise RuntimeError('Existing local header differs or is not private')
    template = (source/'deploy/tunnel-client/projects-hub.example.yaml').read_text()
    config = template.replace('tunnel_0123456789abcdef0123456789abcdef', args.tunnel_id)
    config_file = CONFIG / 'config.yaml'
    if not config_file.exists():
        private_write(config_file, config)
    elif config_file.is_symlink() or config_file.read_text() != config:
        raise RuntimeError('Existing tunnel configuration differs; refusing replacement')
    run([str(BINARY), 'doctor', '--config', str(config_file)])
    unit = (source/'deploy/systemd/projects-hub-tunnel.service').read_text()
    if not UNIT.exists():
        private_write(UNIT, unit)
    elif UNIT.is_symlink() or UNIT.read_text() != unit:
        raise RuntimeError('Existing tunnel unit differs; refusing replacement')
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', UNIT.name])
    installed = dict(tunnel_id=args.tunnel_id, service=UNIT.name, upstream='http://127.0.0.1:8196/mcp',
        health_url='http://127.0.0.1:18082', client_id=credential['client_id'], grant_id=credential['grant_id'],
        expires_at_ms=credential['expires_at_ms'], chatgpt_authentication='none',
        release_sha=CURRENT.resolve().name, pattern='mail-mcp-bridge HTTP extra_headers')
    private_write(receipt, json.dumps(installed, indent=2)+'\n')
    print(json.dumps(installed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
