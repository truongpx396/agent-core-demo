"""Entry point kept stable for the image's `uvicorn main:app`; the service is the `ml_service` package (see its docstring).

The old single-file service lives on in `ml_service/`: batching, the shared ONNX text path, the model registry, and one module per
endpoint. Nothing about the wire contract of /rerank, /prompt-guard or /health changed in the split.
"""
from ml_service.app import create_app

app = create_app()
