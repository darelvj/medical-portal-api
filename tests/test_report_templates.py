"""Tenant-owned reporting templates and permissions."""

import pytest
from werkzeug.security import generate_password_hash

from src.database_app import create_app
from src.models import Role, Tenant, User, db
from src.seed import seed_demo_data


@pytest.fixture
def portal():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:", "JWT_SECRET": "test-secret"})
    with app.app_context():
        db.create_all()
        seed_demo_data()
        yield app
        db.session.remove()
        db.drop_all()


def login(client, email):
    assert client.post("/api/auth/login", json={"email": email, "password": "demo-password"}).status_code == 200


def test_template_permissions_and_lifecycle(portal):
    client = portal.test_client()
    assert client.get("/api/reporting/templates").status_code == 401
    login(client, "clinician@example.test")
    assert client.get("/api/reporting/templates").status_code == 403
    login(client, "avery.stone@example.test")
    outlines = client.get("/api/reporting/templates?modality=CT")
    assert outlines.status_code == 200
    assert outlines.get_json()["total"] == 1
    assert outlines.get_json()["data"][0]["sections"]["impression"] == ""

    details = {"name": "CT chest", "modality": "CT", "sections": {
        "clinical_history": "", "technique": "Protocol:", "findings": "Observations:", "impression": "",
    }}
    assert client.post("/api/reporting/templates", json=details).status_code == 403
    login(client, "admin@example.test")
    assert client.post("/api/reporting/templates", json={**details, "sections": {"findings": "Incomplete"}}).status_code == 400
    created = client.post("/api/reporting/templates", json=details)
    assert created.status_code == 201
    template_id = created.get_json()["data"]["id"]
    assert client.post("/api/reporting/templates", json=details).status_code == 409
    assert client.put(f"/api/reporting/templates/{template_id}", json={"is_active": False}).status_code == 200
    login(client, "avery.stone@example.test")
    assert client.get("/api/reporting/templates?modality=CT").get_json()["total"] == 1
    assert client.get("/api/reporting/templates?include_inactive=1&modality=CT").get_json()["total"] == 1
    assert client.put(f"/api/reporting/templates/{template_id}", json={"is_active": True}).status_code == 403
    login(client, "admin@example.test")
    assert client.get("/api/reporting/templates?include_inactive=1&modality=CT").get_json()["total"] == 2


def test_templates_are_tenant_scoped(portal):
    with portal.app_context():
        admin_role = Role.query.filter_by(name="admin").first()
        tenant = Tenant(id="tenant-other", slug="other", name="Other")
        other_admin = User(id="user-other", tenant_id=tenant.id, name="Other Admin", email="other@example.test",
                           password_hash=generate_password_hash("demo-password"), roles=[admin_role])
        db.session.add_all([tenant, other_admin])
        db.session.commit()
    client = portal.test_client()
    login(client, "admin@example.test")
    first_tenant_template = client.get("/api/reporting/templates").get_json()["data"][0]["id"]
    login(client, "other@example.test")
    assert client.get("/api/reporting/templates").get_json()["total"] == 0
    assert client.put(f"/api/reporting/templates/{first_tenant_template}", json={"is_active": False}).status_code == 404
