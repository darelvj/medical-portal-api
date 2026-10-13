import random
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from werkzeug.security import generate_password_hash

from src.models import Appointment, AuditEvent, Notification, Patient, Permission, Role, Scan, Tenant, User, UserPreference, db


FIRST_NAMES = [
    "Avery", "Jordan", "Morgan", "Riley", "Casey", "Taylor", "Cameron", "Alex", "Jamie", "Drew",
    "Robin", "Quinn", "Emery", "Parker", "Reese", "Sasha", "Skyler", "Rowan", "Dakota", "Hayden",
]
LAST_NAMES = [
    "Rivera", "Lee", "Patel", "Morgan", "Nguyen", "Brooks", "Chen", "Turner", "Reed", "Santos",
    "Foster", "Kim", "Walker", "Shaw", "Bennett", "Diaz", "Ellis", "Campbell", "Wright", "Perry",
]
MODALITIES = ["CT", "MRI", "XR", "US"]
BODY_PARTS = {
    "CT": ["Chest", "Abdomen", "Head", "Spine"],
    "MRI": ["Brain", "Knee", "Spine", "Shoulder"],
    "XR": ["Chest", "Hand", "Knee", "Pelvis"],
    "US": ["Abdomen", "Thyroid", "Pelvis", "Vascular"],
}
SCAN_STATUSES = ["Completed", "Pending Review", "In Progress", "Scheduled", "Cancelled"]


def new_id():
    return str(uuid4())


