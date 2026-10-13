from src.database_app import app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=app.config["ENVIRONMENT"] == "development")


"""
Legacy in-memory implementation retained temporarily for reference.
The active PostgreSQL-backed application is src.database_app.

import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config.update(
    JWT_SECRET=os.getenv("JWT_SECRET", "development-only-change-me"),
    JWT_EXPIRATION_MINUTES=int(os.getenv("JWT_EXPIRATION_MINUTES", "60")),
    AUTH_COOKIE_NAME=os.getenv("AUTH_COOKIE_NAME", "access_token"),
    COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    COOKIE_SAMESITE=os.getenv("COOKIE_SAMESITE", "Lax"),
    ENVIRONMENT=os.getenv("FLASK_ENV", "development"),
)
ALLOWED_ORIGINS = os.getenv(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")
CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)

# Sample patient database
USERS = [{"id": "user-001", "name": "Demo Clinician",
          "email": "clinician@example.test", "role": "clinician",
          "password_hash": generate_password_hash("demo-password")}]
PATIENTS_DB = [
    {"id": "patient-001", "name": "Alex Rivera", "age": 41,
     "gender": "Non-binary", "modality": "CT", "study_uid": "demo-study-001",
     "status": "Completed"},
    {"id": "patient-002", "name": "Morgan Lee", "age": 56,
     "gender": "Female", "modality": "MRI", "study_uid": "demo-study-002",
     "status": "Pending Review"},
]

def error_response(status, code, message):
    return jsonify(success=False, error={"code": code, "message": message}), status

def public_user(user):
    return {key: user[key] for key in ("id", "name", "email", "role")}

def issue_token(user):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user["id"], "iat": now,
                       "exp": now + timedelta(minutes=app.config["JWT_EXPIRATION_MINUTES"])},
                      app.config["JWT_SECRET"], algorithm="HS256")

def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        token = request.cookies.get(app.config["AUTH_COOKIE_NAME"])
        if not token:
            return error_response(401, "AUTHENTICATION_REQUIRED", "Authentication is required")
        try:
            payload = jwt.decode(token, app.config["JWT_SECRET"], algorithms=["HS256"])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return error_response(401, "INVALID_AUTHENTICATION", "Authentication is invalid or expired")
        user = next((u for u in USERS if u["id"] == payload.get("sub")), None)
        if user is None:
            return error_response(401, "INVALID_AUTHENTICATION", "Authentication is invalid")
        request.current_user = user
        return view(*args, **kwargs)
    return wrapped

@app.errorhandler(404)
def not_found(_error):
    return error_response(404, "NOT_FOUND", "The requested resource was not found")

@app.errorhandler(405)
def method_not_allowed(_error):
    return error_response(405, "METHOD_NOT_ALLOWED", "The HTTP method is not supported")

@app.errorhandler(500)
def server_error(_error):
    return error_response(500, "INTERNAL_SERVER_ERROR", "An unexpected server error occurred")

@app.post('/api/auth/login')
def login():
    body = request.get_json(silent=True) or {}
    email, password = body.get("email"), body.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
        return error_response(400, "VALIDATION_ERROR", "Email and password are required")
    user = next((u for u in USERS if u["email"].lower() == email.strip().lower()), None)
    if user is None or not check_password_hash(user["password_hash"], password):
        return error_response(401, "INVALID_CREDENTIALS", "Email or password is incorrect")
    response = jsonify(success=True, data=public_user(user))
    response.set_cookie(app.config["AUTH_COOKIE_NAME"], issue_token(user), httponly=True,
                        secure=app.config["COOKIE_SECURE"], samesite=app.config["COOKIE_SAMESITE"],
                        max_age=app.config["JWT_EXPIRATION_MINUTES"] * 60, path='/')
    return response, 200

@app.post('/api/auth/logout')
def logout():
    response = jsonify(success=True, data=None)
    response.delete_cookie(app.config["AUTH_COOKIE_NAME"], path='/')
    return response, 200

@app.get('/api/auth/me')
@require_auth
def me():
    return jsonify(success=True, data=public_user(request.current_user)), 200

def filtered_patients():
    search = request.args.get('search', '').strip().lower()
    status = request.args.get('status', '').strip().lower()
    modality = request.args.get('modality', '').strip().lower()
    result = PATIENTS_DB
    if search:
        result = [p for p in result if search in p['id'].lower() or search in p['name'].lower()]
    if status:
        result = [p for p in result if p['status'].lower() == status]
    if modality:
        result = [p for p in result if p['modality'].lower() == modality]
    return result

@app.get('/api/patients')
@require_auth
def get_patients():
    patients = filtered_patients()
    return jsonify({
        "success": True,
        "total": len(patients),
        "data": patients
    }), 200

@app.get('/api/patients/<patient_id>')
@require_auth
def get_patient(patient_id):
    patient = next((p for p in PATIENTS_DB if p['id'] == patient_id), None)
    if patient is None:
        return error_response(404, 'PATIENT_NOT_FOUND', 'The patient was not found')
    return jsonify(success=True, data=patient), 200

@app.get('/api/dashboard/summary')
@require_auth
def dashboard_summary():
    completed_studies = sum(patient['status'].lower() == 'completed' for patient in PATIENTS_DB)
    pending_review = sum('pending' in patient['status'].lower() for patient in PATIENTS_DB)
    modality_counts = {}
    for patient in PATIENTS_DB:
        modality = patient['modality']
        modality_counts[modality] = modality_counts.get(modality, 0) + 1

    return jsonify(success=True, data={
        'total_patients': len(PATIENTS_DB),
        'completed_studies': completed_studies,
        'pending_review': pending_review,
        'modality_counts': modality_counts,
    }), 200

@app.get('/api/appointments')
@require_auth
def appointments():
    return jsonify(success=True, data=[], total=0), 200

@app.get('/api/notifications')
@require_auth
def notifications():
    return jsonify(success=True, data=[], total=0), 200

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', '5000')),
            debug=app.config['ENVIRONMENT'] == 'development')
"""
