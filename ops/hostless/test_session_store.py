import hashlib
import ssl
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from startup import StartupCheckFailed
from session_store import open_session,failure_code,journal_class


class SessionStoreTests(unittest.TestCase):
    def test_lock_connection_limit_and_connection_errors_have_distinct_codes(self):
        for error,code in [(RuntimeError('collector_writer_already_running'),'database_writer_lock_occupied'),
                           (RuntimeError('database_session_changed_use_session_pooling'),'database_session_changed'),
                           (ConnectionError('private'),'database_connection_failed'),
                           (ssl.SSLError('private'),'database_tls_verification_failed')]:
            self.assertEqual(failure_code(error),code)
        for state,code in [('53300','database_connection_limit'),('28P01','database_authentication_failed'),
                           ('08006','database_connection_failed'),('42501','database_initialization_failed')]:
            error=RuntimeError('secret');error.sqlstate=state
            self.assertEqual(failure_code(error),code)

    def test_actual_driver_sqlstate_diag_and_chain_precede_message_fallback(self):
        from psycopg import errors
        from session_store import database_sqlstate
        error=errors.TooManyConnections('private message')
        wrapped=RuntimeError('outer');wrapped.__cause__=error
        self.assertEqual(database_sqlstate(wrapped),'53300')
        self.assertEqual(failure_code(wrapped),'database_connection_limit')
        diagnostic=RuntimeError('redacted');diagnostic.diag=SimpleNamespace(sqlstate='53300')
        self.assertEqual(failure_code(diagnostic),'database_connection_limit')
        wrong=errors.InvalidPassword('FATAL: too many connections for role "private"')
        self.assertEqual(failure_code(wrong),'database_authentication_failed')
        with patch('session_store.journal_class',return_value=Mock(side_effect=error)):
            with self.assertRaises(StartupCheckFailed) as caught:
                open_session('private','schema',Mock(),Mock(return_value=False),attempts=1)
        self.assertEqual(caught.exception.sqlstate,'53300')
        self.assertEqual(str(caught.exception),'database_connection_limit')

    def test_libpq_startup_fatal_without_sqlstate_is_not_fabricated(self):
        import psycopg
        from session_store import database_sqlstate
        for message in ('connection failed: FATAL: too many connections for role "private"',
                        'connection failed: FATAL: sorry, too many clients already',
                        'connection failed: FATAL: remaining connection slots are reserved for superusers'):
            error=psycopg.OperationalError(message)
            self.assertIsNone(database_sqlstate(error))
            self.assertEqual(failure_code(error),'database_connection_limit')
        self.assertEqual(failure_code(RuntimeError('FATAL: too many connections for role "private"')),
                         'database_initialization_failed')
        with patch('session_store.journal_class',return_value=Mock(side_effect=error)):
            with self.assertRaises(StartupCheckFailed) as caught:
                open_session('private','schema',Mock(),Mock(return_value=False),attempts=1)
        self.assertIsNone(caught.exception.sqlstate)

    def test_transient_startup_recovers_boundedly_without_reinitializing_archive(self):
        journal=Mock();factory=Mock(side_effect=[ConnectionError('secret'),journal]);emit=Mock();wait=Mock(return_value=False)
        with patch('session_store.journal_class',return_value=factory):
            self.assertIs(open_session('private','schema',emit,wait,jitter=lambda a,b:0),journal)
        self.assertEqual(factory.call_count,2);wait.assert_called_once_with(1)
        self.assertEqual(emit.call_args_list[0].kwargs['error_code'],'database_connection_failed')
        self.assertNotIn('private',str(emit.call_args_list))

    def test_retry_exhaustion_and_stop_do_not_steal_existing_lock(self):
        factory=Mock(side_effect=RuntimeError('collector_writer_already_running'))
        with patch('session_store.journal_class',return_value=factory),self.assertRaises(StartupCheckFailed) as caught:
            open_session('private','schema',Mock(),Mock(return_value=False),jitter=lambda a,b:0)
        self.assertEqual(factory.call_count,4);self.assertEqual(str(caught.exception),'database_writer_lock_occupied')
        with patch('session_store.journal_class',return_value=factory),self.assertRaises(StartupCheckFailed) as caught:
            open_session('private','schema',Mock(),Mock(return_value=True))
        self.assertEqual(str(caught.exception),'database_startup_cancelled')

    def test_tls_and_changed_session_are_terminal_without_reconnect(self):
        for error in (ssl.SSLError('private'),RuntimeError('database_session_changed_use_session_pooling')):
            factory=Mock(side_effect=error);wait=Mock()
            with patch('session_store.journal_class',return_value=factory),self.assertRaises(StartupCheckFailed):
                open_session('private','schema',Mock(),wait)
            factory.assert_called_once();wait.assert_not_called()

    def test_session_proof_checks_pid_and_held_lock_in_one_query(self):
        cls=journal_class();journal=object.__new__(cls)
        journal.backend_pid=10;journal.lock_key=int.from_bytes(hashlib.sha256(b'fixture').digest()[:8],'big')
        execute=Mock();journal.db=SimpleNamespace(connection=SimpleNamespace(execute=execute))
        for row,code in [({'pid':10,'owned':False},'database_writer_lock_lost'),
                         ({'pid':11,'owned':True},'database_session_changed')]:
            execute.return_value.fetchone.return_value=row
            with self.assertRaises(StartupCheckFailed) as caught:journal.check_session()
            self.assertEqual(str(caught.exception),code)
        execute.return_value.fetchone.return_value={'pid':10,'owned':True};journal.check_session()
        self.assertIn('pg_locks',execute.call_args.args[0])
        self.assertNotIn('pg_try_advisory_lock',execute.call_args.args[0])

    def test_connection_retry_uses_entrypoints_shutdown_event(self):
        import continuous_runtime
        stop=threading.Event();emit=Mock()
        with patch('session_store.open_session') as open_session:
            continuous_runtime.open_journal('private','schema',emit=emit,stop=stop)
        self.assertIs(open_session.call_args.args[3].__self__,stop)
        self.assertIs(open_session.call_args.args[2],emit)
