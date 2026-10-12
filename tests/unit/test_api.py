"""TASK: Tests the HTTP endpoints and the demo page.
GRADING: Input/Output Design. Code Quality.
"""
from fastapi.testclient import TestClient

from app.api import app
from app.config import Settings
from app.models import ChartType

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_schema_returns_json_schema_for_request_and_response():
    body = client.get("/schema").json()
    assert set(body) == {"request", "response"}

    request = body["request"]
    assert request["type"] == "object"
    assert request["required"] == ["query"]
    assert request["additionalProperties"] is False
    assert request["properties"]["query"]["minLength"] == 3
    assert request["properties"]["query"]["maxLength"] == 500
    assert request["properties"]["max_citations"]["maximum"] == 50

    response = body["response"]
    assert {"status", "query", "visualization", "citations", "meta", "error"} <= set(
        response["properties"]
    )


def test_schema_documents_every_request_field():
    properties = client.get("/schema").json()["request"]["properties"]
    undocumented = [name for name, prop in properties.items() if not prop.get("description")]
    assert undocumented == []


def test_schema_lists_one_visualization_per_chart_type():
    response = client.get("/schema").json()["response"]
    variants = next(
        option for option in response["properties"]["visualization"]["anyOf"] if "oneOf" in option
    )
    assert set(variants["discriminator"]["mapping"]) == {chart.value for chart in ChartType}


def test_settings_defaults_match_the_spec(monkeypatch):
    for name in ["PLANNER_MODEL", "LABEL_MODEL", "MAX_TRIALS", "CACHE_TTL_SECONDS"]:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.planner_model == "gpt-5.4-mini"
    assert settings.label_model == "gpt-5.4-nano"
    assert settings.max_trials == 20_000
    assert settings.cache_ttl_seconds == 3600
    assert settings.ctgov_concurrency == 4
    assert settings.ctgov_max_attempts == 3
    assert settings.planner_max_tool_calls == 3


def test_settings_read_overrides_from_the_environment(monkeypatch):
    monkeypatch.setenv("PLANNER_MODEL", "gpt-5.4")
    monkeypatch.setenv("MAX_TRIALS", "500")
    settings = Settings(_env_file=None)
    assert settings.planner_model == "gpt-5.4"
    assert settings.max_trials == 500


# ---- POST /query --------------------------------------------------------------------------------


class _StubPipeline:
    """Returns a fixed response and records the request. Thus the test examines only the HTTP layer."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.seen = None

    async def run(self, request):
        from app.models import VisualizationResponse

        self.seen = request
        error = None if self.status == "ok" else {"code": "x", "message": "why"}
        return VisualizationResponse(status=self.status, query=request.query, error=error)


def _post(status: str, body: dict):
    from app.api import get_pipeline

    stub = _StubPipeline(status)
    app.dependency_overrides[get_pipeline] = lambda: stub
    try:
        return client.post("/query", json=body), stub
    finally:
        app.dependency_overrides.clear()


def test_query_returns_the_envelope_with_http_200():
    response, stub = _post("ok", {"query": "pembrolizumab trials by phase", "max_citations": 3})
    assert response.status_code == 200
    assert response.json()["status"] == "ok" and response.json()["query"] == "pembrolizumab trials by phase"
    assert stub.seen.max_citations == 3


def test_unsupported_is_http_200_because_the_service_did_answer():
    response, _ = _post("unsupported", {"query": "which drug works best?"})
    assert response.status_code == 200 and response.json()["error"]["message"] == "why"


def test_error_is_http_503_with_the_same_envelope():
    response, _ = _post("error", {"query": "pembrolizumab trials by phase"})
    assert response.status_code == 503
    assert set(response.json()) == {"status", "query", "visualization", "citations", "meta", "error"}


def test_malformed_request_is_http_422_and_never_reaches_the_pipeline():
    for body in ({}, {"query": "ab"}, {"query": "trials", "trial_phase": ["Phase 3"]}, {"query": "trials", "colour": "red"}):
        response, stub = _post("ok", body)
        assert response.status_code == 422 and stub.seen is None


def test_app_starts_and_wires_a_pipeline_with_a_planner():
    from app.pipeline import Pipeline

    with TestClient(app) as started:
        assert isinstance(started.app.state.pipeline, Pipeline)
        assert started.get("/health").json() == {"status": "ok"}


# ---- the demo page ------------------------------------------------------------------------------


def test_demo_page_and_its_assets_are_served():
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "<title>Clinical trial explorer</title>" in page.text
    for asset in ("/static/app.js", "/static/app.css"):
        assert asset in page.text and client.get(asset).status_code == 200


def test_demo_page_has_a_renderer_for_every_chart_type():
    script = client.get("/static/app.js").text
    renderers = script[script.index("const RENDERERS = {"):]
    for chart in ChartType:
        assert f"{chart.value}:" in renderers, f"no renderer for {chart.value}"


def test_demo_page_offers_every_chart_type_and_status_the_request_accepts():
    page = client.get("/").text
    for chart in ChartType:
        assert f'value="{chart.value}"' in page
    for phase in ("EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"):
        assert f'data-value="{phase}"' in page


def test_demo_page_never_builds_html_from_response_text():
    """The names of sponsors, drugs, and trials come from an external registry. The page uses them as text only."""
    script = client.get("/static/app.js").text
    assert "innerHTML" not in script and "document.write" not in script


def test_hidden_sections_of_the_demo_page_cannot_be_shown_by_a_layout_rule():
    """The loading box uses `display: flex`.

    Without this rule, the box stayed on the screen above the completed answer. The browser
    check found this.
    """
    assert "[hidden] { display: none !important; }" in client.get("/static/app.css").text


def test_dense_charts_in_the_demo_page_can_be_zoomed():
    """Scatter plots and networks get zoom buttons, drag, and a reset.

    A plain scroll stays with the page. Only a pinch or Ctrl + scroll zooms.
    """
    script = client.get("/static/app.js").text
    assert script.count("attachZoom(g,") == 2  # scatter and network
    assert "Reset view" in script and "if (!event.ctrlKey) return;" in script


def test_demo_page_turns_narrower_versions_into_one_click_choices():
    script = client.get("/static/app.js").text
    assert "response.error.suggestions" in script and "applyRequestFields(offer.request || {})" in script
    assert 'if (offer.query) { clearFilters(); $("query").value = offer.query; }' in script  # a reworded question replaces the text


def test_messages_appear_above_the_examples_not_below_them():
    """A refusal or an error must be the first item on the page.

    The browser check found that it was below the example grid, where the user did not see it.
    """
    page = client.get("/").text
    assert page.index('id="message"') < page.index('id="start"') and page.index('id="loading"') < page.index('id="start"')
    assert page.index('id="message"') < page.index('class="hero"')  # the very first thing on the page


def test_page_files_are_always_revalidated_by_the_browser():
    for path in ("/", "/static/app.js", "/static/app.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/health").headers
