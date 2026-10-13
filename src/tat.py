"""Study turnaround-time metrics and admin MIS reporting.

TAT begins when a study is performed and ends when its report is signed.
Manual durations are explicit overrides; clinical event timestamps remain intact.
"""

from datetime import datetime, timezone
from statistics import median

import click
from flask import jsonify, request
from sqlalchemy import inspect, text

from src.models import Patient, Scan, db


DEFAULT_TARGETS = {"CT": 120, "MRI": 240, "XR": 60, "US": 180}


def utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None or value.utcoffset() is None else value.astimezone(timezone.utc)


def tat_metrics(scan, now=None):
    now = now or datetime.now(timezone.utc)
    start = utc(scan.performed_at)
    end = utc(scan.signed_at or (scan.reported_at if scan.status == "Completed" else None))
    target = scan.tat_target_minutes or DEFAULT_TARGETS.get(scan.modality, 240)
    if scan.tat_manual_minutes is not None:
        minutes, source = scan.tat_manual_minutes, "manual"
    elif start and end and end >= start:
        minutes, source = round((end - start).total_seconds() / 60), "calculated"
    else:
        minutes, source = None, None
    elapsed = round((now - start).total_seconds() / 60) if start and not end and scan.status not in ("Cancelled", "Completed") and scan.report_status != "Signed" else None
    return {
        "minutes": minutes, "source": source, "target_minutes": target,
        "elapsed_minutes": max(elapsed, 0) if elapsed is not None else None,
        "breached": minutes > target if minutes is not None else elapsed > target if elapsed is not None else None,
        "note": scan.tat_note,
    }


def register_tat_routes(app):
    from src.database_app import audit_scan, error_response, parse_calendar_bound, require_admin, tenant_row, utc_isoformat

    @app.get("/api/admin/mis/tat")
    @require_admin
    def tat_report():
        modality = request.args.get("modality", "").strip().upper()
        radiologist_id = request.args.get("radiologist_id", "").strip()
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()
        if modality and modality not in DEFAULT_TARGETS:
            return error_response(400, "VALIDATION_ERROR", "Unsupported modality")
        start = parse_calendar_bound(date_from) if date_from else None
        end = parse_calendar_bound(date_to, inclusive_end_date=True) if date_to else None
        if (date_from and start is None) or (date_to and end is None) or (start and end and start >= end):
            return error_response(400, "VALIDATION_ERROR", "Choose a valid performed-date range")
        query = Scan.query.join(Patient).filter(Scan.tenant_id == request.current_user.tenant_id)
        if modality:
            query = query.filter(Scan.modality == modality)
        if radiologist_id:
            query = query.filter(Scan.reporting_clinician_id == radiologist_id)
        if start:
            query = query.filter(Scan.performed_at >= start)
        if end:
            query = query.filter(Scan.performed_at < end)
        scans = query.order_by(Scan.performed_at.desc().nullslast(), Scan.id).all()
        now = datetime.now(timezone.utc)
        rows = []
        for scan in scans:
            tat = tat_metrics(scan, now)
            rows.append({
                "scan_id": scan.id, "patient_id": scan.patient_id,
                "patient_name": scan.patient.full_name, "accession_number": scan.accession_number,
                "modality": scan.modality, "body_part": scan.body_part,
                "scan_status": scan.status, "report_status": scan.report_status,
                "radiologist_id": scan.reporting_clinician_id,
                "radiologist_name": scan.reporting_clinician.name if scan.reporting_clinician else None,
                "performed_at": utc_isoformat(scan.performed_at),
                "signed_at": utc_isoformat(scan.signed_at or scan.reported_at),
                "tat": tat,
            })
        measured = [row["tat"]["minutes"] for row in rows if row["tat"]["minutes"] is not None]
        on_time = sum(row["tat"]["minutes"] <= row["tat"]["target_minutes"] for row in rows if row["tat"]["minutes"] is not None)
        overdue = sum(row["tat"]["elapsed_minutes"] is not None and row["tat"]["breached"] for row in rows)
        by_modality = {}
        for modality_name in DEFAULT_TARGETS:
            values = [row["tat"]["minutes"] for row in rows if row["modality"] == modality_name and row["tat"]["minutes"] is not None]
            if values:
                by_modality[modality_name] = {"count": len(values), "average_minutes": round(sum(values) / len(values))}
        summary = {
            "studies": len(rows), "measured": len(measured), "pending_overdue": overdue,
            "manual_count": sum(row["tat"]["source"] == "manual" for row in rows),
            "average_minutes": round(sum(measured) / len(measured)) if measured else None,
            "median_minutes": round(median(measured)) if measured else None,
            "on_time_count": on_time,
            "on_time_percent": round(on_time * 100 / len(measured), 1) if measured else None,
            "by_modality": by_modality,
        }
        return jsonify(success=True, data={"summary": summary, "studies": rows}), 200

    @app.patch("/api/admin/scans/<scan_id>/tat")
    @require_admin
    def update_tat(scan_id):
        scan = tenant_row(Scan, scan_id)
        if scan is None:
            return error_response(404, "SCAN_NOT_FOUND", "The scan was not found")
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not (set(body) & {"manual_minutes", "target_minutes"}):
            return error_response(400, "VALIDATION_ERROR", "Provide manual_minutes or target_minutes")
        for key in ("manual_minutes", "target_minutes"):
            if key in body and body[key] is not None and (type(body[key]) is not int or not 1 <= body[key] <= 100000):
                return error_response(400, "VALIDATION_ERROR", f"{key} must be 1 to 100000 minutes or null")
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note.strip()) > 500):
            return error_response(400, "VALIDATION_ERROR", "Note must be at most 500 characters")
        if body.get("manual_minutes") is not None:
            if scan.status != "Completed" and scan.report_status != "Signed":
                return error_response(409, "WORKFLOW_CONFLICT", "Manual TAT is available only for completed reports")
            if not isinstance(note, str) or not note.strip():
                return error_response(400, "VALIDATION_ERROR", "A reason is required for a manual TAT")
        previous = {"manual_minutes": scan.tat_manual_minutes, "target_minutes": scan.tat_target_minutes}
        if "manual_minutes" in body:
            scan.tat_manual_minutes = body["manual_minutes"]
            scan.tat_note = note.strip() if body["manual_minutes"] is not None and isinstance(note, str) else None
        if "target_minutes" in body:
            scan.tat_target_minutes = body["target_minutes"]
        audit_scan(scan, request.current_user, "scan.tat_updated", {
            "previous": previous,
            "current": {"manual_minutes": scan.tat_manual_minutes, "target_minutes": scan.tat_target_minutes},
            "reason": scan.tat_note,
        })
        db.session.commit()
        return jsonify(success=True, data={"scan_id": scan.id, "tat": tat_metrics(scan)}), 200

    @app.cli.command("upgrade-tat")
    def upgrade_tat_command():
        """Add TAT override/target columns without changing existing clinical timestamps."""
        if not inspect(db.engine).has_table("scans"):
            raise click.ClickException("Run flask --app src.server init-db first")
        columns = {column["name"] for column in inspect(db.engine).get_columns("scans")}
        if "report_status" not in columns:
            raise click.ClickException("Run flask --app src.server upgrade-reporting before upgrade-tat")
        with db.engine.begin() as connection:
            for name, definition in (
                ("tat_manual_minutes", "INTEGER"),
                ("tat_target_minutes", "INTEGER"),
                ("tat_note", "VARCHAR(500)"),
            ):
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE scans ADD COLUMN {name} {definition}"))
        click.echo("TAT columns added. Existing scan timestamps were not changed.")
