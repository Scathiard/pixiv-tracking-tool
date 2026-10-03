"""Per-user application ownership before tray, configuration, or library startup.

The OS releases the lock after a crash. A tiny loopback rendezvous tells another
launch when the existing server is ready; an old URL is never treated as ready.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time


class ApplicationInstance:
    def __init__(self, directory=None):
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", str(os.getpid()) if os.name == "nt" else str(os.getuid()))
        self.directory = Path(directory) if directory else Path(tempfile.gettempdir()) / ("PixivTracker-" + sha256(user.encode()).hexdigest()[:16])
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = None
        self.socket = None
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.url = None
        self.secret = secrets.token_hex(24)
        self.thread = None

    def acquire(self):
        self.lock = (self.directory / "application.lock").open("a+b")
        self.lock.seek(0, 2)
        if not self.lock.tell():
            self.lock.write(b"0"); self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close(); self.lock = None
            return False
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.bind(("127.0.0.1", 0))
            self.socket.listen(8)
            self.socket.settimeout(.2)
            record = {"port": self.socket.getsockname()[1], "nonce": self.secret}
            target = self.directory / "rendezvous.json"
            temporary = self.directory / ("rendezvous-" + self.secret + ".tmp")
            temporary.write_text(json.dumps(record), encoding="utf-8")
            os.replace(temporary, target)
            self.thread = threading.Thread(target=self._serve, name="application-instance", daemon=True)
            self.thread.start()
            return True
        except BaseException:
            self.close()
            raise

    def _serve(self):
        listener = self.socket
        while not self.closed.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(.5)
                try:
                    request = connection.recv(256).decode("ascii")
                    if not secrets.compare_digest(request, self.secret):
                        continue
                    response = {"state": "ready" if self.ready.is_set() else "starting", "url": self.url}
                    connection.sendall(json.dumps(response).encode("utf-8"))
                except (OSError, UnicodeError):
                    continue

    def mark_ready(self, url):
        if not self.closed.is_set():
            self.url = url
            self.ready.set()

    def existing_url(self, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = json.loads((self.directory / "rendezvous.json").read_text(encoding="utf-8"))
                with socket.create_connection(("127.0.0.1", int(record["port"])), timeout=.5) as connection:
                    connection.sendall(str(record["nonce"]).encode("ascii"))
                    response = json.loads(connection.recv(1024))
                    value = response.get("url")
                    if response.get("state") == "ready" and isinstance(value, str) and value.startswith("http://localhost:"):
                        return value
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self.closed.wait(.15)
        raise RuntimeError("现有程序尚未启动完成，或启动失败；请稍后重新打开")

    def close(self):
        self.closed.set()
        if self.socket is not None:
            self.socket.close(); self.socket = None
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        if self.lock is not None:
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close(); self.lock = None
