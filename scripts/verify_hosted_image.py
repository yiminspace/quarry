#!/usr/bin/env python3
"""Exercise an actual Linux image with disposable auth/SQLite data, never user volumes."""
import argparse
import json
import sys
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--expected-version')
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    name, volume = f'quarry-image-verify-{suffix}', f'quarry-image-verify-{suffix}'

    def docker(*words, **kwargs):
        return subprocess.run(['docker', *words], check=True, text=True, capture_output=True, **kwargs).stdout.strip()

    with tempfile.TemporaryDirectory(prefix='quarry-image-verify-') as directory:
        config = Path(directory)
        config.chmod(0o755)
        workspace = config / 'workspace'
        workspace.mkdir(mode=0o755)
        database = workspace / 'notes.sqlite'
        with sqlite3.connect(database) as connection:
            connection.executescript('CREATE TABLE entries(n INTEGER); INSERT INTO entries VALUES(42);')
        database.chmod(0o644)
        password_hash = docker('run', '--rm', '-i', args.image, 'hash-password', '--stdin', input='disposable-image-password\n')
        resource = {'project': 'example', 'db': 'notes', 'env': 'dev', 'url': 'sqlite:////etc/quarry/workspace/notes.sqlite', 'production': True}
        hosted = {
            'listen': '0.0.0.0:18080', 'state_dir': '/var/lib/quarry',
            'auth': {'mode': 'builtin', 'username': 'owner', 'user_id': 'admin', 'password_hash': password_hash},
            'databases': [{**resource, 'id': 'allowed', 'readers': ['admin']}, {**resource, 'id': 'hidden', 'readers': ['someone-else']}],
        }
        (config / 'hosted.json').write_text(json.dumps(hosted))
        for path in [config / 'hosted.json']:
            path.chmod(0o644)
        try:
            docker('run', '--detach', '--name', name, '--publish', '127.0.0.1::18080',
                   '--mount', f'type=bind,src={config},dst=/etc/quarry,readonly',
                   '--mount', f'type=volume,src={volume},dst=/var/lib/quarry', args.image)
            def published_base():
                # Docker may allocate a new ephemeral host port on every restart.
                binding = docker('port', name, '18080/tcp')
                return 'http://127.0.0.1:' + binding.rsplit(':', 1)[1]

            base = published_base()

            def request(path, value=None, token=None):
                headers = {'Content-Type': 'application/json'}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                request = urllib.request.Request(base + path, data=None if value is None else json.dumps(value).encode(), headers=headers)
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as error:
                    response = error
                raw = response.read()
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw.decode()
                return response.status, value

            def ready():
                deadline = time.monotonic() + 30
                while True:
                    try:
                        status, health = request('/health')
                        assert status == 200 and health['service'] == 'quarry'
                        if args.expected_version:
                            assert health['version'] == args.expected_version
                        return health
                    except (OSError, AssertionError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Linux image did not become ready')
                        time.sleep(.15)

            ready()
            docker('exec', name, 'quarry-server', 'healthcheck')
            status, index = request('/quarry/')
            assert status == 200 and 'quarry-mode' in index
            assert request('/quarry/Cargo.toml')[0] == 404
            status, discovery = request('/api/hosted/config')
            assert status == 200 and discovery['auth']['mode'] == 'builtin' and 'password' not in json.dumps(discovery)
            assert request('/api/db/platform')[0] == 401
            assert request('/api/hosted/auth/login', {'username': 'owner', 'password': 'incorrect'})[0] == 401
            status, session = request('/api/hosted/auth/login', {'username': 'owner', 'password': 'disposable-image-password'})
            assert status == 200
            access = session['access_token']
            status, catalog = request('/api/db/platform', token=access)
            assert status == 200 and [r['id'] for r in catalog['resources']] == ['allowed']
            query = {'resourceId': 'allowed', 'sql': 'SELECT n FROM entries', 'maxRows': 10, 'offset': 0, 'requestId': str(uuid.uuid4())}
            status, result = request('/api/db/query', query, access)
            assert status == 200 and result['rows'] == [{'n': 42}]
            assert request('/api/db/query', {**query, 'resourceId': 'hidden'}, access)[0] == 404
            assert request('/api/db/query', {**query, 'sql': 'DELETE FROM entries'}, access)[0] >= 400
            for path in ['/api/db/personal', '/api/db/local/up']:
                assert request(path, {}, access)[0] >= 400
            status, rotated = request('/api/hosted/auth/refresh', {'refresh_token': session['refresh_token']})
            assert status == 200 and request('/api/db/platform', token=access)[0] == 401
            assert request('/api/hosted/auth/refresh', {'refresh_token': session['refresh_token']})[0] == 401
            status, handoff = request('/api/hosted/auth/handoff', {}, rotated['access_token'])
            assert status == 200
            status, browser = request('/api/hosted/auth/handoff/consume', {'code': handoff['code']})
            assert status == 200 and browser['access_token'] != rotated['access_token']
            assert request('/api/hosted/auth/handoff/consume', {'code': handoff['code']})[0] == 401
            docker('restart', name)
            base = published_base()
            ready()
            docker('exec', name, 'quarry-server', 'healthcheck')
            assert request('/api/db/platform', token=browser['access_token'])[0] == 200
            assert request('/api/hosted/auth/logout', {'refresh_token': rotated['refresh_token']})[0] == 200
            assert request('/api/db/platform', token=rotated['access_token'])[0] == 401
            assert request('/api/db/platform', token=browser['access_token'])[0] == 200
            print('PASS: real Linux image loader, configured-port health, GUI, login, ACL, read-only querying, rotation, handoff, restart persistence and logout')
        except BaseException:
            # Collect startup/health diagnostics before deleting only this run's
            # disposable container. Never inspect unrelated or user containers.
            for command in (
                ['docker', 'inspect', '--format', '{{json .State}} {{json .NetworkSettings.Ports}}', name],
                ['docker', 'logs', '--tail', '100', name],
            ):
                result = subprocess.run(command, text=True, capture_output=True)
                print('Image acceptance diagnostic:', ' '.join(command), file=sys.stderr)
                print(result.stdout + result.stderr, file=sys.stderr)
            raise
        finally:
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True)
            subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)


if __name__ == '__main__':
    main()
