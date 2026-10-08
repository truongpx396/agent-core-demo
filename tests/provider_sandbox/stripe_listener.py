"""A real `stripe listen` forwarding to a socket this process owns, so a test can hold REAL Stripe-signed deliveries.

Nothing in the hermetic suite can show that the adapter verifies what Stripe actually sends: its tests sign with the documented scheme
themselves. This starts the Stripe CLI, which subscribes to the sandbox's events and POSTs each one, signed with the CLI session's
`whsec_` secret, to `http://127.0.0.1:<port>/`. The test then hands those exact headers and bytes to `StripeProvider.parse_webhook`.

The CLI is told which account to use through the `STRIPE_API_KEY` environment variable, not `--api-key`, so the key never appears in the
process list. Its output is redacted before it is ever shown (it contains the signing secret).
"""
import http.server
import json
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field

_SECRET = re.compile(r"whsec_\w+")
_KEY = re.compile(r"(sk|rk)_(test|live)_\w+")


def redact(text: str) -> str:
    return _KEY.sub(r"\1_\2_***", _SECRET.sub("whsec_***", text))


@dataclass(frozen=True)
class Delivery:
    headers: dict[str, str]  # lower-cased names
    body: bytes
    received_at: float = field(default_factory=time.time)

    @property
    def event(self) -> dict:
        return json.loads(self.body)


class StripeListener:
    def __init__(self, api_key: str, events: tuple[str, ...]):
        self._api_key = api_key
        self._events = events
        self._deliveries: list[Delivery] = []
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._server: http.server.HTTPServer | None = None
        self._output: list[str] = []
        self.secret = ""

    def start(self, timeout: float = 45.0) -> str:
        """Begin forwarding; returns once the CLI reports itself ready (and has printed the signing secret)."""
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["content-length"]))
                with listener._lock:
                    listener._deliveries.append(Delivery({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):  # a stdlib server logs every request to stderr
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        port = self._server.server_address[1]
        self._process = subprocess.Popen(
            ["stripe", "listen", "--events", ",".join(self._events), "--forward-to", f"http://127.0.0.1:{port}/"],
            env={**os.environ, "STRIPE_API_KEY": self._api_key},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline and not self.secret:
            if self._process.poll() is not None:
                break
            if select.select([self._process.stdout], [], [], 1)[0]:
                line = self._process.stdout.readline()  # type: ignore[union-attr]  # stdout is a pipe: Popen was given PIPE
                self._output.append(redact(line.strip()))
                match = _SECRET.search(line)
                if match:
                    self.secret = match.group(0)
        if not self.secret:
            self.stop()
            raise RuntimeError("`stripe listen` never became ready; its output: " + " | ".join(self._output[-5:]))
        # Keep draining the pipe so a chatty CLI can never block on a full buffer.
        threading.Thread(target=self._drain, daemon=True).start()
        return self.secret

    def _drain(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._output.append(redact(line.strip()))

    def deliveries(self) -> list[Delivery]:
        with self._lock:
            return list(self._deliveries)

    def wait_for(self, matches, *, after: int = 0, timeout: float = 90.0) -> Delivery:
        """The first delivery (ignoring the first `after` received) whose decoded event satisfies `matches`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for delivery in self.deliveries()[after:]:
                if matches(delivery.event):
                    return delivery
            time.sleep(0.5)
        seen = [d.event.get("type") for d in self.deliveries()[after:]]
        raise TimeoutError(f"no matching delivery within {timeout:.0f}s; saw {seen}; CLI said: " + " | ".join(self._output[-5:]))

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
