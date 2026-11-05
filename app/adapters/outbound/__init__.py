"""The driven side: the application calls out through a port in `app/ports/`, one package per technology implements it. Never imports `inbound/`.

`postgres`, `qdrant`, `redis` (the answer cache and the Streams queue), `minio`, `litellm`, `mlservice`, `embeddings`, `web`, `crawl4ai`, `notify`, and
`mcp` (the MCP client and the sandbox bridge).
"""
