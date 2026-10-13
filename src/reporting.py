"""Small, explicit reporting workflow. No DICOM objects are stored here."""

from datetime import datetime, timezone
import click
from flask import jsonify, request
from sqlalchemy import func, inspect, text

from src.models import AssignmentCursor, Patient, Scan, User, db


REPORT_SECTION_LABELS = (
    ("clinical_history", "CLINICAL HISTORY"),
    ("technique", "TECHNIQUE"),
    ("findings", "FINDINGS"),
    ("impression", "IMPRESSION"),
)


def register_reporting_routes(app):
    # Imported here to avoid a module-initialization cycle with database_app.
    from src.database_app import age_on, audit_scan, error_response, has_role, may_view_scan, require_admin, require_auth, scan_payload, tenant_row, utc_isoformat

    def candidates(role, modality=None):
        users = User.query.filter(User.tenant_id == request.current_user.tenant_id, User.is_active.is_(True)).order_by(User.name, User.id).all()
        return [user for user in users if has_role(user, role) and (modality is None or modality in (user.reporting_modalities or "").split(","))]

    def eligible(user_id, role, modality=None):
        user = tenant_row(User, user_id) if isinstance(user_id, str) else None
        return user if user and user.is_active and has_role(user, role) and (modality is None or modality in (user.reporting_modalities or "").split(",")) else None

    def get_visible_scan(scan_id):
        scan = tenant_row(Scan, scan_id)
        if scan is None or not may_view_scan(scan, request.current_user):
            return None
        return scan

    def cancelled(scan):
        return scan.status == "Cancelled"

    def choose_auto(scan, mode):
        pool = candidates("radiologist", scan.modality)
        if not pool:
            return None
        if mode == "least_active":
            active = {user.id: Scan.query.filter(
                Scan.tenant_id == request.current_user.tenant_id,
                Scan.reporting_clinician_id == user.id,
                Scan.report_status.notin_(("Signed", "Unassigned")),
            ).count() for user in pool}
            return min(pool, key=lambda user: (active[user.id], user.name, user.id))
        cursor = db.session.get(AssignmentCursor, (scan.tenant_id, scan.modality))
        if cursor is None:
            cursor = AssignmentCursor(tenant_id=scan.tenant_id, modality=scan.modality)
            db.session.add(cursor)
            db.session.flush()
        cursor = AssignmentCursor.query.filter_by(tenant_id=scan.tenant_id, modality=scan.modality).with_for_update().one()
        ordered = sorted(pool, key=lambda user: user.id)
        last_index = next((index for index, user in enumerate(ordered) if user.id == cursor.last_user_id), -1)
        selected = ordered[(last_index + 1) % len(ordered)]
        cursor.last_user_id = selected.id
        return selected

    @app.get("/api/reporting/candidates")
    @require_auth
    def reporting_candidates():
        if not any(has_role(request.current_user, role) for role in ("admin", "radiologist")):
            return error_response(403, "PERMISSION_DENIED", "Reporting access is required")
        roles = ("radiologist", "qa", "signer") if has_role(request.current_user, "admin") else ("radiologist",)
        return jsonify(success=True, data={role: [
            {"id": user.id, "name": user.name, "modalities": user.reporting_modalities.split(",")}
            for user in candidates(role)
        ] for role in roles}), 200

    @app.get("/api/reporting/worklist")
    @require_auth
    def reporting_worklist():
        """Paginated studies assigned to the current radiologist, never an arbitrary user ID."""
        from src.tat import tat_metrics

        user = request.current_user
        if not has_role(user, "radiologist"):
            return error_response(403, "PERMISSION_DENIED", "Radiologist access is required")
        role = request.args.get("role", "primary").strip()
        stage = request.args.get("stage", "active").strip()
        modality = request.args.get("modality", "").strip().upper()
        search = request.args.get("search", "").strip().lower()
        sort = request.args.get("sort", "oldest").strip()
        allowed_stages = {"active", "all", "Assigned", "Draft", "QA Rejected", "Review Requested", "Reviewed", "QA Pending", "QA Approved", "Signed"}
        if role not in {"primary", "review"} or stage not in allowed_stages or (modality and modality not in {"CT", "MRI", "XR", "US"}) or sort not in {"oldest", "newest"}:
            return error_response(400, "VALIDATION_ERROR", "Choose valid worklist filters")
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "20"))
        except ValueError:
            return error_response(400, "VALIDATION_ERROR", "page and per_page must be integers")
        if page < 1 or not 1 <= per_page <= 50:
            return error_response(400, "VALIDATION_ERROR", "page must be positive and per_page must be 1 to 50")

        query = Scan.query.join(Patient).filter(Scan.tenant_id == user.tenant_id)
        query = query.filter(Scan.reporting_clinician_id == user.id if role == "primary" else Scan.reviewer_id == user.id)
        if stage == "active":
            query = query.filter(Scan.report_status != "Signed", Scan.status != "Cancelled")
            if role == "review":
                query = query.filter(Scan.report_status == "Review Requested")
        elif stage != "all":
            query = query.filter(Scan.report_status == stage)
        if modality:
            query = query.filter(Scan.modality == modality)
        if search:
            pattern = f"%{search}%"
            query = query.filter(db.or_(
                func.lower(Patient.first_name).like(pattern), func.lower(Patient.last_name).like(pattern),
                func.lower(Patient.medical_record_number).like(pattern), func.lower(Scan.study_uid).like(pattern),
            ))
        total = query.count()
        ordering = Scan.scheduled_at.asc().nullslast() if sort == "oldest" else Scan.scheduled_at.desc().nullslast()
        scans = query.order_by(ordering, Scan.id).offset((page - 1) * per_page).limit(per_page).all()
        data = [{
            "scan_id": scan.id, "patient_id": scan.patient_id, "patient_name": scan.patient.full_name,
            "age": age_on(scan.patient.date_of_birth),
            "modality": scan.modality, "body_part": scan.body_part, "study_uid": scan.study_uid,
            "status": scan.status, "report_status": scan.report_status,
            "scheduled_at": utc_isoformat(scan.scheduled_at), "performed_at": utc_isoformat(scan.performed_at),
            "tat": tat_metrics(scan),
        } for scan in scans]
        return jsonify(success=True, data=data, total=total, pagination={
            "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page,
        }), 200

    @app.post("/api/scans/<scan_id>/assignment")
    @require_admin
    def assign_scan(scan_id):
        scan = tenant_row(Scan, scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if scan.report_status == "Signed" or scan.status == "Cancelled":
            return error_response(409, "WORKFLOW_CONFLICT", "Signed or cancelled studies cannot be reassigned")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error_response(400, "VALIDATION_ERROR", "Send a JSON object")
        mode = body.get("mode", "manual")
        if mode not in ("manual", "least_active", "round_robin"):
            return error_response(400, "VALIDATION_ERROR", "Unsupported assignment mode")
        if mode == "manual":
            radiologist = eligible(body.get("radiologist_id"), "radiologist", scan.modality)
            if not radiologist:
                return error_response(400, "VALIDATION_ERROR", "Select an active radiologist eligible for this modality")
        else:
            radiologist = choose_auto(scan, mode)
            if radiologist is None:
                return error_response(409, "NO_ELIGIBLE_RADIOLOGIST", "No active radiologist handles this modality")
        if scan.report_status not in ("Unassigned", "Assigned") and scan.reporting_clinician_id != radiologist.id:
            return error_response(409, "WORKFLOW_CONFLICT", "A started report must be completed before reassignment")
        qa = eligible(body.get("qa_user_id"), "qa") if body.get("qa_user_id") else None
        signer = eligible(body.get("signer_id"), "signer") if body.get("signer_id") else None
        if body.get("qa_user_id") and not qa or body.get("signer_id") and not signer:
            return error_response(400, "VALIDATION_ERROR", "Select active QA and signer users")
        previous = scan.reporting_clinician_id
        scan.reporting_clinician = radiologist
        if qa:
            scan.qa_user = qa
        if signer:
            scan.signer = signer
        if scan.report_status == "Unassigned":
            scan.report_status = "Assigned"
        audit_scan(scan, request.current_user, "report.assigned", {"mode": mode, "previous_radiologist_id": previous, "radiologist_id": radiologist.id})
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.post("/api/reporting/auto-assign")
    @require_admin
    def auto_assign_scans():
        body = request.get_json(silent=True)
        mode = body.get("mode") if isinstance(body, dict) else None
        if mode not in ("least_active", "round_robin"):
            return error_response(400, "VALIDATION_ERROR", "Choose least_active or round_robin")
        qa = eligible(body.get("qa_user_id"), "qa") if body.get("qa_user_id") else None
        signer = eligible(body.get("signer_id"), "signer") if body.get("signer_id") else None
        if not qa or not signer:
            return error_response(400, "VALIDATION_ERROR", "Choose active QA and signer users for automatic assignment")
        scans = Scan.query.filter(Scan.tenant_id == request.current_user.tenant_id, Scan.report_status == "Unassigned", Scan.status != "Cancelled").order_by(Scan.scheduled_at, Scan.id).limit(100).all()
        assigned = 0
        skipped = 0
        for scan in scans:
            radiologist = choose_auto(scan, mode)
            if radiologist is None:
                skipped += 1
                continue
            scan.reporting_clinician = radiologist
            scan.qa_user = qa
            scan.signer = signer
            scan.report_status = "Assigned"
            audit_scan(scan, request.current_user, "report.auto_assigned", {"mode": mode, "radiologist_id": radiologist.id})
            db.session.flush()
            assigned += 1
        db.session.commit()
        return jsonify(success=True, data={"assigned": assigned, "skipped": skipped, "limit": 100}), 200

    @app.put("/api/scans/<scan_id>/report/draft")
    @require_auth
    def save_draft(scan_id):
        scan = get_visible_scan(scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if cancelled(scan):
            return error_response(409, "WORKFLOW_CONFLICT", "Cancelled scans cannot be reported")
        if not has_role(request.current_user, "radiologist") or request.current_user.id != scan.reporting_clinician_id or scan.report_status not in ("Assigned", "Draft", "QA Rejected"):
            return error_response(403, "WORKFLOW_DENIED", "Only the assigned radiologist can edit this draft")
        if scan.performed_at is None:
            return error_response(409, "WORKFLOW_CONFLICT", "Mark the scan performed (Pending Review) before drafting")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error_response(400, "VALIDATION_ERROR", "Send a report draft")
        sections = body.get("sections")
        if sections is not None:
            allowed = {key for key, _ in REPORT_SECTION_LABELS}
            if not isinstance(sections, dict) or set(sections) != allowed or any(
                not isinstance(sections[key], str) or len(sections[key]) > 10000 for key in allowed
            ):
                return error_response(400, "VALIDATION_ERROR", "Provide the four report sections as text (up to 10000 characters each)")
            normalized = {key: sections[key].strip() for key, _ in REPORT_SECTION_LABELS}
            draft = "\n\n".join(f"{label}\n{normalized[key]}" for key, label in REPORT_SECTION_LABELS if normalized[key])
            if not draft or len(draft) > 20000:
                return error_response(400, "VALIDATION_ERROR", "Draft must contain 1 to 20000 characters in total")
        else:
            draft = body.get("text")
            if not isinstance(draft, str) or not draft.strip() or len(draft) > 20000:
                return error_response(400, "VALIDATION_ERROR", "Draft must contain 1 to 20000 characters")
        scan.report_sections = normalized if sections is not None else None
        if scan.report_status == "QA Rejected":
            scan.review_text = None
            scan.reviewer = None
        scan.draft_text = draft.strip()
        scan.report_status = "Draft"
        scan.qa_feedback = None
        scan.qa_approved_at = None
        audit_scan(scan, request.current_user, "report.draft_saved")
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.post("/api/scans/<scan_id>/report/review-request")
    @require_auth
    def request_review(scan_id):
        scan = get_visible_scan(scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if cancelled(scan):
            return error_response(409, "WORKFLOW_CONFLICT", "Cancelled scans cannot be reported")
        if not has_role(request.current_user, "radiologist") or request.current_user.id != scan.reporting_clinician_id or scan.report_status != "Draft":
            return error_response(403, "WORKFLOW_DENIED", "Save a draft before requesting review")
        body = request.get_json(silent=True)
        reviewer = eligible(body.get("reviewer_id"), "radiologist", scan.modality) if isinstance(body, dict) else None
        if reviewer is None or reviewer.id == scan.reporting_clinician_id:
            return error_response(400, "VALIDATION_ERROR", "Choose a different eligible radiologist")
        scan.reviewer = reviewer
        scan.review_text = None
        scan.report_status = "Review Requested"
        audit_scan(scan, request.current_user, "report.review_requested", {"reviewer_id": reviewer.id})
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.post("/api/scans/<scan_id>/report/review")
    @require_auth
    def submit_review(scan_id):
        scan = get_visible_scan(scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if cancelled(scan):
            return error_response(409, "WORKFLOW_CONFLICT", "Cancelled scans cannot be reported")
        if not has_role(request.current_user, "radiologist") or request.current_user.id != scan.reviewer_id or scan.report_status != "Review Requested":
            return error_response(403, "WORKFLOW_DENIED", "Only the requested reviewer can submit a second opinion")
        body = request.get_json(silent=True)
        review = body.get("text") if isinstance(body, dict) else None
        if not isinstance(review, str) or not review.strip() or len(review) > 20000:
            return error_response(400, "VALIDATION_ERROR", "Review must contain 1 to 20000 characters")
        scan.review_text = review.strip()
        scan.report_status = "Reviewed"
        audit_scan(scan, request.current_user, "report.reviewed")
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.post("/api/scans/<scan_id>/report/submit-qa")
    @require_auth
    def submit_qa(scan_id):
        scan = get_visible_scan(scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if cancelled(scan):
            return error_response(409, "WORKFLOW_CONFLICT", "Cancelled scans cannot be reported")
        if not has_role(request.current_user, "radiologist") or request.current_user.id != scan.reporting_clinician_id or scan.report_status not in ("Draft", "Reviewed"):
            return error_response(403, "WORKFLOW_DENIED", "Only the assigned radiologist can submit this draft")
        if scan.report_sections and not all(scan.report_sections.get(key, "").strip() for key in ("findings", "impression")):
            return error_response(400, "VALIDATION_ERROR", "Findings and impression are required before sending to QA")
        if not scan.qa_user_id or not scan.signer_id:
            return error_response(409, "WORKFLOW_CONFLICT", "Ask an administrator to assign QA and signer first")
        if not scan.qa_user.is_active or not scan.signer.is_active or not has_role(scan.qa_user, "qa") or not has_role(scan.signer, "signer"):
            return error_response(409, "WORKFLOW_CONFLICT", "QA and signer must be active")
        scan.report_status = "QA Pending"
        audit_scan(scan, request.current_user, "report.submitted_to_qa")
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.post("/api/scans/<scan_id>/report/qa")
    @require_auth
    def qa_report(scan_id):
        scan = get_visible_scan(scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if cancelled(scan):
            return error_response(409, "WORKFLOW_CONFLICT", "Cancelled scans cannot be reported")
        if not has_role(request.current_user, "qa") or request.current_user.id != scan.qa_user_id or scan.report_status != "QA Pending":
            return error_response(403, "WORKFLOW_DENIED", "Only the assigned QA user can validate this report")
        body = request.get_json(silent=True)
        decision = body.get("decision") if isinstance(body, dict) else None
        feedback = body.get("feedback", "") if isinstance(body, dict) else ""
        if decision not in ("approve", "reject") or not isinstance(feedback, str) or len(feedback) > 20000 or decision == "reject" and not feedback.strip():
            return error_response(400, "VALIDATION_ERROR", "Choose approve or reject; rejection requires feedback")
        scan.qa_feedback = feedback.strip() or None
        scan.qa_approved_at = datetime.now(timezone.utc) if decision == "approve" else None
        scan.report_status = "QA Approved" if decision == "approve" else "QA Rejected"
        audit_scan(scan, request.current_user, "report.qa_" + decision)
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.post("/api/scans/<scan_id>/report/sign")
    @require_auth
    def sign_report(scan_id):
        scan = get_visible_scan(scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if cancelled(scan):
            return error_response(409, "WORKFLOW_CONFLICT", "Cancelled scans cannot be reported")
        if not has_role(request.current_user, "signer") or request.current_user.id != scan.signer_id or scan.report_status != "QA Approved":
            return error_response(403, "WORKFLOW_DENIED", "Only the assigned signer can sign an approved report")
        now = datetime.now(timezone.utc)
        scan.signed_at = now
        scan.reported_at = now
        scan.report_summary = scan.draft_text
        scan.report_status = "Signed"
        scan.status = "Completed"
        audit_scan(scan, request.current_user, "report.signed")
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.cli.command("upgrade-structured-reports")
    def upgrade_structured_reports_command():
        """Add structured report sections without changing existing drafts."""
        if not inspect(db.engine).has_table("scans"):
            raise click.ClickException("Initialize the database before upgrading reports")
        if "report_sections" not in {column["name"] for column in inspect(db.engine).get_columns("scans")}:
            column_type = "JSONB" if db.engine.dialect.name == "postgresql" else "JSON"
            with db.engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE scans ADD COLUMN report_sections {column_type}"))
        click.echo("Structured report sections are ready. Existing drafts were preserved.")
