"""The harness's pure parts must run anywhere (specs/014 FR-012): no network, no model, and none of the heavy frameworks the ML service's
images avoid. Run in a SUBPROCESS so `sys.modules` is the harness's alone, not whatever the rest of the test session already imported."""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

PROBE = """
import importlib, socket, sys

def refuse(*a, **k):
    raise AssertionError("the harness opened a network connection at import time")

socket.socket.connect = refuse
socket.create_connection = refuse
for name in ["metrics", "stats", "suites", "controls", "baselines", "report", "candidates", "hardware", "datasets", "lexical", "public",
             "runners.base", "runners.embedding", "runners.rerank", "runners.screen", "__main__"]:
    importlib.import_module(f"scripts.model_eval.{name}")
heavy = [m for m in ("torch", "transformers", "mteb", "sklearn", "sentence_transformers", "tensorflow") if m in sys.modules]
assert not heavy, f"importing the harness pulled in {heavy}"
# ml_service.onnx_text is imported lazily by the in-process backends only; importing the harness must not need prometheus-client
assert "prometheus_client" not in sys.modules
print("ok")
"""


def test_importing_every_harness_module_opens_no_socket_and_loads_no_ml_framework():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "docker" / "ml-service"), str(REPO / "packages" / "billing" / "src")])}
    done = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
