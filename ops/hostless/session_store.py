"""Pinned journal with proof of advisory ownership and bounded connection retry.

No lock reacquisition during checks, no hidden reconnect, no TLS downgrade, no
DSN rewriting. Transaction pooling is unsupported: use a direct/session endpoint.
"""
import hashlib
import random
import re
import ssl
from startup import StartupCheckFailed, known_code, sqlstate as safe_sqlstate


def error_chain(error):
    seen=set()
    for _ in range(8):
        if error is None or id(error) in seen:return
        seen.add(id(error));yield error
        error=error.__cause__ if error.__cause__ is not None else error.__context__


def database_sqlstate(error):
    """Only structured server/driver diagnostics, never invent a SQLSTATE."""
    for item in error_chain(error):
        for code in (getattr(item,'sqlstate',None),getattr(getattr(item,'diag',None),'sqlstate',None)):
            if type(code) is str and re.fullmatch(r'[0-9A-Z]{5}',code):return code
    return None


def failure_code(error):
    code=database_sqlstate(error)
    if code=='53300':return 'database_connection_limit'
    if code in ('28000','28P01'):return 'database_authentication_failed'
    if type(code) is str and code.startswith('08'):return 'database_connection_failed'
    if code in ('40001','40P01'):return 'database_initialization_retryable'
    if code is not None:return 'database_initialization_failed'
    known=known_code(error)
    if known:return known
    # libpq's startup FATAL path can discard structured diagnostics before
    # psycopg exposes OperationalError. Match ONLY a known server capacity error
    # on an actual psycopg OperationalError. Report SQLSTATE as unknown, not 53300.
    capacity=re.compile(r'(?:^|:)\s*FATAL:\s*(?:too many connections for (?:role|database)\b|'
                        r'(?:sorry, )?too many clients already\b|remaining connection slots are reserved\b)',re.I|re.M)
    for item in error_chain(error):
        driver_error=any(cls.__name__=='OperationalError' and cls.__module__.split('.')[0]=='psycopg'
                         for cls in type(item).__mro__)
        if driver_error and capacity.search(str(item)):return 'database_connection_limit'
    # Driver TLS errors often have no SQLSTATE. Text is examined locally only;
    # never returned, logged, chained or attached to the safe replacement error.
    text=str(error).lower()
    if isinstance(error,ssl.SSLError) or any(marker in text for marker in (
        'certificate verify failed','certificate validation','root certificate',
        'sslrootcert','hostname mismatch','does not match host name',
        'server does not support ssl','ssl is not enabled')):
        return 'database_tls_verification_failed'
    if isinstance(error,(ConnectionError,TimeoutError)) or any(marker in text for marker in (
        'connection refused','connection timed out','timeout expired','could not translate host name',
        'name or service not known','connection reset','server closed the connection')):
        return 'database_connection_failed'
    return 'database_initialization_failed'


def journal_class():
    from argus_collectors.postgres import PostgresJournal
    class OwnedJournal(PostgresJournal):
        def __init__(self,dsn,*,schema):
            self.lock_key=int.from_bytes(hashlib.sha256(schema.encode()).digest()[:8],'big',signed=False)
            super().__init__(dsn,schema=schema)  # same TLS and advisory key
            try:self.check_session()
            except BaseException:
                self.close();raise
        def check_session(self):
            row=self.db.connection.execute('''SELECT pg_backend_pid() AS pid,
                EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                  AND pid=pg_backend_pid() AND granted AND mode='ExclusiveLock'
                  AND classid::bigint=%s AND objid::bigint=%s AND objsubid=1) AS owned''',
                (self.lock_key>>32,self.lock_key & 0xffffffff)).fetchone()
            if row['pid']!=self.backend_pid:raise StartupCheckFailed('database_session_changed')
            if not row['owned']:raise StartupCheckFailed('database_writer_lock_lost')
    return OwnedJournal


def open_session(dsn,schema,emit,wait,*,attempts=4,jitter=random.uniform):
    factory=journal_class()
    retryable={'database_writer_lock_occupied','database_connection_limit',
               'database_connection_failed','database_initialization_retryable'}
    for attempt in range(1,attempts+1):
        try:
            journal=factory(dsn,schema=schema)
            try:emit('database_session_owned',ownership_verified=True)
            except BaseException:
                journal.close();raise
            return journal
        except Exception as error:
            code=failure_code(error)
            failure=StartupCheckFailed(code)
            failure.sqlstate=database_sqlstate(error)
            if code not in retryable or attempt==attempts:
                raise failure from None
            delay=min(30,2**(attempt-1))+jitter(0,1)
            emit('database_session_retry',error_code=code,sqlstate=safe_sqlstate(failure),
                 attempt=attempt,retry_seconds=round(delay,3))
            if wait(delay):raise StartupCheckFailed('database_startup_cancelled') from None
