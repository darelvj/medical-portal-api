"""TAT calculations, overrides, and admin-only MIS access."""

from datetime import datetime, timedelta, timezone

import pytest

from src.database_app import create_app
from src.models import Scan, db
from src.seed import seed_demo_data
from src.tat import tat_metrics


@pytest.fixture
def portal():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:", "JWT_SECRET": "test-secret"})
    with app.app_context():
        db.create_all()
        seed_demo_data()
        yield app
        db.session.remove()
        db.drop_all()


def sign_in(client, email):
    assert client.post("/api/auth/login", json={"email": email, "password": "demo-password"}).status_code == 200


def test_tat_prefers_manual_override_without_changing_events(portal):
    with portal.app_context():
        scan = Scan.query.filter_by(report_status="Signed").first()
        assert scan is not None
        scan_id = scan.id
        original_start, original_end = scan.performed_at, scan.signed_at
        calculated = tat_metrics(scan)
        assert calculated["source"] == "calculated"
        assert calculated["minutes"] >= 0

    client = portal.test_client()
    sign_in(client, "clinician@example.test")
    assert client.get("/api/admin/mis/tat").status_code == 403
    assert client.patch(f"/api/admin/scans/{scan_id}/tat", json={"manual_minutes": 90, "note": "Legacy correction"}).status_code == 403

    sign_in(client, "admin@example.test")
    report = client.get("/api/admin/mis/tat")
    assert report.status_code == 200
    assert report.get_json()["data"]["summary"]["measured"] > 0
    assert client.patch(f"/api/admin/scans/{scan_id}/tat", json={"manual_minutes": 90}).status_code == 400
    updated = client.patch(f"/api/admin/scans/{scan_id}/tat", json={
        "manual_minutes": 90, "target_minutes": 120, "note": "Legacy correction",
    })
    assert updated.status_code == 200
    assert updated.get_json()["data"]["tat"]["source"] == "manual"
    assert updated.get_json()["data"]["tat"]["minutes"] == 90
    with portal.app_context():
        scan = db.session.get(Scan, scan_id)
        assert scan.performed_at == original_start
        assert scan.signed_at == original_end
    cleared = client.patch(f"/api/admin/scans/{scan_id}/tat", json={"manual_minutes": None})
    assert cleared.status_code == 200
    assert cleared.get_json()["data"]["tat"]["source"] == "calculated"


def test_pending_elapsed_and_breach(portal):
    with portal.app_context():
        scan = Scan.query.filter_by(report_status="Unassigned", status="Pending Review").first()
        assert scan is not None
        scan.performed_at = datetime.now(timezone.utc) - timedelta(hours=3)
        scan.tat_target_minutes = 60
        db.session.commit()
        metrics = tat_metrics(scan)
        assert metrics["minutes"] is None
        assert metrics["elapsed_minutes"] >= 179
        assert metrics["breached"] is True
