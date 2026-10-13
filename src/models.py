from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    db.Column("role_id", db.String(36), db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.String(36), db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    db.Column("permission_id", db.String(36), db.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False,
    )


class Tenant(db.Model, TimestampMixin):
    __tablename__ = "tenants"

    id = db.Column(db.String(36), primary_key=True)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)
    name = db.Column(db.String(160), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class Role(db.Model, TimestampMixin):
    __tablename__ = "roles"

    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True, index=True)
    description = db.Column(db.String(255), nullable=False)
    permissions = db.relationship("Permission", secondary=role_permissions, back_populates="roles")


class Permission(db.Model, TimestampMixin):
    __tablename__ = "permissions"

    id = db.Column(db.String(36), primary_key=True)
    code = db.Column(db.String(120), nullable=False, unique=True, index=True)
    description = db.Column(db.String(255), nullable=False)
    roles = db.relationship("Role", secondary=role_permissions, back_populates="permissions")


class User(db.Model, TimestampMixin):
    __tablename__ = "users"

    __table_args__ = (
        db.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        db.UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id"),
    )

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    reporting_modalities = db.Column(db.String(80), nullable=False, default="CT,MRI,XR,US")
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    roles = db.relationship("Role", secondary=user_roles, backref="users")
    preference = db.relationship("UserPreference", back_populates="user", uselist=False, cascade="all, delete-orphan")
    scans = db.relationship("Scan", back_populates="reporting_clinician", foreign_keys="Scan.reporting_clinician_id")
    audit_events = db.relationship("AuditEvent", back_populates="actor")
    tenant = db.relationship("Tenant")


# Email is the login identifier, so it must identify one user across tenants.
db.Index("uq_users_login_email", db.func.lower(User.email), unique=True)


class UserPreference(db.Model, TimestampMixin):
    __tablename__ = "user_preferences"

    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    theme = db.Column(db.String(20), nullable=False, default="dark")
    records_per_page = db.Column(db.Integer, nullable=False, default=24)
    user = db.relationship("User", back_populates="preference")


class Patient(db.Model, TimestampMixin):
    __tablename__ = "patients"

    __table_args__ = (
        db.UniqueConstraint("tenant_id", "medical_record_number", name="uq_patients_tenant_mrn"),
        db.UniqueConstraint("tenant_id", "id", name="uq_patients_tenant_id"),
    )

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    medical_record_number = db.Column(db.String(32), nullable=False, index=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    gender = db.Column(db.String(40), nullable=False)
    blood_type = db.Column(db.String(8), nullable=True)
    phone = db.Column(db.String(32), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    scans = db.relationship("Scan", back_populates="patient", cascade="all, delete-orphan")
    appointments = db.relationship("Appointment", back_populates="patient", cascade="all, delete-orphan")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Scan(db.Model, TimestampMixin):
    __tablename__ = "scans"

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    patient_id = db.Column(db.String(36), db.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    study_uid = db.Column(db.String(128), nullable=False, unique=True, index=True)
    accession_number = db.Column(db.String(40), nullable=False, unique=True, index=True)
    modality = db.Column(db.String(16), nullable=False, index=True)
    body_part = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(32), nullable=False, index=True)
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    performed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    reported_at = db.Column(db.DateTime(timezone=True), nullable=True)
    report_summary = db.Column(db.Text, nullable=True)
    reporting_clinician_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)
    reviewer_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    qa_user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    signer_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    report_status = db.Column(db.String(32), nullable=False, default="Unassigned", index=True)
    draft_text = db.Column(db.Text, nullable=True)
    report_sections = db.Column(db.JSON, nullable=True)
    review_text = db.Column(db.Text, nullable=True)
    qa_feedback = db.Column(db.Text, nullable=True)
    qa_approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    signed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    tat_manual_minutes = db.Column(db.Integer, nullable=True)
    tat_target_minutes = db.Column(db.Integer, nullable=True)
    tat_note = db.Column(db.String(500), nullable=True)
    patient = db.relationship("Patient", back_populates="scans")
    reporting_clinician = db.relationship("User", back_populates="scans", foreign_keys=[reporting_clinician_id])
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])
    qa_user = db.relationship("User", foreign_keys=[qa_user_id])
    signer = db.relationship("User", foreign_keys=[signer_id])


class ReportTemplate(db.Model, TimestampMixin):
    __tablename__ = "report_templates"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "modality", "name", name="uq_report_templates_tenant_modality_name"),
    )

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    created_by_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)
    name = db.Column(db.String(80), nullable=False)
    modality = db.Column(db.String(16), nullable=False, index=True)
    sections = db.Column(db.JSON, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class AssignmentCursor(db.Model):
    __tablename__ = "tenant_assignment_cursors"

    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), primary_key=True)
    modality = db.Column(db.String(16), primary_key=True)
    last_user_id = db.Column(db.String(36), nullable=True)


class Appointment(db.Model, TimestampMixin):
    __tablename__ = "appointments"

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    patient_id = db.Column(db.String(36), db.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    assigned_user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)
    scheduled_for = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    appointment_type = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(32), nullable=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    patient = db.relationship("Patient", back_populates="appointments")
    assigned_user = db.relationship("User")


class Notification(db.Model, TimestampMixin):
    __tablename__ = "notifications"

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    notification_type = db.Column(db.String(40), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    user = db.relationship("User")


class AuditEvent(db.Model, TimestampMixin):
    __tablename__ = "audit_events"

    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.String(36), db.ForeignKey("tenants.id"), nullable=False, index=True)
    actor_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(120), nullable=False, index=True)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.String(36), nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)
    actor = db.relationship("User", back_populates="audit_events")
