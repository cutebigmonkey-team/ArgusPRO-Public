"""Local account administration; only salted scrypt hashes are stored."""
import hashlib
import hmac
from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

USERNAME = re.compile(r'[a-z0-9][a-z0-9_.-]{2,63}')

def username(value):
    if not isinstance(value, str) or not USERNAME.fullmatch(value.strip().lower()):
        raise ValueError('Login must contain 3-64 Latin letters, digits, dots, hyphens or underscores')
    return value.strip().lower()

def key_hash(key, salt):
    return hashlib.scrypt(key.encode('utf-8'), salt=salt, n=32768, r=8, p=3,
                          maxmem=64 * 1024 * 1024, dklen=32)

def validate_key(key):
    if not isinstance(key, str) or not 16 <= len(key) <= 128 or not key.strip():
        raise ValueError('Use a personal key of 16-128 characters')

class AuthStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, salt BLOB NOT NULL, key_hash BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL)''')
        if os.name != 'nt':
            self.path.chmod(0o600)
            self.path.parent.chmod(0o700)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, name, key):
        name = username(name)
        validate_key(key)
        salt = secrets.token_bytes(16)
        digest = key_hash(key, salt)
        try:
            with self.connect() as db:
                db.execute('INSERT INTO users(username,salt,key_hash,created_at) VALUES(?,?,?,?)',
                           (name, salt, digest, int(time.time())))
        except sqlite3.IntegrityError:
            raise ValueError('This login already exists') from None

    def reset_key(self, name, key):
        name = username(name)
        validate_key(key)
        salt = secrets.token_bytes(16)
        digest = key_hash(key, salt)
        with self.connect() as db:
            result = db.execute('UPDATE users SET salt=?,key_hash=?,version=version+1 WHERE username=?',
                                (salt, digest, name))
            if result.rowcount != 1:
                raise ValueError('Unknown login')

    def set_enabled(self, name, enabled):
        with self.connect() as db:
            result = db.execute('UPDATE users SET enabled=?,version=version+1 WHERE username=?',
                                (int(enabled), username(name)))
            if result.rowcount != 1:
                raise ValueError('Unknown login')

    def list_users(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT username,enabled,created_at FROM users ORDER BY username')]

    def authenticate(self, name, key):
        if not isinstance(key, str) or len(key) > 128:
            return None
        try:
            name = username(name)
        except ValueError:
            name = ''
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        # Unknown and disabled accounts incur the same expensive verification.
        salt = bytes(row['salt']) if row else b'\0' * 16
        actual = key_hash(key, salt)
        expected = bytes(row['key_hash']) if row else b'\0' * 32
        if hmac.compare_digest(actual, expected) and row and row['enabled']:
            return {'username': row['username'], 'version': row['version']}
        return None

    def active(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT enabled,version FROM users WHERE username=?',
                             (identity['username'],)).fetchone()
        return bool(row and row['enabled'] and row['version'] == identity['version'])
