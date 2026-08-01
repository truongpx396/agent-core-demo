"""A tiny hand-written knowledge base to ingest into Qdrant."""

DOCS = [
    {
        "id": 1,
        "topic": "langgraph",
        "text": "LangGraph models an app as a stateful graph of nodes and edges. "
                "Conditional edges let the agent loop between an LLM node and a tool node.",
    },
    {
        "id": 2,
        "topic": "langgraph",
        "text": "A LangGraph checkpointer (like MemorySaver) persists state per thread_id, "
                "giving the agent conversation memory across turns.",
    },
    {
        "id": 3,
        "topic": "qdrant",
        "text": "Qdrant stores vectors with JSON payloads. You can filter searches by payload "
                "fields, for example restricting results to a single topic.",
    },
    {
        "id": 4,
        "topic": "qdrant",
        "text": "Cosine distance is a common similarity metric for text embeddings in Qdrant.",
    },
    {
        "id": 5,
        "topic": "company",
        "text": "Acme Corp was founded in 2021 and builds offline developer tools. "
                "Its flagship product is a local AI stack starter kit.",
    },
    {
        "id": 6,
        "topic": "company",
        "text": "Acme Corp's support hours are 9am to 5pm on weekdays, and support is free "
                "for all open-source users.",
    },
]
