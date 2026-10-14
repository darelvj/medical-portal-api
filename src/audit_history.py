"""Read-only, tenant-scoped study audit history for administrators."""

from flask import jsonify, request
from sqlalchemy import func

from src.models import AuditEvent, Patient, Scan, User, db


ASSIGNMENT_ACTIONS = ("report.assigned", "report.auto_assigned")
REPORT_ACTIONS = (
    "report.draft_saved", "report.review_requested", "report.reviewed",
    "report.submitted_to_qa", "report.qa_approve", "report.qa_reject", "report.signed",
)
TAT_ACTIONS = ("scan.tat_updated",)
ACTION_LABELS = {
    "report.assigned": "Radiologist assigned",
    "report.auto_assigned": "Radiologist auto-assigned",
    "report.draft_saved": "Draft saved",
    "report.review_requested": "Second opinion requested",
    "report.reviewed": "Second opinion submitted",
    "report.submitted_to_qa": "Sent to QA",
    "report.qa_approve": "QA approved",
    "report.qa_reject": "QA returned for changes",
    "report.signed": "Report signed",
    "scan.tat_updated": "TAT adjusted",
}
CATEGORIES = {
    "all": ASSIGNMENT_ACTIONS + REPORT_ACTIONS + TAT_ACTIONS,
    "assignment": ASSIGNMENT_ACTIONS,
    "report": REPORT_ACTIONS,
    "tat": TAT_ACTIONS,
}


def event_detail(event, user_names):
    """Expose only approved operational fields, never free-text report content."""
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    if event.action in ASSIGNMENT_ACTIONS:
        method = {"manual": "Manual", "least_active": "Least active", "round_robin": "Round robin"}.get(metadata.get("mode"))
        target = user_names.get(metadata.get("radiologist_id"))
        return " · ".join(part for part in (method, f"Assigned to {target}" if target else None) if part)
    if event.action == "report.review_requested":
        reviewer = user_names.get(metadata.get("reviewer_id"))
        return f"Reviewer: {reviewer}" if reviewer else ""
    if event.action == "scan.tat_updated":
        current = metadata.get("current")
        if not isinstance(current, dict):
            return ""
        details = []
        target = current.get("target_minutes")
        manual = current.get("manual_minutes")
        if type(target) is int:
            details.append(f"Target: {target} min")
        if type(manual) is int:
            details.append(f"Manual TAT: {manual} min")
        return " · ".join(details)
    return ""


def register_audit_history_routes(app):
    from src.database_app import error_response, require_admin, utc_isoformat

    @app.get("/api/admin/audit-events")
    @require_admin
    def list_audit_events():
        category = request.args.get("category", "all").strip()
        search = request.args.get("search", "").strip().lower()
        if category not in CATEGORIES:
            return error_response(400, "VALIDATION_ERROR", "Choose a valid audit category")
        if len(search) > 100:
            return error_response(400, "VALIDATION_ERROR", "Search must be at most 100 characters")
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "20"))
        except ValueError:
            return error_response(400, "VALIDATION_ERROR", "page and per_page must be integers")
        if page < 1 or not 1 <= per_page <= 50:
            return error_response(400, "VALIDATION_ERROR", "page must be positive and per_page must be 1 to 50")

        tenant_id = request.current_user.tenant_id
        query = db.session.query(AuditEvent, Scan, Patient, User).join(
            Scan, db.and_(AuditEvent.entity_id == Scan.id, Scan.tenant_id == tenant_id)
        ).join(
            Patient, db.and_(Scan.patient_id == Patient.id, Patient.tenant_id == tenant_id)
        ).outerjoin(
            User, db.and_(AuditEvent.actor_id == User.id, User.tenant_id == tenant_id)
        ).filter(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.entity_type == "scan",
            AuditEvent.action.in_(CATEGORIES[category]),
        )
        if search:
            pattern = f"%{search}%"
            query = query.filter(db.or_(
                func.lower(Patient.first_name).like(pattern),
                func.lower(Patient.last_name).like(pattern),
                func.lower(Scan.study_uid).like(pattern),
                func.lower(Scan.accession_number).like(pattern),
                func.lower(User.name).like(pattern),
            ))
        total = query.count()
        rows = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
        related_ids = {
            value for event, _, _, _ in rows
            for metadata in (event.metadata_json if isinstance(event.metadata_json, dict) else {},)
            for value in (
                metadata.get("radiologist_id"),
                metadata.get("reviewer_id"),
            ) if isinstance(value, str)
        }
        user_names = {
            user.id: user.name for user in User.query.filter(User.tenant_id == tenant_id, User.id.in_(related_ids)).all()
        } if related_ids else {}
        data = [{
            "id": event.id,
            "occurred_at": utc_isoformat(event.created_at),
            "action": event.action,
            "label": ACTION_LABELS[event.action],
            "detail": event_detail(event, user_names),
            "actor_name": actor.name if actor else "Former user",
            "scan_id": scan.id,
            "study_uid": scan.study_uid,
            "modality": scan.modality,
            "patient_id": patient.id,
            "patient_name": patient.full_name,
        } for event, scan, patient, actor in rows]
        return jsonify(success=True, data=data, total=total, pagination={
            "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page,
        }), 200
