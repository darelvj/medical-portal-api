"""Tenant onboarding and additive PostgreSQL migration for legacy demo data."""

import re
from uuid import uuid4

import click
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from src.models import AssignmentCursor, Role, Tenant, User, db


TENANT_TABLES = ("users", "patients", "scans", "appointments", "notifications", "audit_events")
TENANT_LINKS = (
    ("scans", "patient_id", "patients", "fk_scans_tenant_patient"),
    ("scans", "reporting_clinician_id", "users", "fk_scans_tenant_radiologist"),
    ("scans", "reviewer_id", "users", "fk_scans_tenant_reviewer"),
    ("scans", "qa_user_id", "users", "fk_scans_tenant_qa"),
    ("scans", "signer_id", "users", "fk_scans_tenant_signer"),
    ("appointments", "patient_id", "patients", "fk_appointments_tenant_patient"),
    ("appointments", "assigned_user_id", "users", "fk_appointments_tenant_user"),
    ("notifications", "user_id", "users", "fk_notifications_tenant_user"),
    ("audit_events", "actor_id", "users", "fk_audit_tenant_actor"),
    ("tenant_assignment_cursors", "last_user_id", "users", "fk_cursor_tenant_user"),
    ("report_templates", "created_by_id", "users", "fk_report_template_tenant_creator"),
)


def register_tenant_commands(app):
    @app.cli.command("upgrade-tenancy")
    def upgrade_tenancy_command():
        """Move existing data into the demo tenant; never deletes clinical records."""
        if db.engine.dialect.name != "postgresql":
            raise click.ClickException("Legacy tenancy migration requires PostgreSQL. Fresh SQLite test databases already have the new schema.")
        inspector = inspect(db.engine)
        missing = [table for table in TENANT_TABLES if not inspector.has_table(table)]
        if missing:
            raise click.ClickException(f"Missing tables: {', '.join(missing)}. Run init-db and seed-demo-data first.")
        with db.engine.begin() as connection:
            Tenant.__table__.create(bind=connection, checkfirst=True)
            tenant_id = connection.execute(text("SELECT id FROM tenants WHERE slug = 'demo'")).scalar_one_or_none()
            if tenant_id is None:
                tenant_id = "tenant-demo"
                connection.execute(text(
                    "INSERT INTO tenants (id, slug, name, is_active, created_at, updated_at) "
                    "VALUES (:id, 'demo', 'Demo Organization', true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": tenant_id})
            for table in TENANT_TABLES:
                columns = {column["name"] for column in inspect(connection).get_columns(table)}
                if "tenant_id" not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN tenant_id VARCHAR(36)"))
                connection.execute(text(f"UPDATE {table} SET tenant_id = :tenant_id WHERE tenant_id IS NULL"), {"tenant_id": tenant_id})
                connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL"))
                connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)"))
                _add_constraint(connection, table, f"fk_{table}_tenant", f"FOREIGN KEY (tenant_id) REFERENCES tenants(id)")

            # Required both for tenant-local uniqueness and composite foreign keys.
            for table in ("users", "patients"):
                connection.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_tenant_id ON {table} (tenant_id, id)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tenant_email ON users (tenant_id, email)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_tenant_mrn ON patients (tenant_id, medical_record_number)"))
            for table, column in (("users", "email"), ("patients", "medical_record_number")):
                metadata = inspect(connection)
                for constraint in metadata.get_unique_constraints(table):
                    if constraint.get("column_names") == [column] and constraint.get("name"):
                        name = connection.dialect.identifier_preparer.quote(constraint["name"])
                        connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
                for index in inspect(connection).get_indexes(table):
                    if index.get("unique") and index.get("column_names") == [column] and not index.get("duplicates_constraint"):
                        connection.execute(text(f"DROP INDEX IF EXISTS {connection.dialect.identifier_preparer.quote(index['name'])}"))

            ensure_unique_login_emails(connection)

            AssignmentCursor.__table__.create(bind=connection, checkfirst=True)
            ensure_tenant_links(connection)
        click.echo("Tenant migration complete. Existing records belong to the demo tenant. Old login cookies must sign in again.")

    @app.cli.command("create-tenant")
    @click.option("--slug", required=True, help="Organization login slug, e.g. clinic-b")
    @click.option("--name", required=True, help="Organization display name")
    @click.option("--admin-email", required=True, help="First tenant administrator email")
    @click.option("--admin-name", required=True, help="First tenant administrator name")
    @click.password_option()
    def create_tenant_command(slug, name, admin_email, admin_name, password):
        """Provision an empty tenant and its first admin; no cross-tenant API exists."""
        slug = slug.strip().lower()
        email = admin_email.strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", slug):
            raise click.ClickException("Slug must be 1-64 lowercase letters, numbers, or internal hyphens")
        if not name.strip() or not admin_name.strip() or not email or "@" not in email or len(password) < 8:
            raise click.ClickException("Provide names, a valid email, and a password of at least eight characters")
        if Tenant.query.filter_by(slug=slug).first():
            raise click.ClickException("That organization slug is already in use")
        if User.query.filter(db.func.lower(User.email) == email).first():
            raise click.ClickException("That administrator email is already used by another account")
        admin_role = Role.query.filter_by(name="admin").first()
        if admin_role is None:
            raise click.ClickException("Initialize roles before creating another tenant")
        tenant = Tenant(id=str(uuid4()), slug=slug, name=name.strip())
        user = User(
            id=str(uuid4()), tenant_id=tenant.id, name=admin_name.strip(), email=email,
            password_hash=generate_password_hash(password), roles=[admin_role],
        )
        db.session.add_all((tenant, user))
        if inspect(db.engine).has_table("report_templates"):
            from src.report_templates import seed_starter_templates
            seed_starter_templates(tenant.id)
        db.session.commit()
        click.echo(f"Created tenant {slug} and its first administrator. No patient data was copied.")

    @app.cli.command("upgrade-login-email")
    def upgrade_login_email_command():
        """Enforce a single case-insensitive login identity across tenants."""
        if db.engine.dialect.name != "postgresql":
            raise click.ClickException("This migration requires PostgreSQL")
        with db.engine.begin() as connection:
            ensure_unique_login_emails(connection)
        click.echo("Login emails are globally unique. Users now sign in without an organization field.")


def ensure_unique_login_emails(connection):
    if connection.dialect.name != "postgresql":
        return
    duplicates = connection.execute(text(
        "SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*) > 1 LIMIT 5"
    )).scalars().all()
    if duplicates:
        raise click.ClickException(
            "Duplicate login emails exist across tenants. Change one account's email before migrating: "
            + ", ".join(duplicates)
        )
    connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_login_email ON users (lower(email))"))


def _add_constraint(connection, table, name, definition):
    exists = connection.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = :name AND conrelid = to_regclass(:table)"
    ), {"name": name, "table": table}).first()
    if not exists:
        connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}"))


def ensure_tenant_links(connection):
    """Enforce matching tenant IDs across patient and staff references in PostgreSQL."""
    if connection.dialect.name != "postgresql":
        return
    for table, column, parent, name in TENANT_LINKS:
        if not inspect(connection).has_table(table) or column not in {item["name"] for item in inspect(connection).get_columns(table)}:
            continue
        _add_constraint(connection, table, name, f"FOREIGN KEY (tenant_id, {column}) REFERENCES {parent}(tenant_id, id)")
