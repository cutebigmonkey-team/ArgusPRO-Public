"""Container-wide cgroup v2 samples, including collector/browser children."""
from pathlib import Path
import time


class Utilization:
    def __init__(self,root=Path('/sys/fs/cgroup')):
        self.root=Path(root);self.previous=None

    def sample(self):
        result={'cpu_percent':None,'memory_bytes':None,'memory_percent':None}
        try:
            used=int((self.root/'memory.current').read_text())
            limit=(self.root/'memory.max').read_text().strip()
            result['memory_bytes']=used
            if limit!='max':result['memory_percent']=round(100*used/int(limit),2)
            stats=dict(line.split() for line in (self.root/'cpu.stat').read_text().splitlines())
            quota,period=(self.root/'cpu.max').read_text().split()
            current=(time.monotonic(),int(stats['usage_usec']))
            if self.previous and quota!='max':
                wall=current[0]-self.previous[0]
                if wall>0:result['cpu_percent']=round(100*(current[1]-self.previous[1])/1e6/wall/(int(quota)/int(period)),2)
            self.previous=current
        except (OSError,ValueError,KeyError,ZeroDivisionError):pass
        return result
