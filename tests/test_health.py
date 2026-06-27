from app import create_app


def test_health_ok():
    app = create_app()
    client = app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["service"] == "unified-model-router"
    assert "uptime_seconds" in body
