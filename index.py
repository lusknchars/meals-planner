#!/usr/bin/env python3
"""Run the pinned official Agent Index client for one local Hermes installation."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
AGENT_ID = 'meals-planner'
REPO = 'https://github.com/lusknchars/meals-planner'
NAME = 'Meals Planner'
BLURB = ('Plans the week of meals inside a calorie target and budget, builds one shopping '
         'list, and prepares an order from places nearby for you to confirm.')


def credentials(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError('credential file must be owned by you with mode 0600 or 0400')
        raw = file.read(16385)
    if len(raw) > 16384:
        raise ValueError('credential file is too large')
    found = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in ('PLOW_API_BASE', 'PLOW_AGENT_TOKEN', 'AGENT_ID') or key in found:
            raise ValueError('unexpected credential format')
        values = shlex.split(value, comments=True)
        if len(values) != 1:
            raise ValueError('unexpected credential value')
        found[key] = values[0]
    if found.get('PLOW_API_BASE') != 'https://api.plow.co' or not found.get('PLOW_AGENT_TOKEN'):
        raise ValueError('expected a Plow agent token for https://api.plow.co')
    return {key: found[key] for key in ('PLOW_API_BASE', 'PLOW_AGENT_TOKEN')}


def client(home):
    pin = dict(line.split('=', 1) for line in (ROOT / 'vendor/client.pin').read_text().splitlines()
               if line and not line.startswith('#'))
    if not re.fullmatch('[0-9a-f]{40}', pin['sha']) or pin['path'] != 'standalone/agent_index_client.py':
        raise ValueError('invalid client pin')
    cache = home / '.meals-index-client'
    cache.mkdir(mode=0o700, exist_ok=True)
    dest = cache / (pin['sha'] + '.py')
    if dest.exists():
        data = dest.read_bytes()
    else:
        url = f"https://raw.githubusercontent.com/plow-pbc/agent-index-client/{pin['sha']}/{pin['path']}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(1000001)
    if hashlib.sha256(data).hexdigest() != pin['sha256']:
        raise ValueError('official client checksum mismatch')
    if not dest.exists():
        fd, name = tempfile.mkstemp(dir=cache)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
            os.replace(name, dest)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return dest


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['register', 'status', 'report', 'dry-run'])
    parser.add_argument('--hermes-home', type=Path, required=True)
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--video', help='public URL of the demonstration video')
    parser.add_argument('--image', action='append', default=[],
                        help='public URL of a screenshot; repeatable')
    args = parser.parse_args(argv)
    try:
        home = args.hermes_home.resolve(strict=True)
        if not (home / 'state.db').is_file():
            raise ValueError('Hermes home must contain the actual state.db')
        env = {'HOME': str(home), 'HERMES_HOME': str(home), 'AGENT_ID': AGENT_ID,
               'PATH': '/usr/bin:/bin:/usr/local/bin', 'PYTHONUNBUFFERED': '1'}
        command = [sys.executable, str(client(home))]
        if args.action == 'register':
            if not args.credentials:
                raise ValueError('register requires --credentials pointing to your private Plow file')
            env.update(credentials(args.credentials))
            command += ['--register', '--agent', AGENT_ID, '--name', NAME,
                        '--blurb', BLURB, '--repo', REPO, '--runtime', 'Hermes',
                        '--install-url', REPO + '#install']
            if args.video:
                command += ['--video', args.video]
            for image in args.image:
                command += ['--image', image]
        elif args.action == 'status':
            command += ['status']
        else:
            # Upstream also detects globally installed agentsview. Refuse mixed
            # attribution rather than publishing unrelated coding-agent usage.
            paths = [home / '.local/bin/agentsview', Path('/opt/homebrew/bin/agentsview'),
                     Path('/usr/local/bin/agentsview')]
            if any(os.access(path, os.X_OK) for path in paths):
                raise ValueError('agentsview is installed; use the isolated image reporter to avoid mixed usage')
            command += ['--agent', AGENT_ID]
            if args.action == 'dry-run':
                command += ['--dry-run']
        return subprocess.run(command, env=env, timeout=180).returncode
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, ValueError) else type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
