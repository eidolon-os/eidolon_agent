"""Opt-in local HIL audit. Callers pass public scene facts, never credentials/audio."""
import json
import logging
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

failures = 0

def record(event, *, source="agent", **facts):
    global failures
    directory = os.environ.get("EIDOLON_PARTICIPATION_AUDIT_DIR", "")
    if not directory:
        return
    try:
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("audit directory must be owned and private")
        stamp = datetime.now(timezone.utc)
        data = (json.dumps(dict(schema_version=1, source=source, event=event,
                               timestamp=stamp.isoformat(), monotonic_ns=time.monotonic_ns(),
                               pid=os.getpid(), **facts), ensure_ascii=False, allow_nan=False)+"\n").encode()
        fd = os.open(root / f"{source}-{stamp:%Y%m%d}-{os.getpid()}.jsonl",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("audit file must be owned and private")
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short audit write")
                view = view[n:]
        finally:
            os.close(fd)
    except Exception as error:
        failures += 1
        logging.getLogger(__name__).warning("participation audit write failed type=%s failures=%d", type(error).__name__, failures)
