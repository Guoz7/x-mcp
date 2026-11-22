"""Reentrant process and file lock for the shared browser ledger."""

import os
import threading
import time


class LedgerLock:
    def __init__(self, path, *, on_timeout=None, timeout_seconds=10):
        self.path = path
        self.on_timeout = on_timeout
        self.timeout_seconds = timeout_seconds
        self.local = threading.RLock()
        self.depth = 0
        self.file = None

    def __enter__(self):
        self.local.acquire()
        try:
            if self.depth == 0:
                self.file = open(self.path, "a+b")
                if os.fstat(self.file.fileno()).st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                deadline = time.monotonic() + self.timeout_seconds
                while True:
                    try:
                        self.file.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            if self.on_timeout is not None:
                                raise self.on_timeout()
                            raise TimeoutError("Browser ledger is busy")
                        time.sleep(0.05)
            self.depth += 1
            return self
        except BaseException:
            if self.file is not None and self.depth == 0:
                self.file.close()
                self.file = None
            self.local.release()
            raise

    def __exit__(self, *exc):
        try:
            self.depth -= 1
            if self.depth == 0:
                try:
                    self.file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                finally:
                    self.file.close()
                    self.file = None
        finally:
            self.local.release()
