"""Tracing setup: every turn becomes one trace in Phoenix (SPEC O-1 to O-4).

Phoenix itself runs as a separate process (see run.sh), so traces survive a restart of
this program (O-1). This module only SENDS spans to it.

Stage 5 decision (Option A): we record full messages and replies, because we need them to
debug. That means Phoenix holds customer text. Only the developer can open Phoenix here;
in a real system access would be limited by role and granted as needed.

Call setup_tracing() ONCE, before any agent is built, so ADK and Gemini spans use it.
"""

import os

import httpx
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME", "customer-support")

_provider: TracerProvider | None = None
_project_id: str | None = None  # Phoenix's own id for the project; needed to build trace links


def phoenix_is_up() -> bool:
    """True if Phoenix answers its health check. Used so we fail loudly at start (C-4)."""
    try:
        return httpx.get(f"{PHOENIX_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def setup_tracing() -> None:
    """Create the global TracerProvider and switch on Gemini auto-instrumentation (O-3)."""
    global _provider
    if _provider is not None:
        return  # already set up; calling twice would double every span
    provider = TracerProvider(
        resource=Resource.create({
            "service.name": "customer-support",
            "openinference.project.name": PROJECT_NAME,  # which Phoenix project the spans land in
        })
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{PHOENIX_URL}/v1/traces")))
    trace.set_tracer_provider(provider)  # global, so ADK's own spans use it too
    GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
    _provider = provider


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("support.pipeline")


def flush() -> None:
    """Send any spans still waiting in the queue. Call before the program exits.

    Spans are exported in the background. Without this, a short CLI run can end
    before its last spans are sent and the trace looks incomplete.
    """
    if _provider is not None:
        _provider.force_flush(timeout_millis=5000)


def trace_url(trace_id: str) -> str:
    """A link that opens this trace in Phoenix (O-4).

    Phoenix links use the project's internal id, so we look it up once. The project only
    exists after its first span arrives, so on the very first turn the lookup can miss and
    we fall back to the project name. This only affects the link, never the trace itself.
    """
    global _project_id
    if _project_id is None:
        try:
            rows = httpx.get(f"{PHOENIX_URL}/v1/projects", timeout=2).json().get("data", [])
            _project_id = next((p["id"] for p in rows if p.get("name") == PROJECT_NAME), None)
        except (httpx.HTTPError, ValueError):
            _project_id = None
    return f"{PHOENIX_URL}/projects/{_project_id or PROJECT_NAME}/traces/{trace_id}"
