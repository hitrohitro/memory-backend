import os
import tempfile

os.environ["BACKEND_SECRET"] = "x" * 32
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient
from server import app

c = TestClient(app)
H = {"x-backend-secret": "x" * 32}


def test_health_is_open():
    assert c.get("/health").json() == {"ok": True}


def test_bad_secret_is_404():
    assert c.get("/recent").status_code == 404


def test_save_then_search():
    c.post("/save", headers=H, json={"title": "Tailscale", "summary": "mesh vpn setup"})
    r = c.post("/search", headers=H, json={"query": "tailscale"})
    assert len(r.json()["results"]) == 1


def test_credential_rejected():
    r = c.post("/save", headers=H, json={"title": "t", "summary": "api_key=abcdef123456"})
    assert r.status_code == 422
