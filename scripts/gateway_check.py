"""A real-gateway check that needs no model, key or database: the PINNED LiteLLM image, the shipped dev config
and a stub model backend.

    make gateway-check        # Docker, one throwaway container, about a minute; not part of `make test`

It checks what the app's design rests on and a mock can't show:
1. the shipped dev config loads in the pinned image, and `/model/info` prices every alias in a way the app's
   own parser accepts;
2. a chat call is priced by the gateway: `x-litellm-response-cost` equals the stub's token counts times the
   configured price;
3. NOTHING identifying reaches the model backend, on the Ollama route and an OpenAI-compatible one: not the
   end user (`user`), tenant, principal or conversation. The end user is readable on purpose
   (`gateway.end_user_id`: `tenant:<tenant>/user:<principal>`), which is safe only while the gateway keeps it
   to itself. That is a property of LiteLLM, not of this code, hence the check and why the image is pinned by
   digest; run it whenever the digest is bumped (infra/README.md, "Bumping the LiteLLM image").

Only the model backend is faked (and only the host in each `api_base` rewritten). It can't show budget
enforcement, spend-log or Langfuse fields, which need LiteLLM's database. Exit 0 every check passed, 1 a check
failed, 2 it could not run.
"""
import copy
import json
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO = Path(__file__).resolve().parents[1]
DEV_CONFIG = REPO / "deploy/litellm/litellm-config.yaml"
DEV_COMPOSE = REPO / "deploy/compose/docker-compose.yml"
STUB_PORT, GATEWAY_PORT = 18911, 18912
MASTER = "sk-anything"  # the dev config's own master key, committed
STUB_TOKENS = (10, 20)  # the prompt and completion tokens the stub reports
COMPAT = "chat-openai-compat"  # an extra alias shaped like production's (and Gemini's compatibility endpoint): an OpenAI-compatible route
# Identifying values the check sends and then looks for on the backend's side of the gateway.
TENANT, PRINCIPAL, THREAD = "acme-check", "alice-check@example.com", "thread-gateway-check-7731"


def pinned_image() -> str:
    """The image `make up` runs, exactly as the dev compose file pins it."""
    return yaml.safe_load(DEV_COMPOSE.read_text())["services"]["litellm"]["image"]


def build_config(dev: dict, host: str, port: int) -> dict:
    """The dev config with every model backend pointed at the stub, the Langfuse callbacks (a server of their own) dropped, and one
    OpenAI-compatible alias added. Nothing else is touched: it is the shipped file the gateway reads."""
    config = copy.deepcopy(dev)
    for entry in config["model_list"]:
        entry["litellm_params"]["api_base"] = f"http://{host}:{port}"
    config["model_list"].append(
        {
            "model_name": COMPAT,
            "litellm_params": {"model": "openai/stub-model", "api_base": f"http://{host}:{port}/v1", "api_key": "sk-stub"},
            "model_info": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
        }
    )
    for key in ("success_callback", "failure_callback"):
        config["litellm_settings"].pop(key, None)
    return config


def leaks(seen: dict, needles: list[str]) -> list[str]:
    """Which of `needles` appear anywhere in what the backend received (body or headers)."""
    wire = json.dumps({"body": seen["body"], "headers": seen["headers"]})
    return [needle for needle in needles if needle in wire]


