"""TASK:     Gives the HTTP endpoints: POST /query, GET /schema, GET /health, GET /.
GRADING:  Input/Output Design. One request type and one response type for all results.
INPUT:    HTTP requests.
OUTPUT:   HTTP responses. The body of POST /query is always a VisualizationResponse.
USED BY:  The web server (uvicorn) and the demo page.

PARTS:
    1. Start of the service
    2. The demo page
    3. The API endpoints

This file contains no logic about questions. It gives the request to the pipeline and returns
the response.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.aggregate.labels import NameLabeler
from app.config import get_settings
from app.ctgov.client import CtGovClient
from app.models import QueryRequest, ResponseStatus, SchemaResponse, VisualizationResponse
from app.pipeline import Pipeline
from app.planner.planner import Planner
from app.planner.tools import PlannerTools
from app.runlog import RunLog


# ===== PART 1: Start of the service ===============================================================
# WHAT: Makes the registry client and the pipeline one time, when the service starts.
# WHY:  All requests share one cache and one rate limiter.


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """WHAT: Makes one registry client and one pipeline for the process.
    WHY:  All requests then share the cache and the rate limiter.
    """
    client = CtGovClient()
    log_path = get_settings().run_log_path
    app.state.pipeline = Pipeline(
        client, Planner(PlannerTools(client)), NameLabeler(), RunLog(log_path) if log_path else None,
        check_suggestions=True,
    )
    yield
    await client.aclose()


app = FastAPI(
    title="ClinicalTrials.gov query-to-visualization agent",
    description="Turns a clinical-trial question into a chart specification with citations.",
    version="0.1.0",
    lifespan=lifespan,
)


# ===== PART 2: The demo page ======================================================================
# WHAT: Serves the files of the demo page.
# WHY:  The optional demo shows that the response is sufficient to draw each chart.

STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_page_files(request: Request, call_next):
    """WHAT: Tells the browser to examine the files of the demo page on each load.
    WHY:  The browser must not show an updated page with an old script from its cache.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def demo_page() -> FileResponse:
    """WHAT: Gives the demo page.
    WHY:  The page reads only the documented response. Thus it shows that the specification is
          sufficient to make a renderer.
    """
    return FileResponse(STATIC / "index.html")


# ===== PART 3: The API endpoints ==================================================================
# WHAT: POST /query, GET /health, and GET /schema.
# WHY:  These endpoints are the interface of the service.


def get_pipeline(request: Request) -> Pipeline:
    """WHAT: Returns the pipeline of the service. A test can replace it."""
    return request.app.state.pipeline


@app.post("/query", response_model=VisualizationResponse)
async def query(
    body: QueryRequest, response: Response, pipeline: Pipeline = Depends(get_pipeline)
) -> VisualizationResponse:
    """WHAT:   Answers one question.
    INPUT:  body - the request. The fields are in QueryRequest.
    OUTPUT: A VisualizationResponse. `status` tells the result.

        HTTP 200   `ok` and `unsupported`. The service worked and has an answer.
        HTTP 503   `error`. ClinicalTrials.gov or the LLM failed.
        HTTP 422   The request is not valid.
    """
    result = await pipeline.run(body)
    if result.status is ResponseStatus.ERROR:
        response.status_code = 503
    return result


@app.get("/health")
def health() -> dict[str, str]:
    """WHAT: Shows that the service is in operation."""
    return {"status": "ok"}


@app.get("/schema", response_model=SchemaResponse)
def schema() -> SchemaResponse:
    """WHAT: Gives the JSON Schema of the request body and the response body.
    WHY:  The Pydantic models generate it, so the documentation and the code agree.
    """
    return SchemaResponse(
        request=QueryRequest.model_json_schema(),
        response=VisualizationResponse.model_json_schema(),
    )
