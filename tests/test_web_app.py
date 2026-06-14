from quietroom.web.app import create_app


def test_index_serves_html():
    app, _ = create_app(demo=True)
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Quietroom" in body
    assert "app.js" in body


def test_security_headers_present():
    app, _ = create_app(demo=True)
    resp = app.test_client().get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
