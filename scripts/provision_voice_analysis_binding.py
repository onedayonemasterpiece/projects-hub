#!/usr/bin/env python3
"""Provision the explicitly approved private voice bridge, never start services.

Dry-run is default. No credential values are accepted on the command line or
printed. Existing configurations are never read, replaced or rotated. The intake
file must remain unmounted until source repository audience has been verified.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import secrets
import stat

ENDPOINT = 'https://projects-hub.kenigevents.ru/api/integrations/voice-analysis/intents'
KEY = b'PROJECTS_HUB_VOICE_ANALYSIS_CONFIG_FILE'


class ProvisionError(Exception):
    """Only a fixed safe reason code is exposed to the operator."""


def private_parent(path: Path) -> int:
    if not path.is_absolute() or '..' in path.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)):
        raise ProvisionError('invalid_path')
    # Walk with directory descriptors: no symlink resolution or path race.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        meta = os.fstat(fd)
        if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o700:
            raise ProvisionError('private_parent_required')
        return fd
    except BaseException:
        os.close(fd)
        raise


def absent(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ProvisionError('existing_configuration_refused')


def read_environment(fd, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        meta = os.fstat(descriptor)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1 or meta.st_size > 131072):
            raise ProvisionError('private_service_environment_required')
        content = os.read(descriptor, 131073)
        if len(content) > 131072:
            raise ProvisionError('service_environment_too_large')
        for line in content.splitlines():
            clean = line.strip()
            if clean.startswith(b'export '):
                clean = clean[7:].lstrip()
            if clean.split(b'=', 1)[0].strip() in (KEY, b'PROJECTS_HUB_VOICE_ANALYSIS_CONFIG'):
                raise ProvisionError('existing_binding_pointer_refused')
        return content, (meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_size)
    finally:
        os.close(descriptor)


def write_new(fd, name, content):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(content)
            stream.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_binding(owner_id, project_id, client_id, source_repository_id, source_repository):
    if (not re.fullmatch(r'usr_[a-f0-9]{32}', owner_id)
            or not re.fullmatch(r'prj_[a-f0-9]{32}', project_id)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,120}', client_id)
            or type(source_repository_id) is not int or not 1 <= source_repository_id < 2**63
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', source_repository)):
        raise ProvisionError('invalid_binding_arguments')


def provision(ph_config: Path, intake_config: Path, service_env: Path, *, owner_id, project_id,
              client_id, source_repository_id, source_repository, apply=False):
    validate_binding(owner_id, project_id, client_id, source_repository_id, source_repository)
    if len({str(p) for p in (ph_config, intake_config, service_env)}) != 3:
        raise ProvisionError('distinct_paths_required')
    descriptors = []
    try:
        descriptors = [private_parent(ph_config)]
        descriptors.append(private_parent(intake_config))
        descriptors.append(private_parent(service_env))
        ph_fd, intake_fd, env_fd = descriptors
        absent(ph_fd, ph_config.name)
        absent(intake_fd, intake_config.name)
        old, identity = read_environment(env_fd, service_env.name)
        if not apply:
            return dict(state='dry_run_ready', creates=2, service_pointer_change=True,
                        intake_mounted=False, services_restarted=False)
        # A second instance cannot enter while the local private transaction runs.
        lock = '.voice-binding-provision.lock'
        write_new(env_fd, lock, b'voice binding provisioning\n')
        temporary = '.voice-binding-service-' + secrets.token_hex(12)
        try:
            absent(ph_fd, ph_config.name)
            absent(intake_fd, intake_config.name)
            current, current_identity = read_environment(env_fd, service_env.name)
            if current != old or identity != current_identity:
                raise ProvisionError('service_environment_changed')
            token = secrets.token_urlsafe(48)
            ph = dict(token=token, owner_id=owner_id, project_id=project_id, client_id=client_id,
                source_repository_id=source_repository_id, source_repository=source_repository,
                source_prefix='inbox/voice', source_branch='main')
            intake = dict(enabled=True, owner_id=owner_id, endpoint=ENDPOINT, token=token)
            for fd, path, config in ((ph_fd, ph_config, ph), (intake_fd, intake_config, intake)):
                write_new(fd, path.name, (json.dumps(config, sort_keys=True)+'\n').encode())
                os.fsync(fd)
            updated = old + (b'' if not old or old.endswith(b'\n') else b'\n')
            updated += KEY + b'="' + str(ph_config).encode() + b'"\n'
            write_new(env_fd, temporary, updated)
            current, current_identity = read_environment(env_fd, service_env.name)
            if current != old or identity != current_identity:
                raise ProvisionError('service_environment_changed')
            os.replace(temporary, service_env.name, src_dir_fd=env_fd, dst_dir_fd=env_fd)
            os.fsync(env_fd)
            # Success preserves all prior environment bytes, and never mounts intake.
            return dict(state='provisioned', creates=2, service_pointer_change=True,
                        intake_mounted=False, services_restarted=False)
        except BaseException:
            # Retain any created credentials after a partial failure. Never rotate,
            # guess success, or silently delete them; operator reconciliation needed.
            raise ProvisionError('partial_or_failed_apply_inspect_without_rotation') from None
        finally:
            try:
                os.unlink(temporary, dir_fd=env_fd)
            except FileNotFoundError:
                pass
            os.unlink(lock, dir_fd=env_fd)
    finally:
        for fd in descriptors:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ph-config', type=Path, required=True)
    parser.add_argument('--intake-config', type=Path, required=True)
    parser.add_argument('--service-env', type=Path, required=True)
    parser.add_argument('--owner-id', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--source-repository-id', type=int, required=True)
    parser.add_argument('--source-repository', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = provision(args.ph_config, args.intake_config, args.service_env, apply=args.apply,
            owner_id=args.owner_id, project_id=args.project_id, client_id=args.client_id,
            source_repository_id=args.source_repository_id, source_repository=args.source_repository)
    except (ProvisionError, OSError) as error:
        code = str(error) if isinstance(error, ProvisionError) else 'filesystem_precondition_failed'
        print(json.dumps(dict(state='refused', reason=code)))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
