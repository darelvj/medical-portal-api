"""Tenant-scoped, non-diagnostic report outlines."""

from uuid import uuid4

import click
from flask import jsonify, request
from sqlalchemy import func, inspect
from sqlalchemy.exc import IntegrityError

from src.models import AuditEvent, ReportTemplate, Tenant, db


MODALITIES = {"CT", "MRI", "XR", "US"}
SECTION_KEYS = {"clinical_history", "technique", "findings", "impression"}
STARTER_OUTLINES = {
    "CT": {"name": "CT starter", "sections": {
        "clinical_history": "", "technique": "Acquisition details:\nContrast (if applicable):",
        "findings": "Anatomy reviewed:\nRelevant observations:", "impression": "",
    }},
    "MRI": {"name": "MRI starter", "sections": {
        "clinical_history": "", "technique": "Sequences obtained:\nContrast (if applicable):",
        "findings": "Anatomy reviewed:\nRelevant observations:", "impression": "",
    }},
    "XR": {"name": "X-ray starter", "sections": {
        "clinical_history": "", "technique": "Views obtained:",
        "findings": "Anatomy reviewed:\nRelevant observations:", "impression": "",
    }},
    "US": {"name": "Ultrasound starter", "sections": {
        "clinical_history": "", "technique": "Exam technique and limitations:",
        "findings": "Anatomy reviewed:\nRelevant observations:", "impression": "",
    }},
}


def seed_starter_templates(tenant_id):
    """Add one neutral outline per modality without touching existing templates."""
    for modality, starter in STARTER_OUTLINES.items():
        exists = ReportTemplate.query.filter_by(tenant_id=tenant_id, modality=modality, name=starter["name"]).first()
        if exists is None:
            db.session.add(ReportTemplate(
                id=str(uuid4()), tenant_id=tenant_id, name=starter["name"], modality=modality,
                sections=dict(starter["sections"]),
            ))


def template_payload(template):
    return {
        "id": template.id, "name": template.name, "modality": template.modality,
        "sections": template.sections, "is_active": template.is_active,
    }


def validated_template(body):
    """Return normalized name/modality/sections or a user-safe validation message."""
    name = body.get("name")
    modality = body.get("modality")
    sections = body.get("sections")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        return None, "Template name must contain 1 to 80 characters"
    if not isinstance(modality, str) or modality not in MODALITIES:
        return None, "Choose a supported modality"
    if not isinstance(sections, dict) or set(sections) != SECTION_KEYS or any(
        not isinstance(sections[key], str) or len(sections[key]) > 10000 for key in SECTION_KEYS
    ):
        return None, "Provide the four report sections as text (up to 10000 characters each)"
    normalized = {key: sections[key].strip() for key in sorted(SECTION_KEYS)}
    if not any(normalized.values()) or sum(len(value) for value in normalized.values()) > 18000:
        return None, "Template must contain some text and no more than 18000 characters overall"
    return (name.strip(), modality, normalized), None


