import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.browser_trust import BrowserTrustPolicy


def _app(policy: BrowserTrustPolicy) -> FastAPI:
    app = FastAPI()

    @app.api_route("/probe", methods=["GET", "POST"])
    async def probe(request: Request):
        policy.require_host(request)
        if request.method == "POST":
            policy.require_same_origin(request)
        return {"ok": True}

    return app


def test_configured_origin_cannot_be_redefined_by_forged_host() -> None:
    policy = BrowserTrustPolicy({"http://192.168.0.115:8093"})
    with TestClient(_app(policy), base_url="http://192.168.0.115:8093") as client:
        accepted = client.post("/probe", headers={"Origin": "http://192.168.0.115:8093"})
        forged = client.post(
            "/probe",
            headers={"Host": "attacker.invalid", "Origin": "http://attacker.invalid"},
        )

    assert accepted.status_code == 200
    assert forged.status_code == 400


def test_origin_must_match_the_allowed_request_host_not_only_the_allowlist() -> None:
    policy = BrowserTrustPolicy({"http://192.168.0.115:8093", "http://lantern:8093"})
    with TestClient(_app(policy), base_url="http://lantern:8093") as client:
        response = client.post("/probe", headers={"Origin": "http://192.168.0.115:8093"})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.0.115:8093/path",
        "http://user@192.168.0.115:8093",
        "ftp://192.168.0.115:8093",
        "192.168.0.115:8093",
        "http://lantern:not-a-port",
        "http://lantern",
    ],
)
def test_trusted_origins_must_be_exact(origin: str) -> None:
    with pytest.raises(ValueError, match="exact http"):
        BrowserTrustPolicy({origin})
