"""Does the configured `SERPER_API_KEY` work? One real search through the same client the `web_search` tool uses.

    make web-search-check        # spends ONE serper.dev credit; not part of `make test`

Run it after setting or rotating the key, and after the `WebSearchProviderRefusing` alert fires: the alert says the provider is refusing,
this says whether the fix took. It goes through `app/adapters/web/serper.py` (not a hand-built request), so a pass means the tool's own path works,
including the header, the response parsing and the result cap. It prints the outcome and the first result, never the key.

Exit 0 = results came back, 1 = the provider or the network refused or failed it, 2 = no key is configured.
"""
import asyncio
import sys

from app import bootstrap
from app.adapters.web import serper

QUERY = "serper.dev google search api"


async def main() -> int:
    bootstrap.wire()
    try:
        hits = await serper.search(QUERY)
    except serper.SearchNotConfigured:
        print("SERPER_API_KEY is not set (in .env or the environment): nothing was sent.")
        return 2
    except serper.SearchFailed as exc:
        # The message is the one the model would be given: it names no provider detail. The provider's own text is in the log line above it.
        print(f"FAILED: {exc}")
        return 1
    if not hits:
        print("The search ran (the key works) but returned no results.")
        return 0
    print(f"OK: {len(hits)} result(s). First: {hits[0].title} - {hits[0].url}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
