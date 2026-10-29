"""The application's ports: what the agent and its domains need from the world, stated as `Protocol`s and plain data.

One module per capability. Each holds the Protocol, the data that crosses it, the errors it raises, and the `Slot` the composition
root binds an adapter to. This package imports the standard library, `pydantic`, the kernel (`app/core`) and itself, and nothing that
talks to a system (`tests/core/test_architecture_layers.py`). See `ARCHITECTURE.md` for the rings, and for how to add a port.
"""
