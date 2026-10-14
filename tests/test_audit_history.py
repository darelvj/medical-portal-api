"""The audit timeline is admin-only and cannot leak another tenant's events."""

from datetime import date
from uuid import uuid4

import pytest

from src.database_app import create_app
from src.models import AuditEvent, Patient, Role, Scan, Tenant, User, db
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
    response = client.post("/api/auth/login", json={"email": email, "password": "demo-password"})
    assert response.status_code == 200


def test_audit_history_filters_permissions_and_safe_details(portal):
    with portal.app_context():
        scan = Scan.query.filter_by(report_status="Unassigned", modality="CT").first()
        radiologist = User.query.join(User.roles).filter(Role.name == "radiologist").first()
        assert scan is not None and radiologist is not None
        scan_id, study_uid, radiologist_id = scan.id, scan.study_uid, radiologist.id
    client = portal.test_client()
    assert client.get("/api/admin/audit-events").status_code == 401
    login(client, "clinician@example.test")
    assert client.get("/api/admin/audit-events").status_code == 403
    login(client, "admin@example.test")
    assert client.get("/api/admin/audit-events").get_json()["total"] == 0
    assert client.get("/api/admin/audit-events?category=wrong").status_code == 400
    assert client.get("/api/admin/audit-events?page=0").status_code == 400
    assigned = client.post(f"/api/scans/{scan_id}/assignment", json={"mode": "manual", "radiologist_id": radiologist_id})
    assert assigned.status_code == 200
    with portal.app_context():
        db.session.add(AuditEvent(
            id=str(uuid4()), tenant_id="tenant-demo", actor_id=None, action="scan.tat_updated",
            entity_type="scan", entity_id=scan_id,
            metadata_json={"current": {"target_minutes": 120, "manual_minutes": None}, "secret": "do not show"},
        ))
        db.session.commit()
    assignment = client.get("/api/admin/audit-events?category=assignment")
    assert assignment.status_code == 200
    assert assignment.get_json()["total"] == 1
    row = assignment.get_json()["data"][0]
    assert row["study_uid"] == study_uid
    assert row["label"] == "Radiologist assigned"
    assert radiologist.name in row["detail"]
    assert client.get("/api/admin/audit-events?category=report").get_json()["total"] == 0
    tat = client.get("/api/admin/audit-events?category=tat").get_json()
    assert tat["total"] == 1
    assert tat["data"][0]["actor_name"] == "Former user"
    assert "do not show" not in str(tat)
    assert client.get(f"/api/admin/audit-events?search={study_uid}").get_json()["total"] == 2
    paged = client.get("/api/admin/audit-events?per_page=1&page=2").get_json()
    assert paged["total"] == 2 and paged["pagination"]["pages"] == 2 and len(paged["data"]) == 1


def test_audit_history_never_returns_other_tenant_events(portal):
    with portal.app_context():
        tenant = Tenant(id="tenant-b", slug="clinic-b", name="Clinic B")
        patient = Patient(id="patient-b", tenant_id=tenant.id, first_name="Other", last_name="Tenant",
                          medical_record_number="MRN-B", date_of_birth=date(1980, 1, 1), gender="Female")
        scan = Scan(id="scan-b", tenant_id=tenant.id, patient=patient, study_uid="study-b-secret",
                    accession_number="ACC-B", modality="CT", body_part="Head", status="Scheduled")
        event = AuditEvent(id=str(uuid4()), tenant_id=tenant.id, action="report.assigned",
                           entity_type="scan", entity_id=scan.id, metadata_json={"mode": "manual"})
        db.session.add_all((tenant, patient, scan, event))
        db.session.commit()
    client = portal.test_client()
    login(client, "admin@example.test")
    assert client.get("/api/admin/audit-events").get_json()["total"] == 0
    assert client.get("/api/admin/audit-events?search=study-b-secret").get_json()["total"] == 0
