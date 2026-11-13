"""Fixed startup telemetry. Never serialize exception text or configuration."""
from contextlib import contextmanager
import time


PHASES = {
    'runtime_import': 'runtime_import_failed',
    'cloud_environment': 'cloud_environment_invalid',
    'configuration': 'configuration_invalid',
    'archive_writer_cutover': 'archive_writer_cutover_not_confirmed',
    'archive_access': 'archive_access_failed',
    'registry_load': 'registry_load_failed',
    'database_session': 'database_session_initialization_failed',
    'archive_index_initialization': 'archive_index_initialization_failed',
    'source_configuration': 'source_configuration_failed',
    'runtime_state_initialization': 'runtime_state_initialization_failed',
    'scheduler_initialization':'scheduler_initialization_failed',
    'cursor_restoration':'cursor_restoration_failed',
    'pending_recovery':'pending_recovery_failed',
    'source_registry_reconciliation':'source_registry_reconciliation_failed',
    'comments_initialization':'comments_initialization_failed',
    'comments_synchronization':'comments_synchronization_failed',
    'journal_verification':'journal_verification_failed',
    'pipeline_admission':'pipeline_admission_failed',
    'pipeline_initialization': 'pipeline_initialization_failed',
    'preflight_archive_read': 'archive_read_verification_failed',
}
DATABASE_CODES={'database_session_conflict','database_writer_lock_occupied','database_session_changed',
    'database_writer_lock_lost','database_connection_limit','database_connection_failed',
    'database_tls_verification_failed','database_authentication_failed',
    'database_initialization_failed','database_initialization_retryable','database_startup_cancelled'}
CODES = frozenset(PHASES.values()) | {'configuration_missing'} | DATABASE_CODES
KNOWN = {
    'archive_writer_cutover_not_confirmed': 'archive_writer_cutover_not_confirmed',
    'collector_writer_already_running': 'database_writer_lock_occupied',
    'database_session_changed_use_session_pooling': 'database_session_changed',
}


class StartupFailure(RuntimeError):
    def __init__(self, phase, code):
        if phase not in PHASES or code not in CODES:
            raise ValueError('invalid_startup_classification')
        self.phase, self.code = phase, code
        super().__init__(code)


def known_code(error):
    if isinstance(error, StartupFailure): return error.code
    if isinstance(error, StartupCheckFailed): return error.args[0]
    # Exact constant messages from the pinned journal; no substring inference
    # from driver diagnostics, credentials, URLs or DSNs.
    args = getattr(error, 'args', ())
    return KNOWN.get(args[0]) if len(args) == 1 and type(args[0]) is str else None


def error_type(error):
    for kind in (TimeoutError, ConnectionError, ImportError, ValueError, RuntimeError, OSError):
        if isinstance(error, kind): return kind.__name__
    return 'Exception'


def sqlstate(error):
    value = getattr(error, 'sqlstate', None)
    return value if type(value) is str and value in {'08000','08001','08003','08006','08007','08P01',
                             '28000','28P01','3D000','55P03','57P01','53300','42501','42703','42P01','42601','22P02','23505','23514','25P02','25006'} else None


class Startup:
    def __init__(self, emit, clock=time.monotonic):
        self.emit, self.clock = emit, clock

    @contextmanager
    def phase(self, phase):
        if phase not in PHASES: raise ValueError('invalid_startup_phase')
        started = self.clock()
        self.emit('startup_phase_started', phase=phase)
        try:
            yield
        except Exception as error:
            # Give every started outer phase a terminal event too, retaining
            # the precise original failure phase in a fixed identifier.
            if isinstance(error, StartupFailure):
                self.emit('startup_phase_failed', phase=phase, error_code=error.code,
                          cause_phase=error.phase, error_type=error_type(error),
                          elapsed_ms=round((self.clock()-started)*1000))
                raise
            code = known_code(error) or PHASES[phase]
            self.emit('startup_phase_failed', phase=phase, error_code=code,
                      error_type=error_type(error), sqlstate=sqlstate(error), elapsed_ms=round((self.clock()-started)*1000))
            raise StartupFailure(phase, code) from None
        else:
            self.emit('startup_phase_completed', phase=phase,
                      elapsed_ms=round((self.clock()-started)*1000))


class StartupCheckFailed(Exception):
    """Private validated marker; converted by Startup.phase without raw output."""
    def __init__(self, code):
        if code not in CODES: raise ValueError('invalid_startup_classification')
        super().__init__(code)
