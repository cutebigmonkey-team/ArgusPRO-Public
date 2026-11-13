"""Parse provider Retry-After without shortening a requested cooldown."""
import math
import time
from email.utils import parsedate_to_datetime


def retry_after(error, now=None):
    now=time.time() if now is None else now
    for headers in (getattr(error,'resp',None),getattr(error,'headers',None)):
        if not hasattr(headers,'items'):continue
        value=next((v for k,v in headers.items() if str(k).lower()=='retry-after'),None)
        if value is None:continue
        try:
            seconds=float(value)
        except (TypeError,ValueError):
            try:seconds=parsedate_to_datetime(str(value)).timestamp()-now
            except (TypeError,ValueError,OverflowError):continue
        if math.isfinite(seconds):return max(0,seconds)
    return 0
