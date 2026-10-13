import pytest

from src.database_app import create_app
from src.models import db
from src.seed import seed_demo_data


@pytest.fixture()
def client():
    app = create_app({
        "TESTING": True,
        "JWT_SECRET": "test-secret",
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
    })
    with app.app_context():
        db.create_all()
        seed_demo_data()
        with app.test_client() as test_client:
            yield test_client
        db.session.remove()
        db.drop_all()


def login(client):
    return client.post("/api/auth/login", json={
        "email": "clinician@example.test", "password": "demo-password"
    })


def test_successful_login_sets_httponly_cookie_without_token_in_json(client):
    response = login(client)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["data"]["email"] == "clinician@example.test"
    assert "token" not in response.json
    assert "HttpOnly" in response.headers["Set-Cookie"]


def test_failed_login(client):
    response = client.post("/api/auth/login", json={
        "email": "clinician@example.test", "password": "wrong"
    })
    assert response.status_code == 401
    assert response.json["error"]["code"] == "INVALID_CREDENTIALS"


def test_authentication_status_and_logout(client):
    assert client.get("/api/auth/me").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/auth/me").json["data"]["role"] == "clinician"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_protected_routes(client):
    assert client.get("/api/patients").status_code == 401
    login(client)
    response = client.get("/api/patients")
    assert response.status_code == 200
    assert response.json["total"] > 75


def test_missing_patient(client):
    login(client)
    response = client.get("/api/patients/does-not-exist")
    assert response.status_code == 404
    assert response.json["error"]["code"] == "PATIENT_NOT_FOUND"


def test_search_and_filtering(client):
    login(client)
    assert client.get("/api/patients?search=mrn-20260001").json["total"] > 0
    assert client.get("/api/patients?status=completed").json["total"] > 0
    assert client.get("/api/patients?modality=mri").json["total"] > 0


def test_future_endpoints_are_protected(client):
    for endpoint in ("/api/dashboard/summary", "/api/appointments", "/api/notifications"):
        assert client.get(endpoint).status_code == 401
    login(client)
    appointments = client.get("/api/appointments").json
    assert appointments["success"] is True
    assert appointments["total"] > 0


def test_dashboard_summary(client):
    login(client)
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200
    assert response.json["data"]["total_patients"] == 75
    assert response.json["data"]["completed_studies"] > 0
    assert response.json["data"]["pending_review"] > 0
    assert set(response.json["data"]["modality_counts"]).issubset({"CT", "MRI", "XR", "US"})


def test_user_preferences(client):
    login(client)
    assert client.get("/api/preferences").json["data"]["theme"] == "dark"
    response = client.put("/api/preferences", json={"theme": "light", "records_per_page": 50})
    assert response.status_code == 200
    assert response.json["data"] == {"theme": "light", "records_per_page": 50}


def test_json_errors_and_cors(client):
    response = client.get("/api/unknown")
    assert response.status_code == 404
    assert response.json["success"] is False
    cors = client.get("/api/auth/me", headers={"Origin": "http://127.0.0.1:5173"})
    assert cors.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5173"
    assert cors.headers["Access-Control-Allow-Credentials"] == "true"
