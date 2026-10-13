"""Cross-tenant reads and writes must fail even with valid IDs and admin roles."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import jwt
from werkzeug.security import generate_password_hash

from src.database_app import create_app
from src.models import Appointment, Notification, Patient, Role, Scan, Tenant, User, db
from src.seed import seed_demo_data


@pytest.fixture
def portal():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:", "JWT_SECRET": "test-secret"})
    with app.app_context():
        db.create_all()
        seed_demo_data()
        tenant_b = Tenant(id="tenant-b", slug="clinic-b", name="Clinic B")
        admin_role = Role.query.filter_by(name="admin").one()
        b_admin = User(
            id="admin-b", tenant_id=tenant_b.id, name="Clinic B Admin", email="admin@clinic-b.test",
            password_hash=generate_password_hash("demo-password"), roles=[admin_role],
        )
        b_patient = Patient(
            id="patient-b", tenant_id=tenant_b.id, medical_record_number="MRN-20260001",
            first_name="Fictional", last_name="TenantB", date_of_birth=date(1980, 1, 1), gender="Female",
        )
        b_scan = Scan(
            id="scan-b", tenant_id=tenant_b.id, patient=b_patient, study_uid=f"2.25.{uuid4().int}",
            accession_number="ACC-B", modality="CT", body_part="Chest", status="Scheduled",
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        b_appointment = Appointment(
            id="appointment-b", tenant_id=tenant_b.id, patient=b_patient, assigned_user=b_admin,
            scheduled_for=datetime.now(timezone.utc) + timedelta(days=2), appointment_type="Imaging", status="Scheduled",
        )
        b_notification = Notification(
            id="notification-b", tenant_id=tenant_b.id, user=b_admin,
            title="Clinic B only", message="Fictional training message", notification_type="demo",
        )
        db.session.add_all((tenant_b, b_admin, b_patient, b_scan, b_appointment, b_notification))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def sign_in(client, email):
    response = client.post("/api/auth/login", json={
        "email": email, "password": "demo-password",
    })
    assert response.status_code == 200
    return response.get_json()["data"]


def test_tenant_admin_cannot_read_or_modify_other_tenant(portal):
    client = portal.test_client()
    a = sign_in(client, "admin@example.test")
    assert a["tenant_slug"] == "demo"
    assert client.get("/api/patients/patient-b").status_code == 404
    assert client.put("/api/patients/patient-b", json={"first_name": "Changed"}).status_code == 404
    assert client.post("/api/patients/patient-b/scans", json={
        "modality": "CT", "body_part": "Chest", "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
    }).status_code == 404
    assert client.patch("/api/scans/scan-b/status", json={"status": "In Progress"}).status_code == 404
    assert client.patch("/api/scans/scan-b/schedule", json={"scheduled_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()}).status_code == 404
    assert client.post("/api/scans/scan-b/assignment", json={"mode": "round_robin"}).status_code == 404
    assert client.put("/api/scans/scan-b/report/draft", json={"sections": {
        "clinical_history": "", "technique": "", "findings": "Cross tenant", "impression": "",
    }}).status_code == 404
    assert client.patch("/api/admin/scans/scan-b/tat", json={"target_minutes": 90}).status_code == 404
    assert client.put("/api/admin/users/admin-b", json={"is_active": False}).status_code == 404
    assert client.put("/api/admin/users/user-001", json={"tenant_id": "tenant-b"}).status_code == 400
    assert all(row["id"] != "patient-b" for row in client.get("/api/patients").get_json()["data"])
    assert all(row["id"] != "admin-b" for row in client.get("/api/admin/users").get_json()["data"])
    assert all(row["scan_id"] != "scan-b" for row in client.get("/api/admin/mis/tat").get_json()["data"]["studies"])
    assert all(row["id"] != "appointment-b" for row in client.get("/api/appointments").get_json()["data"])
    assert all(row["id"] != "notification-b" for row in client.get("/api/notifications").get_json()["data"])
    assert client.get("/api/dashboard/summary").get_json()["data"]["total_patients"] == 75
    start = datetime.now(timezone.utc).date().isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=4)).date().isoformat()
    assert all(row["id"] != "scan-b" for row in client.get(f"/api/scans/calendar?start={start}&end={end}").get_json()["data"])

    b = sign_in(client, "admin@clinic-b.test")
    assert b["tenant_slug"] == "clinic-b"
    assert client.get("/api/patients/patient-b").status_code == 200
    assert client.get("/api/patients/patient-001").status_code == 404
    assert client.get("/api/admin/users").get_json()["total"] == 1
    with portal.app_context():
        clinician_role_id = Role.query.filter_by(name="clinician").one().id
    assert client.put("/api/admin/users/admin-b", json={"role_id": clinician_role_id}).status_code == 400
    assert client.get("/api/patients").get_json()["total"] == 1
    assert client.get("/api/admin/mis/tat").get_json()["data"]["summary"]["studies"] == 1
    assert client.get("/api/appointments").get_json()["total"] == 1
    assert client.get("/api/notifications").get_json()["total"] == 1
    assert client.get("/api/dashboard/summary").get_json()["data"]["total_patients"] == 1
    assert any(row["id"] == "scan-b" for row in client.get(f"/api/scans/calendar?start={start}&end={end}").get_json()["data"])
    with portal.app_context():
        a_radiologist = User.query.filter_by(tenant_id="tenant-demo", email="avery.stone@example.test").one()
        a_radiologist_id = a_radiologist.id
        a_radiologist_email = a_radiologist.email
    assert client.post("/api/scans/scan-b/assignment", json={
        "mode": "manual", "radiologist_id": a_radiologist_id,
    }).status_code == 400
    assert client.get("/api/reporting/candidates").get_json()["data"]["radiologist"] == []
    sign_in(client, a_radiologist_email)
    assert all(row["scan_id"] != "scan-b" for row in client.get("/api/reporting/worklist?stage=all").get_json()["data"])


def test_tenant_is_resolved_from_email_and_token_is_isolated(portal):
    client = portal.test_client()
    missing = client.post("/api/auth/login", json={"email": "admin@example.test"})
    assert missing.status_code == 400
    assert sign_in(client, "admin@clinic-b.test")["tenant_id"] == "tenant-b"
    assert client.get("/api/auth/me").get_json()["data"]["tenant_id"] == "tenant-b"
    forged_context = jwt.encode({
        "sub": "admin-b", "tid": "tenant-demo", "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }, portal.config["JWT_SECRET"], algorithm="HS256")
    client.set_cookie("access_token", forged_context)
    assert client.get("/api/auth/me").status_code == 401


def test_login_emails_are_unique_across_tenants(portal):
    client = portal.test_client()
    sign_in(client, "admin@clinic-b.test")
    with portal.app_context():
        clinician_role_id = Role.query.filter_by(name="clinician").one().id
    response = client.post("/api/admin/users", json={
        "name": "Duplicate", "email": "ADMIN@example.test", "password": "demo-password",
        "role_id": clinician_role_id,
    })
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"
