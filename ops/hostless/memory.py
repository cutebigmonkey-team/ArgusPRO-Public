"""Release temporary archive allocations before launching the next browser."""
import ctypes
import gc
from pathlib import Path
import sys
import resource


def measurement():
    """Payload-free process and container readings, including child memory."""
    result={'rss_bytes':resident_bytes(),
            'process_peak_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}
    import os
    statuses={};pending=[os.getpid()];visited=set()
    while pending:
        pid=pending.pop()
        if pid in visited:continue
        visited.add(pid);path=Path('/proc')/str(pid)/'status'
        try:
            fields={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in path.read_text().splitlines() if ':' in line}
            statuses[pid]=int(fields.get('VmRSS','0 kB').split()[0])*1024
            for children in (path.parent/'task').glob('*/children'):
                try:pending.extend(int(value) for value in children.read_text().split())
                except OSError:continue
        except (OSError,ValueError,KeyError):continue
    result['process_tree_bytes']=sum(statuses.values()) or None
    result['child_processes']=max(0,len(statuses)-1)
    for field,filename in [('container_bytes','memory.current'),
                           ('container_peak_bytes','memory.peak'),('container_limit_bytes','memory.max')]:
        try:
            value=(Path('/sys/fs/cgroup')/filename).read_text().strip()
            result[field]=int(value) if value!='max' else None
        except (OSError,ValueError):result[field]=None
    return result


def can_admit(*,budget_bytes=768*1024**2,reserve_bytes=160*1024**2):
    measured=measurement()
    limit=measured.get('container_limit_bytes')
    ceiling=min(budget_bytes,limit) if limit is not None else budget_bytes
    used=measured.get('container_bytes') if limit is not None else measured.get('process_tree_bytes')
    # Missing measurements never masquerade as zero memory use.
    return used is not None and used+reserve_bytes<ceiling


def resident_bytes():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])*1024
    except OSError:
        pass
    return None


def release():
    before=resident_bytes()
    gc.collect()
    if sys.platform.startswith('linux'):
        try:
            trim=ctypes.CDLL(None).malloc_trim
            trim.argtypes=[ctypes.c_size_t]
            trim.restype=ctypes.c_int
            trim(0)
        except (AttributeError,OSError):
            pass
    return {'rss_before':before,'rss_after':resident_bytes()}


if __name__=='__main__':
    # Image-build regression: discarded archive-sized buffers must not remain
    # resident when the same process is about to admit a Chromium subprocess.
    buffers=[bytearray(64*1024) for _ in range(1024)]
    peak=resident_bytes()
    del buffers
    result=release()
    if peak is not None and result['rss_after'] is not None:
        assert peak-result['rss_after']>32*1024**2, 'temporary_memory_not_released'
    print('Temporary memory release verified')
