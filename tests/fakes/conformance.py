"""Does an implementation match the Protocol it claims to implement?

A Protocol is checked structurally by mypy, but only where the implementation is annotated as the Protocol, and not at all for a fake
bound through `Slot.overriding` in a test. This is the check that does not depend on the annotation: every public method of the Protocol must
exist on the implementation, be a coroutine function exactly when the Protocol's is, and take the same parameter names in the same order (a
caller passes keywords). It is what lets a fake and an adapter be trusted to be interchangeable."""
import inspect


def _public_methods(protocol: type) -> dict[str, object]:
    return {
        name: member
        for name, member in vars(protocol).items()
        if callable(member) and not name.startswith("_")
    }


def _params(function) -> list[str]:
    return [name for name in inspect.signature(function).parameters if name != "self"]


def mismatches(protocol: type, implementation: object) -> list[str]:
    """Human-readable differences between `implementation` and `protocol`; empty when it conforms."""
    problems: list[str] = []
    for name, declared in _public_methods(protocol).items():
        actual = getattr(implementation, name, None)
        if actual is None:
            problems.append(f"{name}: missing")
            continue
        if inspect.iscoroutinefunction(declared) != inspect.iscoroutinefunction(actual):
            problems.append(f"{name}: async-ness differs from the port")
        if _params(declared) != _params(actual):
            problems.append(f"{name}: parameters {_params(actual)} differ from the port's {_params(declared)}")
    return problems
