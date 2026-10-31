"""Driven adapters: the code that implements a port (`app/ports/`) against a concrete system.

Grouped by what they talk to (`postgres`, `litellm`, ...). An adapter may import anything inward of it; nothing inward imports an adapter,
and `app/bootstrap.py` is the one place that binds one to its port (`ARCHITECTURE.md`).
"""
