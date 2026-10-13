"""What the provider CLIs forward to: a tiny local HTTP receiver that keeps every delivery's exact headers and bytes.

`stripe listen` and `polar listen` each subscribe to a sandbox's events and POST them, signed, to a URL. A test needs the EXACT bytes and
headers (a signature is over them), so this records them untouched and lets a test wait for the one it expects.

It speaks HTTP/1.1 with a `Content-Length: 0` reply: the Polar CLI reuses connections, and a server that closes after every response makes
it report "socket connection was closed unexpectedly" for a delivery that in fact arrived.
"""
import http.server
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

_SECRET = re.compile(r"whsec_\w+")
_KEY = re.compile(r"(sk|rk)_(test|live)_\w+")
_POLAR_TOKEN = re.compile(r"polar_(oat|pat)_\w+")


def redact(text: str, known: Iterable[str] = ()) -> str:
    """The text with every credential-shaped value, and any value in `known` (a secret the CLI printed in a shape no pattern knows),
    replaced. Applied to everything a CLI says before it can reach a failure message."""
    for secret in known:
        if secret:
            text = text.replace(secret, "***")
    return _POLAR_TOKEN.sub(r"polar_\1_***", _KEY.sub(r"\1_\2_***", _SECRET.sub("whsec_***", text)))


@dataclass(frozen=True)
class Delivery:
    headers: dict[str, str]  # lower-cased names
    body: bytes
    received_at: float = field(default_factory=time.time)

    @property
    def event(self) -> dict:
        return json.loads(self.body)


class Receiver:
    def __init__(self) -> None:
        self._deliveries: list[Delivery] = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers["content-length"]))
                with receiver._lock:
                    receiver._deliveries.append(Delivery({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # a stdlib server logs every request to stderr
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def deliveries(self) -> list[Delivery]:
        with self._lock:
            return list(self._deliveries)

    def wait_for(self, matches: Callable[[dict], bool], *, after: int = 0, timeout: float = 90.0, context: Callable[[], str] = lambda: "") -> Delivery:
        """The first delivery (ignoring the first `after` received) whose decoded event satisfies `matches`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for delivery in self.deliveries()[after:]:
                if matches(delivery.event):
                    return delivery
            time.sleep(0.5)
        seen = [d.event.get("type") for d in self.deliveries()[after:]]
        raise TimeoutError(f"no matching delivery within {timeout:.0f}s; saw {seen}; {context()}")

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
