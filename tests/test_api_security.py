import pytest

from careeros.api.main import app
from tests.support import TestClient


def client() -> TestClient:
    return TestClient(app, auto_write=False)


def test_trusted_loopback_success_has_security_headers():
    response = client().get("/health")
    assert response.status_code == 200
    assert (
        response.headers["content-security-policy"]
        == "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "host",
    [
        "localhost.evil",
        "localhost.",
        "localhost:abc",
        "localhost:0",
        "localhost:65536",
        "user@localhost",
        "[::1",
        "localhost,127.0.0.1",
    ],
)
def test_host_attacks_rejected(host: str):
    response = client().get("/health", headers={"Host": host})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://127.0.0.1",
        "http://localhost.evil",
        "http://127.0.0.2",
        "http://127.0.0.1.evil",
    ],
)
def test_origin_attacks_rejected(origin: str):
    response = client().get("/health", headers={"Origin": origin})
    assert response.status_code == 403


def test_exact_http_origin_and_fetch_metadata_allowed():
    response = client().get(
        "/health",
        headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("fetch", ["same-site", "cross-site", "nonevil", "unknown"])
def test_fetch_metadata_rejected(fetch: str):
    assert client().get("/health", headers={"Sec-Fetch-Site": fetch}).status_code == 403


@pytest.mark.parametrize(
    "header",
    [
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Port",
        "X-Real-IP",
    ],
)
def test_forwarded_headers_rejected(header: str):
    assert client().get("/health", headers={header: "127.0.0.1"}).status_code == 403


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/report"),
        ("post", "/ranked-jobs"),
        ("post", "/review-queue"),
        ("post", "/applications/1/requirements"),
        ("put", "/applications/1/requirements-review"),
        ("delete", "/applications/1/requirements-review"),
    ],
)
def test_all_unsafe_methods_require_write_intent(method: str, path: str):
    response = getattr(client(), method)(path)
    assert response.status_code == 403


def test_write_intent_does_not_replace_validation():
    response = client().post("/report", headers={"X-CareerOS-Write": "1"})
    assert response.status_code != 403


def test_preflight_fails_closed_and_errors_have_headers():
    response = client().options(
        "/report",
        headers={"Origin": "http://127.0.0.1", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_docs_disabled():
    test_client = client()
    assert test_client.get("/openapi.json").status_code == 404
    assert test_client.get("/docs").status_code == 404
    assert test_client.get("/redoc").status_code == 404
