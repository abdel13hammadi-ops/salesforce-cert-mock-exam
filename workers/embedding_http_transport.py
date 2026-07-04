"""Standard-library HTTP transport for embedding provider adapters.

Performs exactly one POST per call with an explicit timeout and no implicit
retries. Does not log API keys, authorization headers, request bodies, or raw
response bodies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: str


class StdlibEmbeddingHttpTransport:
    """urllib-based ``EmbeddingHttpTransport`` implementation."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        body_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        request = urllib.request.Request(url, data=body_bytes, method="POST")
        for header_name, header_value in headers.items():
            request.add_header(header_name, header_value)

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                return HttpResponse(status_code=int(response.status), body=response_body)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            return HttpResponse(status_code=int(exc.code), body=error_body)
        except urllib.error.URLError:
            raise
