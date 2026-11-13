"""Retry transient legacy Drive recovery in place without rebuilding the index."""
import random
from startup import Startup,StartupFailure,known_code,error_type
from retry_after import retry_after


def recover(w,store,journal,feed):
    failures=0
    while not w.STOP_REQUESTED.is_set():
        journal.check_session()
        try:
            with Startup(w.emit).phase('pending_recovery'):
                w.recover_drive(store,journal,feed)
            return True
        except Exception as error:
            journal.check_session()  # no retry/reconnect of a lost session
            cause=error.__context__ if isinstance(error,StartupFailure) else error
            status=getattr(getattr(cause,'resp',None),'status',None)
            transient=isinstance(cause,(TimeoutError,ConnectionError)) or status in (429,500,502,503,504)
            if not transient:raise
            failures+=1
            delay=max(retry_after(cause),min(300,2**min(failures,8)))+random.uniform(0,1)
            w.emit('runtime_recovery_retry',error_code=known_code(error) or 'pending_recovery_failed',
                   error_type=error_type(cause),retry_seconds=round(delay,3))
            if w.STOP_REQUESTED.wait(delay):return False
    return False
