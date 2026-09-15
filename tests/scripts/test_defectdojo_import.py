"""Tests for scripts/defectdojo_import.py. Hermetic — `httpx.MockTransport`
stands in for a real DefectDojo instance (no live server needed), same
"no live services" discipline the rest of this suite holds to
(tests/conftest.py). The request-shape assertions (auth header, multipart
fields, auto_create_context) are the actual contract verified directly
against a real DefectDojo 3.3.100 instance while building this script — see
that module's own docstring; this test guards against a future edit
silently drifting from that contract, not re-discovering it.
"""
import httpx
import pytest

from scripts import defectdojo_import


def _mock_transport(*, status_code=201, response_json=None, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["request"] = request
        return httpx.Response(status_code, json=response_json or {})

    return httpx.MockTransport(handler)


@pytest.fixture
def report_file(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text("<report/>")
    return path


def test_import_scan_posts_expected_multipart_fields_and_auth_header(monkeypatch, report_file):
    capture = {}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Client(transport=_mock_transport(capture=capture)).post(url, **kwargs),
    )

    defectdojo_import.import_scan(
        base_url="http://localhost:8080",
        api_key="secret-token",
        file_path=report_file,
        scan_type="ZAP Scan",
        product_type_name="agent-core-demo",
        product_name="agent-core-demo",
        engagement_name="ci-123",
    )

    request = capture["request"]
    assert request.url == "http://localhost:8080/api/v2/import-scan/"
    assert request.headers["authorization"] == "Token secret-token"
    body = request.content.decode("utf-8", errors="ignore")
    assert 'name="scan_type"' in body and "ZAP Scan" in body
    assert 'name="auto_create_context"' in body and "True" in body
    assert 'name="product_type_name"' in body and "agent-core-demo" in body
    assert 'name="engagement_name"' in body and "ci-123" in body
    assert 'name="file"; filename="report.xml"' in body


def test_import_scan_returns_parsed_json_on_success(monkeypatch, report_file):
    stats = {"test": 3, "statistics": {"after": {"total": {"total": 15}}}}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Client(
            transport=_mock_transport(status_code=201, response_json=stats)
        ).post(url, **kwargs),
    )

    result = defectdojo_import.import_scan(
        base_url="http://localhost:8080",
        api_key="secret-token",
        file_path=report_file,
        scan_type="ZAP Scan",
        product_type_name="agent-core-demo",
        product_name="agent-core-demo",
        engagement_name="ci-123",
    )
    assert result == stats


def test_import_scan_raises_on_error_status(monkeypatch, report_file):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Client(
            transport=_mock_transport(status_code=400, response_json={"message": "bad request"})
        ).post(url, **kwargs),
    )

    with pytest.raises(httpx.HTTPStatusError):
        defectdojo_import.import_scan(
            base_url="http://localhost:8080",
            api_key="secret-token",
            file_path=report_file,
            scan_type="ZAP Scan",
            product_type_name="agent-core-demo",
            product_name="agent-core-demo",
            engagement_name="ci-123",
        )


def test_main_fails_fast_without_api_key(monkeypatch, report_file, capsys):
    monkeypatch.delenv("DEFECTDOJO_API_KEY", raising=False)
    exit_code = defectdojo_import.main(["--file", str(report_file), "--scan-type", "ZAP Scan"])
    assert exit_code == 1
    assert "DEFECTDOJO_API_KEY" in capsys.readouterr().err


def test_main_fails_fast_on_missing_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DEFECTDOJO_API_KEY", "secret-token")
    missing = tmp_path / "does-not-exist.xml"
    exit_code = defectdojo_import.main(["--file", str(missing), "--scan-type", "ZAP Scan"])
    assert exit_code == 1
    assert "No such file" in capsys.readouterr().err


def test_main_prints_summary_and_returns_zero_on_success(monkeypatch, report_file, capsys):
    monkeypatch.setenv("DEFECTDOJO_API_KEY", "secret-token")
    stats = {
        "test": 3,
        "statistics": {"after": {"high": {"total": 1}, "low": {"total": 2}, "total": {"total": 3}}},
    }
    monkeypatch.setattr(defectdojo_import, "import_scan", lambda **kwargs: stats)

    exit_code = defectdojo_import.main(["--file", str(report_file), "--scan-type", "ZAP Scan"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "test #3" in out
    assert "high=1" in out and "low=2" in out and "total 3" in out
