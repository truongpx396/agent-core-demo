"""One APIRouter per concern, mounted by `app/api/main.py`.

Handlers are plain functions on purpose — the tests call them directly — so a router module is
just where a handler lives. A test that patches a name a handler reads must patch it on THAT
module (`routers.ingest.MAX_UPLOAD_FILES_PER_REQUEST`), not on `main`: a name patched anywhere
else is a copy the handler never looks at.
"""
