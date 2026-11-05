"""The application's ports: what the agent and its domains need from the world, stated as `Protocol`s and plain data.

`outbound/` holds every port: each is DRIVEN, a thing the application calls, implemented by a package under `app/adapters/outbound/`
(the layout mirrors it). The driving side has no ports: the inbound adapters call the application's own functions (`app/agent/runtime/`),
because nothing needs to swap the application for a fake. If one ever does, its Protocols go in `inbound/` here. `slot.py` is shared:
the typed seam every port uses.

Each port module holds the Protocol, the data that crosses it, the errors it raises, and the `Slot` the composition root binds an
adapter to. This package imports the standard library, `pydantic`, the kernel (`app/core`) and itself, and nothing that talks to a
system (`tests/core/test_architecture_layers.py`). See `ARCHITECTURE.md` for the rings, and for how to add a port.
"""
