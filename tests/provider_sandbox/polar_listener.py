"""A real `polar listen` forwarding to a socket this process owns, and `polar trigger` to make Polar send events through it.

The hermetic Polar tests sign with the documented scheme themselves (their vectors come from the Standard Webhooks library and are
verified by Polar's own SDK). This holds REAL deliveries: `polar listen` subscribes to the sandbox organization's events and POSTs each
one, signed with the session secret it prints, to the receiver (tests/provider_sandbox/receiver.py), and `polar trigger` asks Polar to
send a sample event built from its own schema. The test then hands those exact headers and bytes to `PolarProvider.parse_webhook`.

The secret it prints has no recognizable prefix, so it is redacted by VALUE: every line the CLI says is passed through `redact` with it.
"""
import subprocess
import threading
import time

from tests.provider_sandbox.receiver import Delivery, Receiver, redact

__all__ = ["Delivery", "PolarListener", "redact"]


class PolarListener:
    def __init__(self) -> None:
        self._receiver = Receiver()
        self._process: subprocess.Popen[str] | None = None
        self._output: list[str] = []
        self.secret = ""

    def start(self, timeout: float = 90.0) -> str:
        """Begin forwarding; returns the signing secret once a delivery has really arrived.

        Readiness cannot be read from the CLI's output: piped, it is block-buffered and its "Connected" lines appear only after the first
        event. So the secret comes from `polar listen --print-secret` (the same one the session signs with: it is stable per organization),
        and the session is proven ready by sending a harmless sample until one reaches the receiver."""
        printed = subprocess.run(["polar", "listen", "--print-secret"], capture_output=True, text=True, timeout=60)
        self.secret = printed.stdout.strip()
        if printed.returncode != 0 or not self.secret or len(self.secret.split()) != 1:
            raise RuntimeError("`polar listen --print-secret` gave no secret (is the CLI logged in? `polar auth login`): " + redact((printed.stdout + printed.stderr).strip()[-200:], [self.secret]))
        self._process = subprocess.Popen(
            ["polar", "listen", self._receiver.url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline and not self._receiver.deliveries():
            if self._process.poll() is not None:
                break
            self.trigger("customer.created")
            time.sleep(3)
        if not self._receiver.deliveries():
            output = " | ".join(self._output[-6:])
            self.stop()
            raise RuntimeError("`polar listen` never delivered a sample event; its output: " + output)
        return self.secret

    def _drain(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._output.append(redact(line.strip(), [self.secret]))

    def trigger(self, event: str, *overrides: str, seed: int | None = None, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        """Ask Polar to send a sample `event` to this session. `overrides` are `path=value` pairs (`data.billing_reason=purchase`)."""
        command = ["polar", "trigger", event]
        if seed is not None:
            command += ["--seed", str(seed)]
        for override in overrides:
            command += ["--override", override]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        self._output.append(redact((result.stdout + result.stderr).strip()[-300:], [self.secret]))
        return result

    def deliveries(self) -> list[Delivery]:
        return self._receiver.deliveries()

    def wait_for(self, matches, *, after: int = 0, timeout: float = 90.0) -> Delivery:
        return self._receiver.wait_for(matches, after=after, timeout=timeout, context=lambda: "CLI said: " + " | ".join(self._output[-6:]))

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._receiver.stop()
