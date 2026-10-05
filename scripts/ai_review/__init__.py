"""The AI PR reviewer (.github/workflows/ai-review.yml). `python -m scripts.ai_review` runs it.

review.py is the orchestrator and the only module that does I/O; findings.py (placing findings on diff
lines), providers.py (the fallback chain) and retry.py (the retry policy) are pure and sit below it.
Nothing is re-exported here on purpose: the tests patch names on `review`, and a re-export would hand
them a copy that the code under test never reads.
"""
