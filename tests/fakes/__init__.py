"""In-memory implementations of the ports in `app/ports/`, for tests of the code that CONSUMES a port.

A fake is a real implementation of the Protocol that keeps its state in a dict or a list and does no I/O, bound with `Slot.overriding`. It
replaces the old habit of monkeypatching a module attribute inside the consumer, which made a test depend on how the consumer happened to
import its dependency. A test of an ADAPTER (the SQL it writes, the HTTP it sends) does not use these: it drives the adapter itself, against
a recorded cursor, a transport stub or a real container.

Each fake is checked against the same behavioural expectations as the port's contract in `tests/fakes/test_fakes.py`, so a fake cannot
quietly drift from what the adapter does.
"""
