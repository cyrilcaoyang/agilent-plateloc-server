"""Agent documentation routes, and the drift guards that keep them honest."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agilent_plateloc_server.documentation import _document, router
from agilent_plateloc_server.service import (
    LAST_ERROR_CODES,
    _ALL_PLATE_SEALER_SKILLS,
)


def test_agent_docs_routes(unclaimed_client: TestClient) -> None:
    r = unclaimed_client.get("/agent-docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "shutdown" in r.text and "allowed_actions" in r.text
    r = unclaimed_client.get("/agent-docs/api-reference")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "/control/seal/start" in r.text
    r = unclaimed_client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "(agent-docs/api-reference)" in r.text and "(openapi.json)" in r.text


@pytest.mark.parametrize("prefix", ["", "/plateloc", "/api/equipment/test/documentation"])
def test_index_links_without_hardware(prefix: str) -> None:
    service = FastAPI()
    service.include_router(router)
    app = FastAPI()
    app.mount(prefix or "/", service)
    with TestClient(app) as client:
        response = client.get(f"{prefix}/llms.txt")
        assert response.status_code == 200
        links = re.findall(r"\]\(([^)]+)\)", response.text)
        assert set(links) == {"agent-docs", "agent-docs/api-reference", "openapi.json"}
        for link in links:
            resolved = urljoin(str(response.url), link)
            assert resolved == f"http://testserver{prefix}/{link}"
            assert client.get(resolved).status_code == 200


def test_openapi_lists_documentation_routes(unclaimed_client: TestClient) -> None:
    paths = unclaimed_client.get("/openapi.json").json()["paths"]
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt"} <= set(paths)


def test_api_reference_documents_every_route(unclaimed_client: TestClient) -> None:
    """Every served path must appear in the reference — the point of the file
    is that an agent can trust it instead of reading the OpenAPI document."""
    reference = unclaimed_client.get("/agent-docs/api-reference").text
    for path in unclaimed_client.get("/openapi.json").json()["paths"]:
        assert path in reference, f"{path} is undocumented in API_REFERENCE.md"


def test_docs_name_every_skill_and_error_code() -> None:
    """The two enumerations the device publishes are documented verbatim, so a
    new skill or ``last_error.code`` cannot ship without a doc update."""
    reference = _document("API_REFERENCE.md")
    for skill in _ALL_PLATE_SEALER_SKILLS:
        assert f"`{skill}`" in reference, f"skill {skill} is undocumented"
    guide = _document("AGENT_GUIDE.md")
    for code in LAST_ERROR_CODES:
        assert f"`{code}`" in guide, f"last_error code {code} is undocumented"
