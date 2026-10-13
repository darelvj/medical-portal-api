"""Reporting permissions and transitions against an isolated fictional database."""

import pytest

from src.database_app import create_app
from src.models import Role, Scan, User, db
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


def sign_in(client, email):
    response = client.post("/api/auth/login", json={"email": email, "password": "demo-password"})
    assert response.status_code == 200


def test_assignment_visibility_and_full_report_workflow(portal):
    with portal.app_context():
        scan = Scan.query.filter_by(status="Pending Review", report_status="Unassigned").first()
        assert scan is not None
        scan_id, patient_id, uid = scan.id, scan.patient_id, scan.study_uid
        radiologists = User.query.join(User.roles).filter(Role.name == "radiologist").order_by(User.name).all()
        primary_id, reviewer_id = radiologists[0].id, radiologists[1].id
        primary_email, reviewer_email = radiologists[0].email, radiologists[1].email
        qa = User.query.join(User.roles).filter(Role.name == "qa").first()
        signer = User.query.join(User.roles).filter(Role.name == "signer").first()
        qa_id, signer_id = qa.id, signer.id
        qa_email, signer_email = qa.email, signer.email

    client = portal.test_client()
    sign_in(client, primary_email)
    assert client.get(f"/api/patients?search={uid}").get_json()["total"] == 0
    assert client.get(f"/api/patients/{patient_id}").status_code == 404

    sign_in(client, "admin@example.test")
    assert client.get("/api/reporting/worklist").status_code == 403
    assigned = client.post(f"/api/scans/{scan_id}/assignment", json={
        "mode": "manual", "radiologist_id": primary_id,
        "qa_user_id": qa_id, "signer_id": signer_id,
    })
    assert assigned.status_code == 200
    assert assigned.get_json()["data"]["report_status"] == "Assigned"

    sign_in(client, reviewer_email)
    assert client.get(f"/api/patients?search={uid}").get_json()["total"] == 0
    assert client.put(f"/api/scans/{scan_id}/report/draft", json={"text": "Not my study"}).status_code == 404

    sign_in(client, primary_email)
    worklist = client.get(f"/api/reporting/worklist?search={uid}&stage=Assigned")
    assert worklist.status_code == 200
    assert worklist.get_json()["total"] == 1
    assert worklist.get_json()["data"][0]["scan_id"] == scan_id
    assert client.get("/api/reporting/worklist?role=review&stage=all").get_json()["total"] == 0
    assert client.get("/api/reporting/worklist?page=0").status_code == 400
    assert client.get(f"/api/patients?search={uid}").get_json()["total"] == 1
    assert client.get(f"/api/patients/{patient_id}").status_code == 200
    assert client.put(f"/api/scans/{scan_id}/report/draft", json={"text": "Fictional draft report"}).status_code == 200
    assert client.post(f"/api/scans/{scan_id}/report/review-request", json={"reviewer_id": reviewer_id}).status_code == 200

    sign_in(client, reviewer_email)
    review_worklist = client.get(f"/api/reporting/worklist?role=review&search={uid}")
    assert review_worklist.status_code == 200
    assert review_worklist.get_json()["total"] == 1
    assert review_worklist.get_json()["data"][0]["scan_id"] == scan_id
    assert client.get(f"/api/patients?search={uid}").get_json()["total"] == 1
    assert client.post(f"/api/scans/{scan_id}/report/review", json={"text": "Independent fictional opinion"}).status_code == 200
    assert client.get(f"/api/reporting/worklist?role=review&search={uid}").get_json()["total"] == 0

    sign_in(client, primary_email)
    assert client.post(f"/api/scans/{scan_id}/report/submit-qa").status_code == 200
    sign_in(client, qa_email)
    assert client.post(f"/api/scans/{scan_id}/report/qa", json={"decision": "approve"}).status_code == 200
    sign_in(client, signer_email)
    signed = client.post(f"/api/scans/{scan_id}/report/sign")
    assert signed.status_code == 200
    assert signed.get_json()["data"]["report_status"] == "Signed"
    assert signed.get_json()["data"]["status"] == "Completed"
    assert client.post(f"/api/scans/{scan_id}/report/sign").status_code == 403


def test_auto_assignment_uses_eligible_radiologists(portal):
    client = portal.test_client()
    sign_in(client, "admin@example.test")
    with portal.app_context():
        qa = User.query.join(User.roles).filter(Role.name == "qa").first()
        signer = User.query.join(User.roles).filter(Role.name == "signer").first()
        qa_id, signer_id = qa.id, signer.id
    result = client.post("/api/reporting/auto-assign", json={
        "mode": "round_robin", "qa_user_id": qa_id, "signer_id": signer_id,
    })
    assert result.status_code == 200
    assert result.get_json()["data"]["assigned"] > 0
    with portal.app_context():
        assigned = Scan.query.filter_by(report_status="Assigned").all()
        assert assigned
        assert all(scan.modality in scan.reporting_clinician.reporting_modalities.split(",") for scan in assigned)


def test_structured_draft_can_be_saved_before_it_is_ready_for_qa(portal):
    with portal.app_context():
        scan = Scan.query.filter_by(status="Pending Review", report_status="Unassigned").first()
        radiologist = User.query.join(User.roles).filter(Role.name == "radiologist").first()
        qa = User.query.join(User.roles).filter(Role.name == "qa").first()
        signer = User.query.join(User.roles).filter(Role.name == "signer").first()
        scan_id = scan.id
        radiologist_id, radiologist_email = radiologist.id, radiologist.email
        qa_id, signer_id = qa.id, signer.id

    client = portal.test_client()
    sign_in(client, "admin@example.test")
    assert client.post(f"/api/scans/{scan_id}/assignment", json={
        "mode": "manual", "radiologist_id": radiologist_id, "qa_user_id": qa_id, "signer_id": signer_id,
    }).status_code == 200
    sign_in(client, radiologist_email)
    partial = {"clinical_history": "Fictional history", "technique": "", "findings": "", "impression": ""}
    bad = client.put(f"/api/scans/{scan_id}/report/draft", json={"sections": {"findings": "Incomplete"}})
    assert bad.status_code == 400
    saved = client.put(f"/api/scans/{scan_id}/report/draft", json={"sections": partial})
    assert saved.status_code == 200
    assert saved.get_json()["data"]["report_sections"] == partial
    assert saved.get_json()["data"]["report_status"] == "Draft"
    assert client.post(f"/api/scans/{scan_id}/report/submit-qa").status_code == 400

    complete = {**partial, "findings": "Fictional observations", "impression": "Fictional conclusion"}
    saved = client.put(f"/api/scans/{scan_id}/report/draft", json={"sections": complete})
    assert saved.status_code == 200
    assert "FINDINGS\nFictional observations" in saved.get_json()["data"]["draft_text"]
    assert "IMPRESSION\nFictional conclusion" in saved.get_json()["data"]["draft_text"]
    assert client.post(f"/api/scans/{scan_id}/report/submit-qa").status_code == 200
    with portal.app_context():
        assert db.session.get(Scan, scan_id).report_sections == complete
