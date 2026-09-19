"""Push one scan report (OWASP ZAP, Trivy, Semgrep, Checkov, ...) into a
running DefectDojo instance (`make defectdojo-up`, or CI's `zap` workflow)
via its `/api/v2/import-scan/` endpoint — see README's "Security scanning &
load testing" section for why DefectDojo sits downstream of every scanner
here rather than being just another report file: it's where findings get
deduplicated and tracked across runs.

Dependency-light (stdlib argparse + httpx only, no `app.*` import) so this
runs standalone in CI (`.github/workflows/zap.yml`) without installing the
repo's full `requirements-lock.txt` just to POST one file.

Gotchas, verified against a real DefectDojo 3.3.100 instance:
- `auto_create_context` needs BOTH `product_type_name` and `product_name` —
  `product_name` alone 400s ("Product ... does not exist and no
  product_type_name provided") when the Product doesn't exist yet. Defaults
  below pass both so first-run `make defectdojo-import` needs no manual UI
  setup.
- The ZAP parser wants XML, not the JSON report `make zap-baseline`/`make
  zap-api-scan` also produce — importing JSON fails with "Internal error:
  Wrong file format, please use xml."; zap's own `-x` XML report imports
  cleanly.

`SCAN_TYPE` stays a caller-supplied string (DefectDojo's own vocabulary,
e.g. `"ZAP Scan"`, `"Trivy Scan"`, `"Semgrep JSON Report"`) rather than
guessed from the file extension, so a DefectDojo parser rename/addition
can't silently break this.
"""
import argparse
import os
import sys
from pathlib import Path

import httpx

DEFAULT_URL = "http://localhost:8080"
DEFAULT_PRODUCT_TYPE = "agent-core-demo"
DEFAULT_PRODUCT = "agent-core-demo"


def import_scan(
    *,
    base_url: str,
    api_key: str,
    file_path: Path,
    scan_type: str,
    product_type_name: str,
    product_name: str,
    engagement_name: str,
) -> dict:
    """POSTs one report to `/api/v2/import-scan/` with
    `auto_create_context=True` so a first run needs no manual Product/
    Engagement setup. Returns the parsed JSON response (includes a
    `statistics` breakdown by severity); raises `httpx.HTTPStatusError`
    otherwise (DefectDojo's own error bodies are informative as-is).
    """
    with file_path.open("rb") as fh:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/v2/import-scan/",
            headers={"Authorization": f"Token {api_key}"},
            data={
                "scan_type": scan_type,
                "product_type_name": product_type_name,
                "product_name": product_name,
                "engagement_name": engagement_name,
                "auto_create_context": "True",
            },
            files={"file": (file_path.name, fh)},
            timeout=60.0,
        )
    response.raise_for_status()
    return response.json()


def _summarize(result: dict) -> str:
    after = result.get("statistics", {}).get("after", {})
    order = ["critical", "high", "medium", "low", "info"]
    counts = [f"{sev}={after.get(sev, {}).get('total', 0)}" for sev in order if sev in after]
    total = after.get("total", {}).get("total", "?")
    return f"test #{result.get('test')}: {', '.join(counts)} (total {total})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="Path to the scan report file")
    parser.add_argument(
        "--scan-type",
        required=True,
        help='DefectDojo scan_type string, e.g. "ZAP Scan", "Trivy Scan", "Semgrep JSON Report", "Checkov Scan"',
    )
    parser.add_argument(
        "--engagement-name",
        default=os.environ.get("DEFECTDOJO_ENGAGEMENT_NAME", "manual scan"),
        help="Engagement to group this import under (auto-created if new); default from DEFECTDOJO_ENGAGEMENT_NAME or 'manual scan'",
    )
    parser.add_argument(
        "--product-name",
        default=os.environ.get("DEFECTDOJO_PRODUCT_NAME", DEFAULT_PRODUCT),
        help=f"DefectDojo Product name (auto-created if new); default from DEFECTDOJO_PRODUCT_NAME or {DEFAULT_PRODUCT!r}",
    )
    parser.add_argument(
        "--product-type-name",
        default=os.environ.get("DEFECTDOJO_PRODUCT_TYPE_NAME", DEFAULT_PRODUCT_TYPE),
        help=f"DefectDojo Product_Type name (auto-created if new); default from DEFECTDOJO_PRODUCT_TYPE_NAME or {DEFAULT_PRODUCT_TYPE!r}",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("DEFECTDOJO_URL", DEFAULT_URL),
        help=f"DefectDojo base URL; default from DEFECTDOJO_URL or {DEFAULT_URL!r}",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("DEFECTDOJO_API_KEY")
    if not api_key:
        print(
            "DEFECTDOJO_API_KEY is required (My Account -> API v2 Key in the "
            "DefectDojo UI, or generated via /api/v2/api-token-auth/).",
            file=sys.stderr,
        )
        return 1
    if not args.file.is_file():
        print(f"No such file: {args.file}", file=sys.stderr)
        return 1

    try:
        result = import_scan(
            base_url=args.url,
            api_key=api_key,
            file_path=args.file,
            scan_type=args.scan_type,
            product_type_name=args.product_type_name,
            product_name=args.product_name,
            engagement_name=args.engagement_name,
        )
    except httpx.HTTPStatusError as exc:
        print(f"DefectDojo import failed ({exc.response.status_code}): {exc.response.text}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Could not reach DefectDojo at {args.url}: {exc}", file=sys.stderr)
        return 1

    print(f"Imported into DefectDojo ({args.url}) — {_summarize(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
