"""
Tests for observability.py's two user-visible contracts: the /metrics exposition
and request-id correlation.

Same TestClient posture as test_health_and_cors.py -- instantiated without
`with`, so the startup lifespan never runs and this suite needs no MongoDB,
Ollama, or MCP server.

Deliberately not covered: the individual metric values. Counters are
prometheus_client's job, not this repo's, and asserting on a specific count
would couple the suite to whatever other tests happened to run first against the
same process-global registry.
"""

import logging

from fastapi.testclient import TestClient

import main
import observability

client = TestClient(main.app)


def test_metrics_exposes_prometheus_text_for_a_served_request() -> None:
    client.get("/api/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # The route *template*, not the raw path -- see main._route_template.
    assert 'tender_http_requests_total{method="GET",route="/api/health"' in response.text


def test_scrapes_are_excluded_from_the_apps_own_metrics() -> None:
    """A scrape must not read as application load, or the dashboard measures itself."""
    client.get("/metrics")

    response = client.get("/metrics")

    assert 'route="/metrics"' not in response.text


def test_inbound_request_id_is_adopted_rather_than_replaced() -> None:
    """The cross-process trace depends on this: mcp_client forwards whatever id is
    current, so minting a fresh one here would break correlation at the boundary."""
    response = client.get("/api/health", headers={"X-Request-ID": "caller-supplied"})

    assert response.headers["X-Request-ID"] == "caller-supplied"


def test_exception_is_rendered_into_one_json_line() -> None:
    """The reason for JSON logging at all: a multi-line traceback is one event a
    log collector cannot group back together."""
    formatter = observability.JsonFormatter("test")
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "it failed", None, __import__("sys").exc_info()
        )

    line = formatter.format(record)

    assert "\n" not in line
    assert "ValueError: boom" in line
