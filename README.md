# Fictional Medical Portal Flask Backend

Flask API for the companion React medical portal in the sibling `medical-portal` folder. It uses PostgreSQL and fictional training data only. This is a learning project, not a clinical system. https://github.com/darelvj/medical-portal.git.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Replace the placeholder `DATABASE_URL` username/password and set `JWT_SECRET` in `.env` to a long random value. Never commit `.env`, database dumps, or real secrets. `.env.example` intentionally contains placeholders only. The React frontend has a separate `.env.example` for its development proxy; it must not contain the database password or JWT secret.

Set `DATABASE_URL` to your PostgreSQL database. The included format uses the `psycopg` SQLAlchemy driver:

```text
postgresql+psycopg://username:password@127.0.0.1:5432/medical_portal
```

Create the schema and seed the fictional dataset once:

```bash
flask --app src.server init-db
flask --app src.server seed-demo-data
```

`seed-demo-data` creates fictional accounts, 75 patients, 75+ scans with varied modalities/statuses/dates, appointments, notifications, roles, permissions, preferences, and audit events. It refuses to run when data exists. To intentionally replace all application data, use `flask --app src.server reset-demo-data` and confirm the prompt.

To upgrade an existing PostgreSQL database to the tenant-aware model, stop Flask, take a database backup, and run, in this order:

```bash
flask --app src.server upgrade-tenancy
flask --app src.server upgrade-reporting
flask --app src.server upgrade-tat
```

After updating to the structured report editor, stop Flask and add its non-destructive JSON column to an existing database:

```bash
flask --app src.server upgrade-structured-reports
```

To add report templates to an existing database, run this additive command once (safe to rerun):

```bash
flask --app src.server upgrade-report-templates
```

It creates tenant-scoped template storage and neutral CT, MRI, X-ray, and ultrasound starter outlines for each existing tenant. Fresh demo databases seed these outlines automatically. Tenant admins can create, edit, archive, and restore templates in **Report templates**; assigned radiologists can apply an active, matching-modality outline in the report editor. Applying only changes the local editor—the radiologist must review and explicitly save the draft. Templates contain no patient-specific diagnosis.

Fresh databases created with `init-db` include this column. Existing free-text drafts and signed reports are preserved; an assigned radiologist can reorganize a legacy draft in the new editor. Draft sections may be saved partially, but Findings and Impression are required before submission to QA.

The tenancy migration is additive and designed to be rerunnable. The reporting upgrade also backfills legacy report state, so take a backup before running this sequence. Existing records are assigned to the `demo` tenant; no patient data is copied to other tenants. A fresh database created with `init-db` already has the new schema. Do **not** use `reset-demo-data` to migrate existing records. Old login cookies must sign in again. If you already ran `upgrade-tenancy` before email-only login was introduced, also run `flask --app src.server upgrade-login-email` after resolving any duplicate account emails it reports.

Existing databases do not receive newly seeded QA/signer or second-radiologist accounts; create those from **User management** if needed.

## Start

```bash
flask --app src.server run --host 127.0.0.1 --port 5000
```

The API is available at `http://127.0.0.1:5000`. Demo credentials:

```text
email: clinician@example.test
password: demo-password
```

Passwords are stored as hashes. The JWT is returned only as an HttpOnly cookie; React requests use `credentials: "include"`.

## Endpoints