class _Stub(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        type(self).seen.append({"path": self.path, "body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
        prompt, completion = STUB_TOKENS
        if self.path.startswith("/api/chat"):  # Ollama
            reply: dict[str, Any] = {"model": body.get("model"), "created_at": "2026-01-01T00:00:00Z", "message": {"role": "assistant", "content": "hello"}, "done": True, "done_reason": "stop", "prompt_eval_count": prompt, "eval_count": completion}
        else:  # OpenAI-compatible
            reply = {"id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": body.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


def main() -> int:
    docker = shutil.which("docker") or "/usr/local/bin/docker"
    if not Path(docker).exists():
        print("error: Docker is needed (one throwaway container of the pinned LiteLLM image)", file=sys.stderr)
        return 2
    from app.agent.cost.pricing import parse_model_info
    from app.core import gateway

    dev = yaml.safe_load(DEV_CONFIG.read_text())
    prices = {e["model_name"]: e["model_info"] for e in dev["model_list"]} | {COMPAT: {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}}
    # The container reaches the stub through the host. Docker Desktop answers `host.docker.internal` itself; `--add-host` makes Linux do the same.
    # The stub therefore listens on every interface, which is harmless here: it answers one canned reply and records what it was sent.
    server = ThreadingHTTPServer(("0.0.0.0", STUB_PORT), _Stub)  # noqa: S104 - see above
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = f"gateway-check-{STUB_PORT}"
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        config_path = Path(scratch) / "config.yaml"
        config_path.write_text(yaml.safe_dump(build_config(dev, "host.docker.internal", STUB_PORT), sort_keys=False))
        config_path.chmod(0o644)  # the container's user must be able to read it, and the directory (mkdtemp is 0700, which a Linux bind mount honours)
        Path(scratch).chmod(0o755)
        subprocess.run([docker, "rm", "-f", name], capture_output=True)
        started = subprocess.run(
            [docker, "run", "-d", "--rm", "--name", name, "--add-host", "host.docker.internal:host-gateway", "-p", f"127.0.0.1:{GATEWAY_PORT}:4000",
             "-v", f"{config_path}:/app/config.yaml:ro", "-e", f"LITELLM_MASTER_KEY={MASTER}", pinned_image(), "--config", "/app/config.yaml", "--port", "4000"],
            capture_output=True, text=True,
        )
        if started.returncode != 0:
            print(f"error: could not start the gateway image: {started.stderr.strip()[:300]}", file=sys.stderr)
            return 2
        base, auth = f"http://127.0.0.1:{GATEWAY_PORT}", {"Authorization": f"Bearer {MASTER}"}
        try:
            for _ in range(120):
                try:
                    if httpx.get(f"{base}/health/liveliness", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(1)
            else:
                logs = subprocess.run([docker, "logs", "--tail", "30", name], capture_output=True, text=True)
                print("error: the gateway did not come up; its last log lines:\n" + (logs.stdout + logs.stderr)[-2500:], file=sys.stderr)
                return 2

            # 1. every alias priced, as the app reads it
            info = httpx.get(f"{base}/model/info", headers=auth, timeout=20).json()["data"]
            parsed = parse_model_info(info)
            for entry in info:
                alias = entry["model_name"]
                print(f"{alias:<19} {'PRICED' if parsed[alias] else 'UNPRICED'}")
                if parsed[alias] is None:
                    failures.append(f"{alias} is unpriced in /model/info")

            # 2 + 3. a call carrying exactly what the app sends, to each route
            ctx = {"tenant": TENANT, "principal": PRINCIPAL, "claims": {}}
            identity = gateway.identity_from_config({"configurable": {"ctx": ctx, "thread_id": THREAD}})
            wire_extras = {"user": identity["user"], **identity["extra_body"]}  # what the OpenAI SDK puts at the top of the request body
            needles = [identity["user"], TENANT, PRINCIPAL, THREAD, "litellm_session_id", "session_id", "metadata"]
            for alias in ("chat", "chat-medium", "chat-strong", COMPAT):
                _Stub.seen.clear()
                response = httpx.post(f"{base}/v1/chat/completions", headers=auth, json={"model": alias, "messages": [{"role": "user", "content": "hi"}], **wire_extras}, timeout=60)
                if response.status_code != 200 or not _Stub.seen:
                    failures.append(f"{alias}: the call failed ({response.status_code}) {response.text[:160]}")
                    continue
                backend = _Stub.seen[-1]
                found = leaks(backend, needles)
                expected = STUB_TOKENS[0] * prices[alias]["input_cost_per_token"] + STUB_TOKENS[1] * prices[alias]["output_cost_per_token"]
                cost = response.headers.get("x-litellm-response-cost")
                print(f"{alias:<19} {backend['path']:<22} cost={cost} (expected {expected:.3g})  reached the backend: {found or 'nothing identifying'}")
                failures += [f"{alias}: the backend received {needle!r}" for needle in found]
                if "user" in backend["body"]:
                    failures.append(f"{alias}: the backend received a `user` field")
                if cost is None or not math.isclose(float(cost), expected, rel_tol=1e-6):
                    failures.append(f"{alias}: the gateway priced the call at {cost!r}, expected {expected}")
        finally:
            subprocess.run([docker, "rm", "-f", name], capture_output=True)
            server.shutdown()
    print("\nFAILED:" if failures else "\nall checks passed")
    for failure in failures:
        print(f"  {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
