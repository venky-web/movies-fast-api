def test_health_reports_storage_backend(client, backend):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": backend}


def test_docs_and_openapi_are_served(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "Movies API"
