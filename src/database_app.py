import os
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from uuid import uuid4

import click
import jwt
from flask import Flask, current_app, has_request_context, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from sqlalchemy import func, inspect, text
from werkzeug.security import check_password_hash

from src.models import Appointment, AuditEvent, Notification, Patient, Permission, Role, Scan, Tenant, User, UserPreference, db
from src.seed import seed_demo_data


load_dotenv()


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        JWT_SECRET=os.getenv("JWT_SECRET", "development-only-change-me"),
        JWT_EXPIRATION_MINUTES=int(os.getenv("JWT_EXPIRATION_MINUTES", "60")),
        AUTH_COOKIE_NAME=os.getenv("AUTH_COOKIE_NAME", "access_token"),
        COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        COOKIE_SAMESITE=os.getenv("COOKIE_SAMESITE", "Lax"),
        ENVIRONMENT=os.getenv("FLASK_ENV", "development"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///medical_portal.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    if test_config:
        app.config.update(test_config)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
        engine_options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))
        connect_args = dict(engine_options.get("connect_args", {}))
        connect_args["options"] = f"{connect_args.get('options', '')} -c timezone=UTC".strip()
        engine_options["connect_args"] = connect_args
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options

    db.init_app(app)
    allowed_origins = os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    CORS(app, origins=allowed_origins, supports_credentials=True)

    @app.after_request
    def prevent_api_caching(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    register_routes(app)
    from src.reporting import register_reporting_routes
    register_reporting_routes(app)
    from src.report_templates import register_report_template_routes
    register_report_template_routes(app)
    from src.tat import register_tat_routes
    register_tat_routes(app)
    from src.audit_history import register_audit_history_routes
    register_audit_history_routes(app)
    from src.tenancy import register_tenant_commands
    register_tenant_commands(app)
    register_commands(app)
    return app


def error_response(status, code, message):
    return jsonify(success=False, error={"code": code, "message": message}), status


def as_utc(value):
    """Normalize database timestamps; SQLite may return UTC values without tzinfo."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat(value):
    normalized = as_utc(value)
    return normalized.isoformat().replace("+00:00", "Z") if normalized else None


def age_on(dob):
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def primary_role(user):
    return user.roles[0].name if user.roles else "unassigned"


def has_role(user, role):
    return any(item.name == role for item in user.roles)


def reporting_role(user):
    return any(has_role(user, role) for role in ("radiologist", "qa", "signer")) and not has_role(user, "admin")


def visible_scans(query, user):
    query = query.filter(Scan.tenant_id == user.tenant_id)
    if reporting_role(user):
        query = query.filter(db.or_(
            Scan.reporting_clinician_id == user.id, Scan.reviewer_id == user.id,
            Scan.qa_user_id == user.id, Scan.signer_id == user.id,
        ))
    return query


def may_view_scan(scan, user):
    return scan.tenant_id == user.tenant_id and (not reporting_role(user) or user.id in (
        scan.reporting_clinician_id, scan.reviewer_id, scan.qa_user_id, scan.signer_id,
    ))


def tenant_row(model, row_id):
    """Return a tenant-owned row without revealing whether another tenant owns the ID."""
    return model.query.filter_by(id=row_id, tenant_id=request.current_user.tenant_id).first()


def audit_scan(scan, actor, action, metadata=None):
    db.session.add(AuditEvent(
        id=os.urandom(16).hex(), tenant_id=actor.tenant_id, actor=actor, action=action,
        entity_type="scan", entity_id=scan.id, metadata_json=metadata or {},
    ))


def public_user(user):
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "tenant_slug": user.tenant.slug,
        "tenant_name": user.tenant.name,
        "name": user.name,
        "email": user.email,
        "role": primary_role(user),
        "roles": [role.name for role in user.roles],
        "is_active": user.is_active,
        "reporting_modalities": user.reporting_modalities.split(",") if user.reporting_modalities else [],
    }


def require_permission(permission_code):
    def decorator(view):
        @wraps(view)
        @require_auth
        def wrapped(*args, **kwargs):
            permissions = {
                permission.code
                for role in request.current_user.roles
                for permission in role.permissions
            }
            if permission_code not in permissions:
                return error_response(403, "PERMISSION_DENIED", "You do not have permission to perform this action")
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_admin(view):
    @wraps(view)
    @require_auth
    def wrapped(*args, **kwargs):
        if not any(role.name == "admin" for role in request.current_user.roles):
            return error_response(403, "ADMIN_REQUIRED", "Administrator access is required")
        return view(*args, **kwargs)
    return wrapped


def scan_payload(scan):
    from src.tat import tat_metrics
    if has_request_context() and hasattr(request, "current_user") and scan.tenant_id != request.current_user.tenant_id:
        raise RuntimeError("Cross-tenant scan serialization was blocked")
    patient = scan.patient
    return {
        "id": patient.id,
        "name": patient.full_name,
        "age": age_on(patient.date_of_birth),
        "gender": patient.gender,
        "modality": scan.modality,
        "study_uid": scan.study_uid,
        "status": scan.status,
        "scan_id": scan.id,
        "body_part": scan.body_part,
        "scheduled_at": utc_isoformat(scan.scheduled_at),
        "performed_at": utc_isoformat(scan.performed_at),
        "report_status": scan.report_status,
        "assigned_radiologist_id": scan.reporting_clinician_id,
        "assigned_radiologist_name": scan.reporting_clinician.name if scan.reporting_clinician else None,
        "reviewer_id": scan.reviewer_id,
        "reviewer_name": scan.reviewer.name if scan.reviewer else None,
        "qa_user_id": scan.qa_user_id,
        "qa_user_name": scan.qa_user.name if scan.qa_user else None,
        "signer_id": scan.signer_id,
        "signer_name": scan.signer.name if scan.signer else None,
        "draft_text": scan.draft_text,
        "report_sections": scan.report_sections,
        "review_text": scan.review_text,
        "qa_feedback": scan.qa_feedback,
        "qa_approved_at": utc_isoformat(scan.qa_approved_at),
        "signed_at": utc_isoformat(scan.signed_at),
        "tat": tat_metrics(scan),
    }


def parse_future_scan_time(value):
    if not isinstance(value, str):
        return None
    try:
        scheduled_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        return None
    scheduled_at = scheduled_at.astimezone(timezone.utc)
    return scheduled_at if scheduled_at > datetime.now(timezone.utc) else None


def parse_calendar_bound(value, inclusive_end_date=False):
    if not isinstance(value, str):
        return None
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(timezone.utc)
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return parsed + timedelta(days=1) if inclusive_end_date else parsed
    except ValueError:
        return None


def patient_payload(patient, user):
    scans = sorted(
        (scan for scan in patient.scans if may_view_scan(scan, user)),
        key=lambda scan: as_utc(scan.scheduled_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True,
    )
    latest_scan = scans[0] if scans else None
    payload = {
        "id": patient.id,
        "name": patient.full_name,
        "age": age_on(patient.date_of_birth),
        "gender": patient.gender,
        "medical_record_number": patient.medical_record_number,
        "blood_type": patient.blood_type,
        "date_of_birth": patient.date_of_birth.isoformat(),
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "phone": patient.phone,
        "email": patient.email,
        "latest_scan": scan_payload(latest_scan) if latest_scan else None,
        "scans": [scan_payload(scan) for scan in scans],
    }
    if latest_scan:
        payload.update({
            "modality": latest_scan.modality,
            "study_uid": latest_scan.study_uid,
            "status": latest_scan.status,
        })
    return payload


def issue_token(user, app):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user.id, "tid": user.tenant_id, "iat": now, "exp": now + timedelta(minutes=app.config["JWT_EXPIRATION_MINUTES"])},
        app.config["JWT_SECRET"], algorithm="HS256",
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        token = request.cookies.get(current_app.config["AUTH_COOKIE_NAME"])
        if not token:
            return error_response(401, "AUTHENTICATION_REQUIRED", "Authentication is required")
        try:
            payload = jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return error_response(401, "INVALID_AUTHENTICATION", "Authentication is invalid or expired")
        user = db.session.get(User, payload.get("sub"))
        if user is None or not user.is_active or not user.tenant.is_active or payload.get("tid") != user.tenant_id:
            return error_response(401, "INVALID_AUTHENTICATION", "Authentication is invalid")
        request.current_user = user
        return view(*args, **kwargs)
    return wrapped


def user_preferences_payload(preference):
    return {"theme": preference.theme, "records_per_page": preference.records_per_page}


def register_routes(app):
    @app.errorhandler(404)
    def not_found(_error):
        return error_response(404, "NOT_FOUND", "The requested resource was not found")

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return error_response(405, "METHOD_NOT_ALLOWED", "The HTTP method is not supported")

    @app.post("/api/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        email, password = body.get("email"), body.get("password")
        if not all(isinstance(value, str) and value.strip() for value in (email, password)):
            return error_response(400, "VALIDATION_ERROR", "Email and password are required")
        matches = User.query.filter(func.lower(User.email) == email.strip().lower()).limit(2).all()
        # Never choose an arbitrary tenant if a legacy database has duplicate emails.
        user = matches[0] if len(matches) == 1 else None
        if user is None or not user.is_active or not user.tenant.is_active or not check_password_hash(user.password_hash, password):
            return error_response(401, "INVALID_CREDENTIALS", "Email or password is incorrect")
        user.last_login_at = datetime.now(timezone.utc)
        db.session.add(AuditEvent(id=os.urandom(16).hex(), tenant_id=user.tenant_id, actor=user, action="auth.login", entity_type="user", entity_id=user.id))
        db.session.commit()
        response = jsonify(success=True, data=public_user(user))
        response.set_cookie(
            app.config["AUTH_COOKIE_NAME"], issue_token(user, app), httponly=True,
            secure=app.config["COOKIE_SECURE"], samesite=app.config["COOKIE_SAMESITE"],
            max_age=app.config["JWT_EXPIRATION_MINUTES"] * 60, path="/",
        )
        return response, 200

    @app.post("/api/auth/logout")
    def logout():
        response = jsonify(success=True, data=None)
        response.delete_cookie(app.config["AUTH_COOKIE_NAME"], path="/")
        return response, 200

    @app.get("/api/auth/me")
    @require_auth
    def me():
        return jsonify(success=True, data=public_user(request.current_user)), 200

    @app.get("/api/preferences")
    @require_auth
    def get_preferences():
        preference = request.current_user.preference
        if preference is None:
            preference = UserPreference(user=request.current_user)
            db.session.add(preference)
            db.session.commit()
        return jsonify(success=True, data=user_preferences_payload(preference)), 200

    @app.put("/api/preferences")
    @require_auth
    def update_preferences():
        body = request.get_json(silent=True) or {}
        theme = body.get("theme")
        records_per_page = body.get("records_per_page")
        preference = request.current_user.preference or UserPreference(user=request.current_user)
        if theme is not None:
            if theme not in ("light", "dark"):
                return error_response(400, "VALIDATION_ERROR", "Theme must be light or dark")
            preference.theme = theme
        if records_per_page is not None:
            if not isinstance(records_per_page, int) or not 10 <= records_per_page <= 100:
                return error_response(400, "VALIDATION_ERROR", "records_per_page must be between 10 and 100")
            preference.records_per_page = records_per_page
        db.session.add(preference)
        db.session.commit()
        return jsonify(success=True, data=user_preferences_payload(preference)), 200

    @app.get("/api/patients")
    @require_auth
    def get_patients():
        search = request.args.get("search", "").strip().lower()
        status = request.args.get("status", "").strip().lower()
        report_status = request.args.get("report_status", "").strip().lower()
        modality = request.args.get("modality", "").strip().lower()
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "20"))
        except ValueError:
            return error_response(400, "VALIDATION_ERROR", "page and per_page must be integers")
        if page < 1 or not 1 <= per_page <= 100:
            return error_response(400, "VALIDATION_ERROR", "page must be at least 1 and per_page must be between 1 and 100")
        parsed_from = parse_calendar_bound(date_from) if date_from else None
        parsed_to = parse_calendar_bound(date_to, inclusive_end_date=True) if date_to else None
        if (date_from and parsed_from is None) or (date_to and parsed_to is None):
            return error_response(400, "VALIDATION_ERROR", "date_from and date_to must be dates or time-zone-aware timestamps")
        if parsed_from and parsed_to and parsed_from >= parsed_to:
            return error_response(400, "VALIDATION_ERROR", "date_from must be on or before date_to")
        query = visible_scans(Scan.query.join(Patient), request.current_user)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                db.or_(func.lower(Patient.first_name).like(pattern), func.lower(Patient.last_name).like(pattern),
                       func.lower(Patient.medical_record_number).like(pattern), func.lower(Scan.study_uid).like(pattern))
            )
        if status:
            query = query.filter(func.lower(Scan.status) == status)
        if report_status:
            query = query.filter(func.lower(Scan.report_status) == report_status)
        if modality:
            query = query.filter(func.lower(Scan.modality) == modality)
        if parsed_from:
            query = query.filter(Scan.scheduled_at >= parsed_from)
        if parsed_to:
            query = query.filter(Scan.scheduled_at < parsed_to)
        total = query.count()
        scans = query.order_by(Scan.scheduled_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        return jsonify(success=True, total=total, data=[scan_payload(scan) for scan in scans], pagination={
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        }), 200

    @app.get("/api/patients/<patient_id>")
    @require_auth
    def get_patient(patient_id):
        patient = tenant_row(Patient, patient_id)
        if patient is None:
            return error_response(404, "PATIENT_NOT_FOUND", "The patient was not found")
        if reporting_role(request.current_user) and not any(may_view_scan(scan, request.current_user) for scan in patient.scans):
            return error_response(404, "PATIENT_NOT_FOUND", "The patient was not found")
        return jsonify(success=True, data=patient_payload(patient, request.current_user)), 200

    @app.post("/api/patients/<patient_id>/scans")
    @require_permission("scans.write")
    def schedule_scan(patient_id):
        patient = tenant_row(Patient, patient_id)
        if patient is None:
            return error_response(404, "PATIENT_NOT_FOUND", "The patient was not found")
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return error_response(400, "VALIDATION_ERROR", "Request body must be a JSON object")
        modality = body.get("modality")
        body_part = body.get("body_part")
        scheduled_at = parse_future_scan_time(body.get("scheduled_at"))
        if not isinstance(modality, str) or modality not in {"CT", "MRI", "XR", "US"}:
            return error_response(400, "VALIDATION_ERROR", "Select a supported modality")
        if not isinstance(body_part, str) or not body_part.strip() or len(body_part.strip()) > 80:
            return error_response(400, "VALIDATION_ERROR", "body_part must contain 1 to 80 characters")
        if scheduled_at is None:
            return error_response(400, "VALIDATION_ERROR", "scheduled_at must be a future date and time with a time zone")
        scan_id = uuid4()
        scan = Scan(
            id=str(scan_id), tenant_id=request.current_user.tenant_id, patient=patient, study_uid=f"2.25.{scan_id.int}",
            accession_number=f"ACC-{uuid4().hex[:20].upper()}", modality=modality,
            body_part=body_part.strip(), status="Scheduled", scheduled_at=scheduled_at,
        )
        db.session.add(scan)
        db.session.add(AuditEvent(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, actor=request.current_user, action="scan.scheduled",
            entity_type="scan", entity_id=scan.id,
            metadata_json={"patient_id": patient.id, "scheduled_at": utc_isoformat(scheduled_at)},
        ))
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 201

    @app.patch("/api/scans/<scan_id>/schedule")
    @require_permission("scans.write")
    def reschedule_scan(scan_id):
        scan = tenant_row(Scan, scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if scan.status != "Scheduled" or scan.performed_at is not None:
            return error_response(400, "VALIDATION_ERROR", "Only unperformed Scheduled scans can be rescheduled")
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return error_response(400, "VALIDATION_ERROR", "Request body must be a JSON object")
        scheduled_at = parse_future_scan_time(body.get("scheduled_at"))
        if scheduled_at is None:
            return error_response(400, "VALIDATION_ERROR", "scheduled_at must be a future date and time with a time zone")
        previous_time = utc_isoformat(scan.scheduled_at)
        scan.scheduled_at = scheduled_at
        db.session.add(AuditEvent(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, actor=request.current_user, action="scan.rescheduled",
            entity_type="scan", entity_id=scan.id,
            metadata_json={"previous_scheduled_at": previous_time, "scheduled_at": utc_isoformat(scheduled_at)},
        ))
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.get("/api/scans/calendar")
    @require_permission("scans.read")
    def scan_calendar():
        start_value = request.args.get("start", "").strip()
        end_value = request.args.get("end", "").strip()
        start = parse_calendar_bound(start_value)
        end = parse_calendar_bound(end_value, inclusive_end_date=True)
        if not start or not end or start >= end:
            return error_response(400, "VALIDATION_ERROR", "Valid start and end dates with an end after start are required")
        scans = (visible_scans(Scan.query.join(Patient), request.current_user)
                 .filter(Scan.scheduled_at.isnot(None), Scan.scheduled_at >= start, Scan.scheduled_at < end)
                 .order_by(Scan.scheduled_at.asc()).all())
        events = [{
            "id": scan.id,
            "patient_id": scan.patient_id,
            "patient_name": scan.patient.full_name,
            "scheduled_at": utc_isoformat(scan.scheduled_at),
            "modality": scan.modality,
            "body_part": scan.body_part,
            "status": scan.status,
        } for scan in scans]
        return jsonify(success=True, total=len(events), data=events), 200

    @app.patch("/api/scans/<scan_id>/status")
    @require_permission("scans.write")
    def update_scan_status(scan_id):
        scan = tenant_row(Scan, scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        if scan.report_status == "Signed":
            return error_response(409, "WORKFLOW_CONFLICT", "A signed study cannot be changed through scan status")
        body = request.get_json(silent=True) or {}
        status = body.get("status")
        allowed_statuses = {"Scheduled", "In Progress", "Pending Review", "Cancelled"}
        if status not in allowed_statuses:
            return error_response(400, "VALIDATION_ERROR", "status must be a supported scan status")
        previous_status = scan.status
        scan.status = status
        if status == "Pending Review" and scan.performed_at is None:
            scan.performed_at = datetime.now(timezone.utc)
        db.session.add(AuditEvent(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, actor=request.current_user, action="scan.status_updated",
            entity_type="scan", entity_id=scan.id,
            metadata_json={"previous_status": previous_status, "status": status},
        ))
        db.session.commit()
        return jsonify(success=True, data=scan_payload(scan)), 200

    @app.put("/api/patients/<patient_id>")
    @require_permission("patients.write")
    def update_patient(patient_id):
        patient = tenant_row(Patient, patient_id)
        if patient is None:
            return error_response(404, "PATIENT_NOT_FOUND", "The patient was not found")
        body = request.get_json(silent=True) or {}
        allowed_fields = ("first_name", "last_name", "gender", "blood_type", "phone", "email", "medical_record_number")
        for field in allowed_fields:
            if field in body:
                value = body[field]
                if value is not None and not isinstance(value, str):
                    return error_response(400, "VALIDATION_ERROR", f"{field} must be a string")
                value = value.strip() if isinstance(value, str) else value
                if field in ("first_name", "last_name", "medical_record_number") and not value:
                    return error_response(400, "VALIDATION_ERROR", f"{field} cannot be empty")
                setattr(patient, field, value)
        if "date_of_birth" in body:
            try:
                patient.date_of_birth = date.fromisoformat(body["date_of_birth"])
            except (TypeError, ValueError):
                return error_response(400, "VALIDATION_ERROR", "date_of_birth must use YYYY-MM-DD format")
        db.session.add(AuditEvent(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, actor=request.current_user, action="patient.updated",
            entity_type="patient", entity_id=patient.id,
            metadata_json={"updated_fields": sorted(set(body) & set(allowed_fields + ("date_of_birth",)))},
        ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return error_response(400, "VALIDATION_ERROR", "Patient information conflicts with an existing record")
        return jsonify(success=True, data=patient_payload(patient, request.current_user)), 200

    @app.get("/api/admin/roles")
    @require_admin
    def admin_roles():
        roles = Role.query.order_by(Role.name).all()
        return jsonify(success=True, data=[{
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "permissions": sorted(permission.code for permission in role.permissions),
        } for role in roles]), 200

    @app.get("/api/admin/users")
    @require_admin
    def admin_users():
        users = User.query.filter_by(tenant_id=request.current_user.tenant_id).order_by(User.name).all()
        return jsonify(success=True, total=len(users), data=[public_user(user) for user in users]), 200

    @app.post("/api/admin/users")
    @require_admin
    def create_user():
        from werkzeug.security import generate_password_hash

        body = request.get_json(silent=True) or {}
        if "tenant_id" in body:
            return error_response(400, "VALIDATION_ERROR", "Tenant is determined by the signed-in administrator")
        name = body.get("name")
        email = body.get("email")
        password = body.get("password")
        role_id = body.get("role_id")
        modalities = body.get("reporting_modalities", ["CT", "MRI", "XR", "US"])
        if not isinstance(modalities, list) or not modalities or any(value not in ("CT", "MRI", "XR", "US") for value in modalities):
            return error_response(400, "VALIDATION_ERROR", "Select at least one valid reporting modality")
        if not all(isinstance(value, str) and value.strip() for value in (name, email, password, role_id)):
            return error_response(400, "VALIDATION_ERROR", "Name, email, password, and role are required")
        if len(password) < 8:
            return error_response(400, "VALIDATION_ERROR", "Password must be at least 8 characters")
        normalized_email = email.strip().lower()
        if User.query.filter(func.lower(User.email) == normalized_email).first():
            return error_response(400, "EMAIL_ALREADY_EXISTS", "This email cannot be used for another account")
        role = db.session.get(Role, role_id)
        if role is None:
            return error_response(400, "INVALID_ROLE", "The selected role does not exist")
        user = User(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, name=name.strip(), email=normalized_email,
            password_hash=generate_password_hash(password), roles=[role],
            reporting_modalities=",".join(dict.fromkeys(modalities)),
        )
        db.session.add(user)
        db.session.add(AuditEvent(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, actor=request.current_user, action="user.created",
            entity_type="user", entity_id=user.id, metadata_json={"role": role.name},
        ))
        db.session.commit()
        return jsonify(success=True, data=public_user(user)), 201

    @app.put("/api/admin/users/<user_id>")
    @require_admin
    def update_user(user_id):
        from werkzeug.security import generate_password_hash

        user = tenant_row(User, user_id)
        if user is None:
            return error_response(404, "USER_NOT_FOUND", "The user was not found")
        body = request.get_json(silent=True) or {}
        if "tenant_id" in body:
            return error_response(400, "VALIDATION_ERROR", "Users cannot be moved between tenants")
        if "reporting_modalities" in body:
            modalities = body["reporting_modalities"]
            if not isinstance(modalities, list) or not modalities or any(value not in ("CT", "MRI", "XR", "US") for value in modalities):
                return error_response(400, "VALIDATION_ERROR", "Select at least one valid reporting modality")
            user.reporting_modalities = ",".join(dict.fromkeys(modalities))
        if "name" in body:
            if not isinstance(body["name"], str) or not body["name"].strip():
                return error_response(400, "VALIDATION_ERROR", "Name cannot be empty")
            user.name = body["name"].strip()
        if "email" in body:
            if not isinstance(body["email"], str) or not body["email"].strip():
                return error_response(400, "VALIDATION_ERROR", "Email cannot be empty")
            email = body["email"].strip().lower()
            existing = User.query.filter(func.lower(User.email) == email, User.id != user.id).first()
            if existing:
                return error_response(400, "EMAIL_ALREADY_EXISTS", "This email cannot be used for another account")
            user.email = email
        if "role_id" in body:
            role = db.session.get(Role, body["role_id"])
            if role is None:
                return error_response(400, "INVALID_ROLE", "The selected role does not exist")
            target_is_admin = any(existing_role.name == "admin" for existing_role in user.roles)
            if target_is_admin and role.name != "admin":
                other_active_admins = User.query.filter(User.tenant_id == request.current_user.tenant_id, User.id != user.id, User.is_active.is_(True)).all()
                if not any(any(existing_role.name == "admin" for existing_role in admin.roles) for admin in other_active_admins):
                    return error_response(400, "LAST_ADMIN_REQUIRED", "At least one active administrator must remain")
            user.roles = [role]
        if "is_active" in body:
            if not isinstance(body["is_active"], bool):
                return error_response(400, "VALIDATION_ERROR", "is_active must be a boolean")
            if user.id == request.current_user.id and not body["is_active"]:
                return error_response(400, "VALIDATION_ERROR", "You cannot deactivate your own account")
            if not body["is_active"] and any(existing_role.name == "admin" for existing_role in user.roles):
                other_active_admins = User.query.filter(User.tenant_id == request.current_user.tenant_id, User.id != user.id, User.is_active.is_(True)).all()
                if not any(any(existing_role.name == "admin" for existing_role in admin.roles) for admin in other_active_admins):
                    return error_response(400, "LAST_ADMIN_REQUIRED", "At least one active administrator must remain")
            user.is_active = body["is_active"]
        if "password" in body:
            if not isinstance(body["password"], str) or len(body["password"]) < 8:
                return error_response(400, "VALIDATION_ERROR", "Password must be at least 8 characters")
            user.password_hash = generate_password_hash(body["password"])
        db.session.add(AuditEvent(
            id=os.urandom(16).hex(), tenant_id=request.current_user.tenant_id, actor=request.current_user, action="user.updated",
            entity_type="user", entity_id=user.id,
            metadata_json={"updated_fields": sorted(body.keys())},
        ))
        db.session.commit()
        return jsonify(success=True, data=public_user(user)), 200

    @app.get("/api/dashboard/summary")
    @require_auth
    def dashboard_summary():
        scoped = visible_scans(Scan.query, request.current_user)
        modality_counts = dict(scoped.with_entities(Scan.modality, func.count(Scan.id)).group_by(Scan.modality).all())
        return jsonify(success=True, data={
            "total_patients": scoped.with_entities(Scan.patient_id).distinct().count(),
            "completed_studies": scoped.filter(Scan.status == "Completed").count(),
            "pending_review": scoped.filter(Scan.status == "Pending Review").count(),
            "modality_counts": modality_counts,
        }), 200

    @app.get("/api/appointments")
    @require_auth
    def appointments():
        rows = Appointment.query.filter_by(tenant_id=request.current_user.tenant_id).order_by(Appointment.scheduled_for.asc()).limit(50).all()
        return jsonify(success=True, total=len(rows), data=[{
            "id": row.id, "patient_id": row.patient_id, "patient_name": row.patient.full_name,
            "scheduled_for": utc_isoformat(row.scheduled_for), "type": row.appointment_type, "status": row.status,
        } for row in rows]), 200

    @app.get("/api/notifications")
    @require_auth
    def notifications():
        rows = Notification.query.filter_by(tenant_id=request.current_user.tenant_id, user_id=request.current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
        return jsonify(success=True, total=len(rows), data=[{
            "id": row.id, "title": row.title, "message": row.message, "type": row.notification_type,
            "is_read": row.is_read, "created_at": utc_isoformat(row.created_at),
        } for row in rows]), 200


def register_commands(app):
    @app.cli.command("upgrade-reporting")
    def upgrade_reporting_command():
        """Add reporting columns and roles to an existing database without deleting data."""
        if not inspect(db.engine).has_table("scans"):
            raise click.ClickException("Run flask --app src.database_app init-db first")
        columns = {column["name"] for column in inspect(db.engine).get_columns("scans")}
        additions = {
            "reviewer_id": "VARCHAR(36)", "qa_user_id": "VARCHAR(36)", "signer_id": "VARCHAR(36)",
            "report_status": "VARCHAR(32) NOT NULL DEFAULT 'Unassigned'", "draft_text": "TEXT",
            "review_text": "TEXT", "qa_feedback": "TEXT", "qa_approved_at": "TIMESTAMP WITH TIME ZONE",
            "signed_at": "TIMESTAMP WITH TIME ZONE",
        }
        with db.engine.begin() as connection:
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE scans ADD COLUMN {name} {definition}"))
            for name in ("reviewer_id", "qa_user_id", "signer_id", "report_status"):
                connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_scans_{name} ON scans ({name})"))
            user_columns = {column["name"] for column in inspect(db.engine).get_columns("users")}
            if "reporting_modalities" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN reporting_modalities VARCHAR(80) NOT NULL DEFAULT 'CT,MRI,XR,US'"))
            if db.engine.dialect.name == "postgresql" and "tenant_id" in {column["name"] for column in inspect(connection).get_columns("scans")}:
                from src.tenancy import _add_constraint
                for column, name in (("reviewer_id", "fk_scans_tenant_reviewer"), ("qa_user_id", "fk_scans_tenant_qa"), ("signer_id", "fk_scans_tenant_signer")):
                    _add_constraint(connection, "scans", name, f"FOREIGN KEY (tenant_id, {column}) REFERENCES users(tenant_id, id)")
        db.create_all()
        if Permission.query.filter_by(code="scans.assign").first() is None:
            _new_permission("scans.assign")
        for name, description, codes in (
            ("radiologist", "Draft assigned imaging reports", ("patients.read", "scans.read", "reports.draft", "reports.review")),
            ("qa", "Validate assigned imaging reports", ("patients.read", "scans.read", "reports.qa")),
            ("signer", "Sign approved imaging reports", ("patients.read", "scans.read", "reports.sign")),
        ):
            role = Role.query.filter_by(name=name).first()
            if role is None:
                role = Role(id=str(uuid4()), name=name, description=description)
                db.session.add(role)
            role.description = description
            role.permissions = [
                Permission.query.filter_by(code=code).first() or _new_permission(code)
                for code in codes
            ]
        admin = Role.query.filter_by(name="admin").first()
        if admin:
            admin.permissions = Permission.query.all() + [item for item in db.session.new if isinstance(item, Permission)]
        db.session.query(Scan).filter(Scan.reported_at.isnot(None)).update({
            Scan.report_status: "Signed", Scan.draft_text: Scan.report_summary,
            Scan.qa_approved_at: Scan.reported_at, Scan.signed_at: Scan.reported_at,
        }, synchronize_session=False)
        db.session.commit()
        click.echo("Reporting schema and roles upgraded. Existing reports were preserved.")

    @app.cli.command("init-db")
    def init_db_command():
        """Create all PostgreSQL tables. This never deletes existing data."""
        db.create_all()
        if db.engine.dialect.name == "postgresql":
            from src.tenancy import ensure_tenant_links
            with db.engine.begin() as connection:
                ensure_tenant_links(connection)
        click.echo("Database schema created.")

    @app.cli.command("seed-demo-data")
    def seed_command():
        """Seed 75 fictional patients and related records into an empty database."""
        summary = seed_demo_data()
        click.echo(f"Seeded {summary['users']} users, {summary['patients']} patients, and {summary['scans']} scans.")

    @app.cli.command("reset-demo-data")
    @click.confirmation_option(prompt="This deletes all application data. Continue?")
    def reset_command():
        """Explicitly replace existing data with the fictional training dataset."""
        db.drop_all()
        db.create_all()
        if db.engine.dialect.name == "postgresql":
            from src.tenancy import ensure_tenant_links
            with db.engine.begin() as connection:
                ensure_tenant_links(connection)
        summary = seed_demo_data()
        click.echo(f"Reset complete: {summary['patients']} patients and {summary['scans']} scans.")


app = create_app()


def _new_permission(code):
    permission = Permission(id=str(uuid4()), code=code, description=code.replace(".", " ").capitalize())
    db.session.add(permission)
    return permission