| Method | Endpoint | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/auth/login` | No | Authenticate and set cookie |
| POST | `/api/auth/logout` | No | Remove cookie |
| GET | `/api/auth/me` | Yes | Return current user |
| GET | `/api/patients` | Yes | List, search, and filter patients |
| GET | `/api/patients/<patient_id>` | Yes | Return one patient |
| GET | `/api/dashboard/summary` | Yes | Aggregated patient and scan summary |
| GET | `/api/appointments` | Yes | Upcoming appointments |
| GET | `/api/notifications` | Yes | Current user notifications |
| GET | `/api/preferences` | Yes | Current user portal preferences |
| PUT | `/api/preferences` | Yes | Update `theme` and `records_per_page` |
| GET | `/api/admin/users` | Admin | List accounts and assigned roles |
| POST | `/api/admin/users` | Admin | Create a user and assign a role |
| PUT | `/api/admin/users/<user_id>` | Admin | Update account, role, or active state |
| GET | `/api/admin/roles` | Admin | List roles and permissions |
| PUT | `/api/patients/<patient_id>` | `patients.write` | Update patient demographics and contact data |
| GET | `/api/admin/audit-events` | Admin | Paginated, tenant-scoped study assignment/report/TAT audit timeline |

The seeded admin account is `admin@example.test` with the development password `demo-password`. Change or remove seeded demo credentials before using this service outside local development. User creation requires a password of at least eight characters. The API prevents deactivating or demoting the last active administrator.

## Tenants

Every user belongs to one tenant. Login requires only email and password; the backend finds the unique user account and derives its tenant. Login emails are unique across tenants (case-insensitively). If a legacy database contains duplicate emails, the login fails closed and the migration reports those emails for an administrator to resolve. The HttpOnly token carries the tenant ID, and the API checks it against the current user on every request. Tenant admins can manage only their own users, patients, scans, reporting assignments, calendar, and MIS data. IDs from another tenant return 404. PostgreSQL migration adds tenant ownership columns and same-tenant foreign-key constraints. The frontend clears cached queries when a different organization signs in.

Create another empty tenant and its first admin without copying demo patients:

```bash
flask --app src.server create-tenant --slug clinic-b --name "Clinic B" --admin-email admin@clinic-b.test --admin-name "Clinic B Admin"
```

The CLI prompts for the password. Do not pass passwords as shell arguments. Organization names and slugs are platform-provisioned; tenant admins cannot create or inspect other tenants through the API.

This local project enforces tenant separation in the application and PostgreSQL foreign keys. PostgreSQL row-level security is **not yet enabled**; do not treat this as production-grade clinical isolation or run production request traffic as the `postgres` superuser. [OWASP multi-tenant guidance](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html) recommends an independent database boundary and isolation tests for a production deployment.

## Reporting workflow

An administrator assigns an eligible radiologist, QA user, and signer from the study details panel. An unassigned batch can be allocated from User management using either the fewest-active-reports rule or a per-tenant, per-modality round-robin cursor. Radiologist modality eligibility is editable in User management. Drafting requires a performed study (move its scan status to `Pending Review` first). The assigned radiologist saves a draft and can optionally request a second opinion from a different radiologist. They submit the draft to QA; QA approves or returns it with feedback; the assigned signer signs the approved report, which completes the scan. Report state is separate from scan/scheduling status. Workflow actions are audited.

Radiologists also have a **My worklist** page. `GET /api/reporting/worklist` returns only the signed-in radiologist's tenant-owned primary assignments or second-opinion requests. It supports `role=primary|review`, `stage`, `modality`, `search`, `sort=oldest|newest`, `page`, and `per_page` (up to 50). The default active view omits signed and cancelled primary studies and shows pending second opinions. The worklist links directly to the report editor for editable, performed studies. No new database migration is needed for the worklist.

Radiologists, second reviewers, QA users, and signers see only studies assigned to them in patient search, patient details, the scan calendar, and dashboard counts. Other roles retain their existing visibility. This is a local training workflow, not a clinical reporting system.

Tenant admins can inspect **Audit history** for assignment, report workflow, and TAT changes. `GET /api/admin/audit-events` accepts `category=all|assignment|report|tat`, `search`, `page`, and `per_page` (up to 50). Results are ordered newest first and include actor, study, patient, timestamp, and a safe operational summary. It does not expose free-text reports or raw audit metadata. This is a read-only view over existing audit events; no migration is needed. The demo audit log is not tamper-evident or a production compliance solution.

## TAT and MIS

Study TAT is the elapsed time from `performed_at` to `signed_at` (or legacy `reported_at` for previously completed studies). The default targets are CT 120, MRI 240, XR 60, and US 180 minutes. Admins can override a target per study. For completed reports, they can also enter a manual duration with a required reason; this overrides the displayed TAT but never modifies the source timestamps. Clearing the manual duration restores the calculated value. Unfinished studies with a performed time show elapsed time and whether the target has been exceeded. The admin-only MIS page summarizes measured studies, average/median TAT, on-time percentage, overdue pending studies, and modality averages; filters use performed dates. All persisted timestamps remain UTC.

Patient list filters use `search`, `status`, and `modality` query parameters:

```text
GET /api/patients?search=alex&status=Completed&modality=CT
```

Successful list responses use `{"success": true, "data": [], "total": 0}`. Single resources use `{"success": true, "data": {}}`. Errors use a `success: false` response with an error `code` and `message`.

## React connection

```typescript
fetch("http://127.0.0.1:5000/api/auth/me", { credentials: "include" });
```

CORS allows both `http://localhost:5173` and `http://127.0.0.1:5173` with credentials.

## Tests

```bash
pytest -q
```

For production, use HTTPS, set `COOKIE_SECURE=true`, provide a strong secret, disable development mode, and run behind a production WSGI server.
