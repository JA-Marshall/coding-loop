#!/usr/bin/env python3
"""Dedicated synthetic PostgreSQL checks; no production service changes."""
import os
from pathlib import Path
import secrets
import subprocess
import sys

REPO = Path.cwd().resolve()
if not (REPO / 'manage.py').is_file():
    raise RuntimeError('Run from the exact Storehouse checkout')
name = 'runner_batch_' + secrets.token_hex(6)
password = secrets.token_hex(24)
env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
env.update(STOREHOUSE_DEPLOYMENT='test', EBAY_ENVIRONMENT='mocked', DEBUG='1',
           SECRET_KEY=secrets.token_hex(32), DB_CONN_MAX_AGE='0', PYTHONDONTWRITEBYTECODE='1',
           PYTHON_DOTENV_DISABLED='1')

def sql(statement, capture=False):
    result = subprocess.run(['sudo', '-n', '-u', 'postgres', 'psql', '-h', '/var/run/postgresql',
        '-p', '55442', '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-qAt'],
        input=statement, text=True, env=env, cwd='/tmp', timeout=30,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Dedicated test PostgreSQL operation failed; no SQL/credentials printed')
    return result.stdout.strip() if capture else None

created_role = created_db = False
try:
    data_dir = sql('SHOW data_directory;', capture=True)
    if data_dir != '/var/lib/postgresql/14/storehouse-selling':
        raise RuntimeError('Refusing an unexpected PostgreSQL cluster')
    sql(f"CREATE ROLE {name} LOGIN CREATEDB PASSWORD '{password}';")
    created_role = True
    sql(f'CREATE DATABASE {name} OWNER {name};')
    created_db = True
    env['DATABASE_URL'] = f'postgresql://{name}:{password}@127.0.0.1:55442/{name}'
    labels = sys.argv[1:]
    if labels == ['--preflight']:
        args = ['check', '--settings=config.test_settings']
    else:
        if not labels or any(not __import__('re').fullmatch(r'(operations|accounting)(\.[A-Za-z0-9_]+)*', x) for x in labels):
            raise RuntimeError('Only explicit operations/accounting test labels are allowed')
        args = ['test', *labels, '--settings=config.test_settings', '--noinput', '--verbosity=1']
    for command in (['check', '--settings=config.test_settings'], ['makemigrations', '--check', '--dry-run']):
        subprocess.run([str(REPO / '.venv/bin/python'), 'manage.py', *command], cwd=REPO, env=env, timeout=180, check=True)
    result = subprocess.run([str(REPO / '.venv/bin/python'), 'manage.py', *args],
                            cwd=REPO, env=env, timeout=1600)
    raise SystemExit(result.returncode)
finally:
    if created_role:
        sql(f'DROP DATABASE IF EXISTS test_{name} WITH (FORCE);')
    if created_db:
        sql(f'DROP DATABASE IF EXISTS {name} WITH (FORCE);')
    if created_role:
        sql(f'DROP ROLE IF EXISTS {name};')