def register_report_template_routes(app):
    from src.database_app import error_response, has_role, require_admin, require_auth

    @app.get("/api/reporting/templates")
    @require_auth
    def list_report_templates():
        user = request.current_user
        if not (has_role(user, "admin") or has_role(user, "radiologist")):
            return error_response(403, "PERMISSION_DENIED", "Reporting access is required")
        modality = request.args.get("modality", "").strip().upper()
        if modality and modality not in MODALITIES:
            return error_response(400, "VALIDATION_ERROR", "Choose a supported modality")
        query = ReportTemplate.query.filter_by(tenant_id=user.tenant_id)
        if modality:
            query = query.filter_by(modality=modality)
        if not has_role(user, "admin") or request.args.get("include_inactive") != "1":
            query = query.filter_by(is_active=True)
        templates = query.order_by(ReportTemplate.modality, ReportTemplate.name).all()
        return jsonify(success=True, data=[template_payload(item) for item in templates], total=len(templates)), 200

    @app.post("/api/reporting/templates")
    @require_admin
    def create_report_template():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"name", "modality", "sections"}:
            return error_response(400, "VALIDATION_ERROR", "Provide name, modality, and sections")
        values, problem = validated_template(body)
        if problem:
            return error_response(400, "VALIDATION_ERROR", problem)
        name, modality, sections = values
        duplicate = ReportTemplate.query.filter(
            ReportTemplate.tenant_id == request.current_user.tenant_id,
            ReportTemplate.modality == modality, func.lower(ReportTemplate.name) == name.lower(),
        ).first()
        if duplicate:
            return error_response(409, "TEMPLATE_EXISTS", "A template with this name already exists for this modality")
        template = ReportTemplate(
            id=str(uuid4()), tenant_id=request.current_user.tenant_id, created_by_id=request.current_user.id,
            name=name, modality=modality, sections=sections,
        )
        db.session.add(template)
        db.session.add(AuditEvent(
            id=str(uuid4()), tenant_id=request.current_user.tenant_id, actor_id=request.current_user.id,
            action="report_template.created", entity_type="report_template", entity_id=template.id,
        ))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return error_response(409, "TEMPLATE_EXISTS", "A template with this name already exists for this modality")
        return jsonify(success=True, data=template_payload(template)), 201

    @app.put("/api/reporting/templates/<template_id>")
    @require_admin
    def update_report_template(template_id):
        template = ReportTemplate.query.filter_by(id=template_id, tenant_id=request.current_user.tenant_id).first()
        if template is None:
            return error_response(404, "TEMPLATE_NOT_FOUND", "The template was not found")
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body or set(body) - {"name", "modality", "sections", "is_active"}:
            return error_response(400, "VALIDATION_ERROR", "Provide template fields to update")
        if "is_active" in body and not isinstance(body["is_active"], bool):
            return error_response(400, "VALIDATION_ERROR", "is_active must be a boolean")
        values, problem = validated_template({
            "name": body.get("name", template.name), "modality": body.get("modality", template.modality),
            "sections": body.get("sections", template.sections),
        })
        if problem:
            return error_response(400, "VALIDATION_ERROR", problem)
        name, modality, sections = values
        duplicate = ReportTemplate.query.filter(
            ReportTemplate.tenant_id == request.current_user.tenant_id,
            ReportTemplate.modality == modality, func.lower(ReportTemplate.name) == name.lower(),
            ReportTemplate.id != template.id,
        ).first()
        if duplicate:
            return error_response(409, "TEMPLATE_EXISTS", "A template with this name already exists for this modality")
        template.name, template.modality, template.sections = name, modality, sections
        if "is_active" in body:
            template.is_active = body["is_active"]
        db.session.add(AuditEvent(
            id=str(uuid4()), tenant_id=request.current_user.tenant_id, actor_id=request.current_user.id,
            action="report_template.updated", entity_type="report_template", entity_id=template.id,
            metadata_json={"updated_fields": sorted(body)},
        ))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return error_response(409, "TEMPLATE_EXISTS", "A template with this name already exists for this modality")
        return jsonify(success=True, data=template_payload(template)), 200

    @app.cli.command("upgrade-report-templates")
    def upgrade_report_templates_command():
        """Create template storage and starter outlines for existing tenants."""
        if not inspect(db.engine).has_table("tenants"):
            raise click.ClickException("Run the tenant migration before installing report templates")
        with db.engine.begin() as connection:
            ReportTemplate.__table__.create(bind=connection, checkfirst=True)
            if connection.dialect.name == "postgresql":
                from src.tenancy import ensure_tenant_links
                ensure_tenant_links(connection)
        for tenant in Tenant.query.all():
            seed_starter_templates(tenant.id)
        db.session.commit()
        click.echo("Report templates are ready for all existing tenants.")
