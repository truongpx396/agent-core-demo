"""The driving side: something outside calls the application. Leaves: nothing else in `app/` imports them.

`api/` the chat, ingest and health HTTP API. `channels/` the CLI and Telegram, clients of that API. `mcp/` the published MCP servers and their OAuth
gateway. `workers/` the queue consumers (agent turns, ingestion).
"""
