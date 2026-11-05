"""Every adapter, in two halves (`ARCHITECTURE.md`).

`inbound/` is the DRIVING side (primary): the outside calls the application through it. `outbound/` is the DRIVEN side (secondary): the
application calls out through it, each package implementing a port from `app/ports/`. An adapter may import anything inward of it; nothing
inward imports an adapter, and `app/bootstrap.py` is the one place that binds an outbound adapter to its port.
"""
