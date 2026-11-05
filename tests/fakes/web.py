"""Fakes for the ports in `app/ports/outbound/web.py`."""


def use_page_renderer(monkeypatch, render) -> None:
    """Binds a renderer whose `render(url)` is the given coroutine function, for the duration of the test."""
    from app.ports.outbound.web import page_renderer

    renderer = type("_Renderer", (), {"render": staticmethod(render)})()
    monkeypatch.setattr(page_renderer, "get", lambda: renderer)
