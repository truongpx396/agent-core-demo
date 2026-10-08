"""A real `stripe listen` forwarding to a socket this process owns, so a test can hold REAL Stripe-signed deliveries.

Nothing in the hermetic suite can show that the adapter verifies what Stripe actually sends: its tests sign with the documented scheme
themselves. This starts the Stripe CLI, which subscribes to the sandbox's events and POSTs each one, signed with the CLI session's
`whsec_` secret, to the receiver (tests/provider_sandbox/receiver.py). The test then hands those exact headers and bytes to
`StripeProvider.parse_webhook`.

The CLI is told which account to use through the `STRIPE_API_KEY` environment variable, not `--api-key`, so the key never appears in the
process list. Its output is redacted before it is ever shown (it contains the signing secret).
"""
import os
import re
import select
import subprocess
import threading
import time

from tests.provider_sandbox.receiver import Delivery, Receiver, redact

__all__ = ["Delivery", "StripeListener", "redact"]

_SECRET = re.compile(r"whsec_\w+")


class StripeListener:
    def __init__(self, api_key: str, events: tuple[str, ...]):
        self._api_key = api_key
        self._events = events
        self._receiver = Receiver()
        self._process: subprocess.Popen[str] | None = None
        self._output: list[str] = []
        self.secret = ""

    def start(self, timeout: float = 45.0) -> str:
        """Begin forwarding; returns once the CLI reports itself ready (and has printed the signing secret)."""
        self._process = subprocess.Popen(
            ["stripe", "listen", "--events", ",".join(self._events), "--forward-to", self._receiver.url],
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
        return self._receiver.deliveries()

    def wait_for(self, matches, *, after: int = 0, timeout: float = 90.0) -> Delivery:
        return self._receiver.wait_for(matches, after=after, timeout=timeout, context=lambda: "CLI said: " + " | ".join(self._output[-5:]))

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._receiver.stop()
