"""A TCP forwarder that can refuse a connection or cut one mid-stream.

Placed between an adapter and the local AIMS clone it lets a scenario produce
a real network fault, a refused connection or a socket closed while a result
set is still arriving, without stopping the PostgreSQL service, which would
need an elevated shell. Only loopback addresses are ever bound.
"""

import socket
import threading
from dataclasses import dataclass, field


@dataclass
class CuttingProxy:
    target_host: str
    target_port: int
    #: ``"pass"`` forwards; ``"refuse"`` accepts nothing; ``"cut"`` closes both
    #: sockets once ``cut_after_bytes`` have flowed from the target to the client.
    mode: str = "pass"
    cut_after_bytes: int = 4096
    port: int = 0
    _listener: socket.socket | None = field(default=None, repr=False)
    _threads: list[threading.Thread] = field(default_factory=list, repr=False)
    _stopping: threading.Event = field(default_factory=threading.Event, repr=False)
    cuts: int = 0

    def start(self) -> "CuttingProxy":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = listener.getsockname()[1]
        thread = threading.Thread(target=self._accept_loop, name="cutting-proxy", daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        for thread in self._threads:
            thread.join(timeout=2)

    def refuse(self) -> None:
        """Close the listening socket so the next connect is refused outright."""

        self.mode = "refuse"
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            upstream = socket.create_connection((self.target_host, self.target_port), timeout=5)
            pumps = (
                threading.Thread(target=self._pump, args=(client, upstream, False), daemon=True),
                threading.Thread(target=self._pump, args=(upstream, client, True), daemon=True),
            )
            for pump in pumps:
                pump.start()
                self._threads.append(pump)

    def _pump(self, source: socket.socket, sink: socket.socket, from_target: bool) -> None:
        moved = 0
        try:
            while not self._stopping.is_set():
                chunk = source.recv(65536)
                if not chunk:
                    break
                sink.sendall(chunk)
                moved += len(chunk)
                if from_target and self.mode == "cut" and moved >= self.cut_after_bytes:
                    self.cuts += 1
                    break
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