def seed_demo_data():
    """Create a deterministic, entirely fictional training dataset."""
    if User.query.count() > 0:
        raise RuntimeError("Database already contains data. Use the reset command only when you intend to replace demo data.")

    tenant = Tenant.query.filter_by(slug="demo").first()
    if tenant is None:
        tenant = Tenant(id="tenant-demo", slug="demo", name="Demo Organization")
        db.session.add(tenant)

    permissions = {
        code: Permission(id=new_id(), code=code, description=description)
        for code, description in {
            "patients.read": "View patient records",
            "patients.write": "Create and update patient records",
            "scans.read": "View imaging studies",
            "scans.write": "Create and update imaging studies",
            "appointments.read": "View appointments",
            "appointments.write": "Manage appointments",
            "users.read": "View users",
            "users.write": "Manage users and roles",
            "reports.sign": "Sign imaging reports",
            "reports.draft": "Draft assigned imaging reports",
            "reports.qa": "Quality-check assigned imaging reports",
            "reports.review": "Provide a second opinion on assigned reports",
            "scans.assign": "Assign reporting work",
        }.items()
    }
    db.session.add_all(permissions.values())

    clinician_role = Role(id=new_id(), name="clinician", description="Clinical user with patient and scan access")
    radiologist_role = Role(id=new_id(), name="radiologist", description="Draft assigned imaging reports")
    qa_role = Role(id=new_id(), name="qa", description="Validate draft imaging reports")
    signer_role = Role(id=new_id(), name="signer", description="Sign approved imaging reports")
    technician_role = Role(id=new_id(), name="technician", description="Imaging technician")
    scheduler_role = Role(id=new_id(), name="scheduler", description="Appointment coordinator")
    admin_role = Role(id=new_id(), name="admin", description="Application administrator")
    clinician_role.permissions = [permissions[code] for code in ("patients.read", "scans.read", "appointments.read")]
    radiologist_role.permissions = [permissions[code] for code in ("patients.read", "scans.read", "reports.draft", "reports.review")]
    qa_role.permissions = [permissions[code] for code in ("patients.read", "scans.read", "reports.qa")]
    signer_role.permissions = [permissions[code] for code in ("patients.read", "scans.read", "reports.sign")]
    technician_role.permissions = [permissions[code] for code in ("patients.read", "scans.read", "scans.write")]
    scheduler_role.permissions = [permissions[code] for code in ("patients.read", "appointments.read", "appointments.write")]
    admin_role.permissions = list(permissions.values())
    db.session.add_all([clinician_role, radiologist_role, qa_role, signer_role, technician_role, scheduler_role, admin_role])

    users = [
        User(id="user-001", tenant_id=tenant.id, name="Demo Clinician", email="clinician@example.test", password_hash=generate_password_hash("demo-password"), roles=[clinician_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Dr. Avery Stone", email="avery.stone@example.test", password_hash=generate_password_hash("demo-password"), roles=[radiologist_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Dr. Reese Chen", email="reese.chen@example.test", password_hash=generate_password_hash("demo-password"), roles=[radiologist_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Taylor QA", email="qa@example.test", password_hash=generate_password_hash("demo-password"), roles=[qa_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Dr. Morgan Signer", email="signer@example.test", password_hash=generate_password_hash("demo-password"), roles=[signer_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Jordan Kim", email="jordan.kim@example.test", password_hash=generate_password_hash("demo-password"), roles=[technician_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Morgan Hale", email="morgan.hale@example.test", password_hash=generate_password_hash("demo-password"), roles=[scheduler_role]),
        User(id=new_id(), tenant_id=tenant.id, name="Portal Administrator", email="admin@example.test", password_hash=generate_password_hash("demo-password"), roles=[admin_role]),
    ]
    for user in users:
        user.preference = UserPreference(theme="dark", records_per_page=24)
    db.session.add_all(users)

    rng = random.Random(20260930)
    now = datetime.now(timezone.utc)
    patients = []
    scans = []
    for index in range(1, 76):
        birth_year = rng.randint(1940, 2005)
        patient = Patient(
            id=f"patient-{index:03d}",
            tenant_id=tenant.id,
            medical_record_number=f"MRN-{20260000 + index}",
            first_name=FIRST_NAMES[(index - 1) % len(FIRST_NAMES)],
            last_name=LAST_NAMES[(index * 3) % len(LAST_NAMES)],
            date_of_birth=date(birth_year, rng.randint(1, 12), rng.randint(1, 28)),
            gender=rng.choice(["Female", "Male", "Non-binary"]),
            blood_type=rng.choice(["A+", "A-", "B+", "B-", "AB+", "O+", "O-"]),
            phone=f"+1-555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
            email=f"patient{index:03d}@example.test",
        )
        patients.append(patient)
        db.session.add(patient)

        for study_number in range(1, rng.randint(2, 4)):
            modality = rng.choice(MODALITIES)
            status = rng.choices(SCAN_STATUSES, weights=[50, 22, 12, 11, 5], k=1)[0]
            scheduled_at = now - timedelta(days=rng.randint(-30, 360), hours=rng.randint(0, 23))
            performed_at = scheduled_at + timedelta(minutes=rng.randint(20, 100)) if status not in ("Scheduled", "Cancelled") else None
            reported_at = performed_at + timedelta(hours=rng.randint(2, 96)) if status == "Completed" and performed_at else None
            scan = Scan(
                id=new_id(),
                tenant_id=tenant.id,
                patient=patient,
                study_uid=f"2.25.20260930{index:03d}{study_number:02d}",
                accession_number=f"ACC-{20260000 + index}-{study_number}",
                modality=modality,
                body_part=rng.choice(BODY_PARTS[modality]),
                status=status,
                scheduled_at=scheduled_at,
                performed_at=performed_at,
                reported_at=reported_at,
                report_summary="Fictional training record. No clinical interpretation is provided." if reported_at else None,
                reporting_clinician=users[1] if reported_at else None,
                report_status="Signed" if reported_at else "Unassigned",
                draft_text="Fictional training record. No clinical interpretation is provided." if reported_at else None,
                signer=users[4] if reported_at else None,
                qa_user=users[3] if reported_at else None,
                qa_approved_at=reported_at if reported_at else None,
                signed_at=reported_at if reported_at else None,
            )
            scans.append(scan)
            db.session.add(scan)

        if index % 3 == 0:
            db.session.add(Appointment(
                id=new_id(), tenant_id=tenant.id, patient=patient, assigned_user=users[6],
                scheduled_for=now + timedelta(days=rng.randint(1, 45), hours=rng.randint(8, 16)),
                appointment_type=rng.choice(["Imaging follow-up", "Consultation", "Scan preparation"]),
                status=rng.choice(["Scheduled", "Confirmed"]),
            ))

    for index in range(1, 9):
        db.session.add(Notification(
            id=new_id(), tenant_id=tenant.id, user=users[0], title="Study needs attention",
            message=f"Fictional imaging study queue item {index} is awaiting review.",
            notification_type="scan_review", is_read=index > 3,
        ))
    db.session.add(AuditEvent(
        id=new_id(), tenant_id=tenant.id, actor=users[7], action="seed.created", entity_type="database",
        metadata_json={"patients": len(patients), "scans": len(scans), "fictional_data": True},
    ))
    from src.report_templates import seed_starter_templates
    seed_starter_templates(tenant.id)
    db.session.commit()
    return {"patients": len(patients), "scans": len(scans), "users": len(users)}
