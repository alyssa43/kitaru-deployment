"""Verify the public endpoint before giving its URL to the operator."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener

from evalve import EvalveError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_for_kitaru(url: str, timeout: int = 600) -> None:
    if not url.startswith("https://"):
        raise EvalveError("Kitaru readiness requires an HTTPS URL.")
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"{url}/health", timeout=8) as response:
                healthy = json.loads(response.read(65_536)).get("database") == "ok"
            if healthy:
                try:
                    with opener.open(f"{url}/api/v1/sessions", timeout=8):
                        raise EvalveError(
                            "Kitaru accepted an API request without credentials."
                        )
                except HTTPError as exc:
                    exc.close()
                    if exc.code == 401:
                        return
        except HTTPError as exc:
            exc.close()
        except (URLError, OSError, ValueError, AttributeError):
            pass
        time.sleep(5)
    raise EvalveError(
        f"Kitaru did not become ready at {url}. Check DNS and the EC2 bootstrap logs, then retry up."
    )
