import os
import hashlib
import hmac
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for

from db import close_db, get_db


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-change-me")
app.teardown_appcontext(close_db)

BELT_SEQUENCE = [
    "White",
    "Yellow",
    "Orange",
    "Purple",
    "Blue",
    "Green",
    "3rd Brown",
    "2nd Brown",
    "1st Brown",
    "Black Belt",
]
PROGRAM_TRACKS = ("kid", "adult")
MAX_CLASSES_PER_WEEK = 3
LEARNED_TARGET = 3


def _belt_name_for_index(belt_index):
    safe_idx = max(0, min(int(belt_index or 0), len(BELT_SEQUENCE) - 1))
    return BELT_SEQUENCE[safe_idx]


def _normalize_track(value):
    normalized = (value or "").strip().lower()
    return normalized if normalized in PROGRAM_TRACKS else "kid"


def _ensure_feature_schema(cur):
    # Keep old local databases compatible by creating/altering new tables on demand.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS class_offerings (
          id INT AUTO_INCREMENT PRIMARY KEY,
          class_name VARCHAR(120) NOT NULL,
          class_date DATE NOT NULL,
          start_time TIME NOT NULL,
          end_time TIME NOT NULL,
          instructor_user_id INT NULL,
          created_by_user_id INT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (instructor_user_id) REFERENCES users(id),
          FOREIGN KEY (created_by_user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS class_enrollments (
          id INT AUTO_INCREMENT PRIMARY KEY,
          offering_id INT NOT NULL,
          child_id INT NOT NULL,
          enrolled_by_user_id INT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_class_enrollment (offering_id, child_id),
          FOREIGN KEY (offering_id) REFERENCES class_offerings(id),
          FOREIGN KEY (child_id) REFERENCES children(id),
          FOREIGN KEY (enrolled_by_user_id) REFERENCES users(id)
        )
        """
    )

    # Backfill columns added for belt/track and 1-3 learn counts.
    alter_statements = [
        "ALTER TABLE children ADD COLUMN program_track ENUM('kid', 'adult') NOT NULL DEFAULT 'kid'",
        "ALTER TABLE children ADD COLUMN belt_index INT NOT NULL DEFAULT 0",
        "ALTER TABLE techniques ADD COLUMN program_track ENUM('kid', 'adult') NOT NULL DEFAULT 'kid'",
        "ALTER TABLE techniques ADD COLUMN belt_name VARCHAR(40) NOT NULL DEFAULT 'White'",
        "ALTER TABLE child_skill_progress ADD COLUMN learned_count TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE attendance_students ADD COLUMN is_present TINYINT(1) NOT NULL DEFAULT 1",
        "ALTER TABLE attendance_sessions ADD COLUMN offering_id INT NULL",
    ]
    for statement in alter_statements:
        try:
            cur.execute(statement)
        except Exception:
            pass

    # Create separate SQL views for kid/adult belt placement.
    cur.execute(
        """
        CREATE OR REPLACE VIEW kid_belt_students AS
        SELECT id, child_name, belt_index
        FROM children
        WHERE program_track = 'kid'
        """
    )
    cur.execute(
        """
        CREATE OR REPLACE VIEW adult_belt_students AS
        SELECT id, child_name, belt_index
        FROM children
        WHERE program_track = 'adult'
        """
    )

    # Attendance helper tables.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_sessions (
          id INT AUTO_INCREMENT PRIMARY KEY,
          offering_id INT NULL,
          class_name VARCHAR(120) NOT NULL,
          class_date DATE NOT NULL,
          start_time TIME NOT NULL,
          end_time TIME NOT NULL,
          staff_user_id INT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (offering_id) REFERENCES class_offerings(id),
          FOREIGN KEY (staff_user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_students (
          id INT AUTO_INCREMENT PRIMARY KEY,
          attendance_session_id INT NOT NULL,
          child_id INT NOT NULL,
          is_present TINYINT(1) NOT NULL DEFAULT 1,
          UNIQUE KEY uq_attendance_student (attendance_session_id, child_id),
          FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id),
          FOREIGN KEY (child_id) REFERENCES children(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_technique_logs (
          id INT AUTO_INCREMENT PRIMARY KEY,
          attendance_session_id INT NOT NULL,
          child_id INT NOT NULL,
          technique_id INT NOT NULL,
          learned_increment TINYINT NOT NULL DEFAULT 1,
          UNIQUE KEY uq_attendance_technique_log (attendance_session_id, child_id, technique_id),
          FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id),
          FOREIGN KEY (child_id) REFERENCES children(id),
          FOREIGN KEY (technique_id) REFERENCES techniques(id)
        )
        """
    )


def _get_child_belt_progress(cur, child_id, track, belt_name):
    cur.execute(
        """
        SELECT COUNT(*) AS belt_total
        FROM techniques
        WHERE is_active = 1
          AND program_track = %s
          AND belt_name = %s
        """,
        (track, belt_name),
    )
    total_row = cur.fetchone() or {}
    belt_total = int(total_row.get("belt_total") or 0)

    cur.execute(
        """
        SELECT COUNT(DISTINCT csp.technique_id) AS learned_total
        FROM child_skill_progress csp
        JOIN techniques t ON t.id = csp.technique_id
        WHERE csp.child_id = %s
          AND t.program_track = %s
          AND t.belt_name = %s
          AND (csp.learned_count >= %s OR csp.completed = 1)
        """,
        (child_id, track, belt_name, LEARNED_TARGET),
    )
    learned_row = cur.fetchone() or {}
    learned_total = int(learned_row.get("learned_total") or 0)
    return learned_total, belt_total


def _apply_learning_entry(cur, child_id, technique_id, staff_user_id, increment=1, notes_text=None):
    # Increment learning count for a child-technique pair, capped at 3.
    cur.execute("SELECT id FROM children WHERE id = %s", (child_id,))
    child = cur.fetchone()
    cur.execute(
        "SELECT id FROM techniques WHERE id = %s AND is_active = 1",
        (technique_id,),
    )
    technique = cur.fetchone()
    if not child or not technique:
        return False

    step = max(1, min(int(increment or 1), LEARNED_TARGET))
    cur.execute(
        """
        SELECT id, learned_count
        FROM child_skill_progress
        WHERE child_id = %s AND technique_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (child_id, technique_id),
    )
    existing = cur.fetchone()
    if existing:
        new_count = min(LEARNED_TARGET, int(existing.get("learned_count") or 0) + step)
        completed = 1 if new_count >= LEARNED_TARGET else 0
        cur.execute(
            """
            UPDATE child_skill_progress
            SET learned_count = %s,
                completed = %s,
                completed_at = CASE WHEN %s = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                notes = COALESCE(%s, notes)
            WHERE id = %s
            """,
            (new_count, completed, completed, notes_text or None, existing["id"]),
        )
    else:
        learned_count = min(LEARNED_TARGET, step)
        completed = 1 if learned_count >= LEARNED_TARGET else 0
        cur.execute(
            """
            INSERT INTO child_skill_progress
                (child_id, technique_id, assigned_by_user_id, learned_count, completed, completed_at, notes)
            VALUES
                (%s, %s, %s, %s, %s, CASE WHEN %s = 1 THEN CURRENT_TIMESTAMP ELSE NULL END, %s)
            """,
            (
                child_id,
                technique_id,
                staff_user_id,
                learned_count,
                completed,
                completed,
                notes_text or None,
            ),
        )
    return True


def _fetch_child_progress_summary(cur, parent_user_id=None):
    # Return per-child belt-based progress, optionally scoped to one parent.
    _ensure_feature_schema(cur)
    query = "SELECT c.id, c.child_name, c.program_track, c.belt_index FROM children c"
    params = ()
    if parent_user_id is not None:
        query += " WHERE c.parent_user_id = %s"
        params = (parent_user_id,)
    query += " ORDER BY c.child_name"
    cur.execute(query, params)
    children = cur.fetchall()
    for child in children:
        track = _normalize_track(child.get("program_track"))
        belt_index = int(child.get("belt_index") or 0)
        belt_index = max(0, min(belt_index, len(BELT_SEQUENCE) - 1))
        current_belt = _belt_name_for_index(belt_index)
        next_belt = (
            _belt_name_for_index(belt_index + 1)
            if belt_index < len(BELT_SEQUENCE) - 1
            else "Mastery Track"
        )
        completed_skills, total_skills = _get_child_belt_progress(
            cur, child["id"], track, current_belt
        )
        child["program_track"] = track
        child["belt_index"] = belt_index
        child["current_belt"] = current_belt
        child["next_belt"] = next_belt
        child["total_skills"] = total_skills
        child["completed_skills"] = completed_skills
        if total_skills > 0:
            child["belt_progress_percent"] = round((completed_skills * 100.0) / total_skills)
            child["skills_needed_for_next_belt"] = max(total_skills - completed_skills, 0)
        else:
            child["belt_progress_percent"] = 0
            child["skills_needed_for_next_belt"] = 0
        child["belt_progress_count"] = completed_skills
        child["can_promote"] = (
            total_skills > 0
            and completed_skills >= total_skills
            and belt_index < len(BELT_SEQUENCE) - 1
        )
    return children


def _fetch_child_progress_rows(cur, child_ids):
    # Return detailed progress rows grouped by child id for dashboard rendering.
    if not child_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(child_ids))
    cur.execute(
        f"""
        SELECT
            csp.id,
            csp.child_id,
            csp.technique_id,
            csp.completed,
            csp.learned_count,
            csp.assigned_at,
            csp.completed_at,
            csp.notes,
            t.technique_name,
            t.program_track,
            t.belt_name,
            u.username AS assigned_by
        FROM child_skill_progress csp
        JOIN techniques t ON t.id = csp.technique_id
        JOIN users u ON u.id = csp.assigned_by_user_id
        WHERE csp.child_id IN ({placeholders})
        ORDER BY csp.assigned_at DESC
        """,
        tuple(child_ids),
    )
    rows = cur.fetchall()
    grouped = {child_id: [] for child_id in child_ids}
    for row in rows:
        grouped[row["child_id"]].append(row)
    return grouped


def _build_two_week_calendar(start_date, shifts):
    # Build a 14-day calendar payload grouped into 2 weeks for UI rendering.
    shifts_by_date = {}
    for shift in shifts:
        key = shift["shift_date"].isoformat()
        shifts_by_date.setdefault(key, []).append(shift)

    days = []
    for offset in range(14):
        day_value = start_date + timedelta(days=offset)
        key = day_value.isoformat()
        days.append(
            {
                "iso_date": key,
                "display_date": day_value.strftime("%b %d"),
                "weekday": day_value.strftime("%A"),
                "weekday_short": day_value.strftime("%a"),
                "shifts": shifts_by_date.get(key, []),
            }
        )

    return [days[:7], days[7:]]


def _ensure_parent_notes_table(cur):
    # Ensure parent notes table exists so note features work on existing databases.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS parent_notes (
          id INT AUTO_INCREMENT PRIMARY KEY,
          child_id INT NOT NULL,
          author_user_id INT NOT NULL,
          note_text TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (child_id) REFERENCES children(id),
          FOREIGN KEY (author_user_id) REFERENCES users(id)
        )
        """
    )


def _fetch_parent_notes_rows(cur, child_ids):
    # Return staff-authored notes to parents grouped by child id.
    if not child_ids:
        return {}

    _ensure_parent_notes_table(cur)
    placeholders = ", ".join(["%s"] * len(child_ids))
    cur.execute(
        f"""
        SELECT
            pn.id,
            pn.child_id,
            pn.note_text,
            pn.created_at,
            u.username AS author_username,
            u.role AS author_role
        FROM parent_notes pn
        JOIN users u ON u.id = pn.author_user_id
        WHERE pn.child_id IN ({placeholders})
        ORDER BY pn.created_at DESC
        """,
        tuple(child_ids),
    )
    rows = cur.fetchall()
    grouped = {child_id: [] for child_id in child_ids}
    for row in rows:
        grouped[row["child_id"]].append(row)
    return grouped


def hash_password(raw_password: str) -> str:
    # Store passwords as a prefixed SHA256 digest used across auth flows.
    digest = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
    return f"sha256${digest}"


def verify_password(stored_hash: str, candidate: str) -> bool:
    # Support prefixed SHA256, legacy raw SHA256, and exact-string fallback.
    stored_hash = (stored_hash or "").strip()
    if stored_hash.startswith("sha256$"):
        expected = stored_hash.split("$", 1)[1]
        actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, actual)
    # Backward compatibility: allow raw SHA256 hex without prefix.
    if len(stored_hash) == 64 and all(ch in "0123456789abcdef" for ch in stored_hash.lower()):
        actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored_hash.lower(), actual)
    return hmac.compare_digest(stored_hash, candidate)


# -----------------------------
# Auth helpers
# -----------------------------
def login_required(view):
    # Redirect unauthenticated users to login before protected views run.
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*allowed_roles):
    # Enforce role-based access control for protected routes.
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") not in allowed_roles:
                flash("You do not have access to that page.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    # Authenticate user and initialize session state.
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT id, username, password_hash, role FROM users WHERE LOWER(TRIM(username)) = LOWER(%s)",
            (username,),
        )
        user = cur.fetchone()
        cur.close()

        if not user or not verify_password(user["password_hash"], password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    # Create employee/parent accounts with validation and optional child record.
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "").strip()
        child_name = request.form.get("child_name", "").strip()

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        if role not in {"employee", "parent"}:
            flash("Invalid role selected.", "error")
            return render_template("register.html")

        if role == "parent" and not child_name:
            flash("Parent registration requires a student name.", "error")
            return render_template("register.html")

        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()
        if existing:
            cur.close()
            flash("Username already exists. Choose a different username.", "error")
            return render_template("register.html")

        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
            (username, hash_password(password), role),
        )
        user_id = cur.lastrowid

        if role == "parent":
            cur.execute(
                "INSERT INTO children (child_name, parent_user_id) VALUES (%s, %s)",
                (child_name, user_id),
            )

        db.commit()
        cur.close()

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    role = session.get("role")
    if role == "manager":
        return redirect(url_for("manager_dashboard"))
    if role == "employee":
        return redirect(url_for("employee_dashboard"))
    if role == "parent":
        return redirect(url_for("parent_dashboard"))

    flash("Unknown role.", "error")
    return redirect(url_for("logout"))


# -----------------------------
# Employee views
# -----------------------------
@app.route("/employee")
@login_required
@role_required("employee")
def employee_dashboard():
    # Show employee shifts and submitted request history.
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT s.id, s.shift_date, s.start_time, s.end_time, s.class_name, u.username AS assigned_to
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        WHERE s.employee_user_id = %s
        ORDER BY s.shift_date, s.start_time
        """,
        (session["user_id"],),
    )
    my_shifts = cur.fetchall()

    cur.execute(
        """
        SELECT r.id, r.request_type, r.status, r.reason, r.created_at,
               s.shift_date, s.start_time, s.end_time, s.class_name,
               u.username AS requested_employee
        FROM requests r
        LEFT JOIN shifts s ON s.id = r.shift_id
        LEFT JOIN users u ON u.id = r.requested_employee_id
        WHERE r.requester_user_id = %s
        ORDER BY r.created_at DESC
        """,
        (session["user_id"],),
    )
    my_requests = cur.fetchall()

    calendar_start = date.today()
    calendar_end = calendar_start + timedelta(days=13)
    cur.execute(
        """
        SELECT
            s.id,
            s.shift_date,
            s.class_name,
            TIME_FORMAT(s.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(s.end_time, '%H:%i') AS end_label
        FROM shifts s
        WHERE s.employee_user_id = %s
          AND s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time
        """,
        (session["user_id"], calendar_start, calendar_end),
    )
    upcoming_shifts = cur.fetchall()
    calendar_weeks = _build_two_week_calendar(calendar_start, upcoming_shifts)
    cur.close()

    return render_template(
        "employee_dashboard.html",
        my_shifts=my_shifts,
        my_requests=my_requests,
        calendar_weeks=calendar_weeks,
    )


@app.route("/employee/schedule")
@login_required
@role_required("employee")
def employee_schedule():
    # Render a schedule-only view for the logged-in employee.
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT s.shift_date, s.start_time, s.end_time, s.class_name
        FROM shifts s
        WHERE s.employee_user_id = %s
        ORDER BY s.shift_date, s.start_time
        """,
        (session["user_id"],),
    )
    my_shifts = cur.fetchall()
    cur.close()
    return render_template("employee_schedule.html", my_shifts=my_shifts)


@app.route("/employee/progress", methods=["GET", "POST"])
@login_required
@role_required("employee")
def employee_progress():
    return _staff_progress_screen("Progress Screen for Staff")


@app.route("/employee/attendance", methods=["GET", "POST"])
@login_required
@role_required("employee")
def employee_attendance():
    return _staff_attendance_screen("Attendance (Staff)")


@app.route("/employee/request-switch", methods=["GET", "POST"])
@login_required
@role_required("employee")
def request_switch():
    # Let an employee request a shift transfer to another employee.
    db = get_db()
    cur = db.cursor(dictionary=True)

    if request.method == "POST":
        shift_id = request.form.get("shift_id")
        requested_employee_id = request.form.get("requested_employee_id")
        reason = request.form.get("reason", "").strip()

        cur.execute(
            "SELECT id FROM shifts WHERE id = %s AND employee_user_id = %s",
            (shift_id, session["user_id"]),
        )
        owned_shift = cur.fetchone()
        if not owned_shift:
            flash("You can only request switches for your own shifts.", "error")
            cur.close()
            return redirect(url_for("request_switch"))

        cur.execute(
            """
            INSERT INTO requests (request_type, requester_user_id, shift_id, requested_employee_id, reason, status)
            VALUES ('switch', %s, %s, %s, %s, 'pending')
            """,
            (session["user_id"], shift_id, requested_employee_id, reason),
        )
        db.commit()
        cur.close()

        flash("Shift switch request submitted.", "success")
        return redirect(url_for("employee_dashboard"))

    cur.execute(
        """
        SELECT id, shift_date, start_time, end_time, class_name
        FROM shifts
        WHERE employee_user_id = %s AND shift_date >= %s
        ORDER BY shift_date, start_time
        """,
        (session["user_id"], date.today()),
    )
    my_upcoming_shifts = cur.fetchall()

    cur.execute(
        "SELECT id, username FROM users WHERE role = 'employee' AND id != %s ORDER BY username",
        (session["user_id"],),
    )
    employees = cur.fetchall()
    cur.close()

    return render_template(
        "request_switch.html", my_upcoming_shifts=my_upcoming_shifts, employees=employees
    )


@app.route("/employee/request-callout", methods=["GET", "POST"])
@login_required
@role_required("employee")
def request_callout():
    # Let an employee submit a call-out request for one of their shifts.
    db = get_db()
    cur = db.cursor(dictionary=True)

    if request.method == "POST":
        shift_id = request.form.get("shift_id")
        reason = request.form.get("reason", "").strip()

        cur.execute(
            "SELECT id FROM shifts WHERE id = %s AND employee_user_id = %s",
            (shift_id, session["user_id"]),
        )
        owned_shift = cur.fetchone()
        if not owned_shift:
            flash("You can only submit call-outs for your own shifts.", "error")
            cur.close()
            return redirect(url_for("request_callout"))

        cur.execute(
            """
            INSERT INTO requests (request_type, requester_user_id, shift_id, reason, status)
            VALUES ('callout', %s, %s, %s, 'pending')
            """,
            (session["user_id"], shift_id, reason),
        )
        db.commit()
        cur.close()

        flash("Call-out request submitted.", "success")
        return redirect(url_for("employee_dashboard"))

    cur.execute(
        """
        SELECT id, shift_date, start_time, end_time, class_name
        FROM shifts
        WHERE employee_user_id = %s AND shift_date >= %s
        ORDER BY shift_date, start_time
        """,
        (session["user_id"], date.today()),
    )
    my_upcoming_shifts = cur.fetchall()
    cur.close()

    return render_template("request_callout.html", my_upcoming_shifts=my_upcoming_shifts)


def _staff_progress_screen(page_title):
    # Shared employee/manager student-progress entry and listing screen.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        # Route multiple form intents from a single staff progress page.
        action = request.form.get("action", "add_progress").strip()

        if action == "promote_belt":
            child_id = request.form.get("child_id", type=int)
            cur.execute(
                "SELECT id, belt_index, program_track FROM children WHERE id = %s",
                (child_id,),
            )
            child = cur.fetchone()
            if not child:
                flash("Student not found.", "error")
                cur.close()
                return redirect(request.path)

            belt_index = int(child.get("belt_index") or 0)
            track = _normalize_track(child.get("program_track"))
            current_belt = _belt_name_for_index(belt_index)
            learned_total, belt_total = _get_child_belt_progress(
                cur, child_id, track, current_belt
            )
            if belt_total == 0 or learned_total < belt_total:
                flash("Student has not completed all techniques for this belt.", "error")
                cur.close()
                return redirect(request.path)
            if belt_index >= len(BELT_SEQUENCE) - 1:
                flash("Student is already at the final belt.", "info")
                cur.close()
                return redirect(request.path)

            cur.execute(
                "UPDATE children SET belt_index = %s WHERE id = %s",
                (belt_index + 1, child_id),
            )
            db.commit()
            flash("Student promoted to next belt.", "success")

        elif action == "send_parent_note":
            child_id = request.form.get("child_id", type=int)
            parent_note = request.form.get("parent_note", "").strip()

            cur.execute("SELECT id FROM children WHERE id = %s", (child_id,))
            child = cur.fetchone()
            if not child or not parent_note:
                flash("Please choose a valid student and write a note.", "error")
                cur.close()
                return redirect(request.path)

            _ensure_parent_notes_table(cur)
            cur.execute(
                """
                INSERT INTO parent_notes (child_id, author_user_id, note_text)
                VALUES (%s, %s, %s)
                """,
                (child_id, session["user_id"], parent_note),
            )
            db.commit()
            flash("Parent note sent.", "success")
        elif action == "apply_attendance":
            class_name = request.form.get("class_name", "").strip()
            class_date = request.form.get("class_date", "").strip()
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            child_ids = request.form.getlist("attendance_child_ids")
            technique_ids = request.form.getlist("attendance_technique_ids")
            learned_increment = request.form.get("learned_increment", type=int) or 1

            if not (class_name and class_date and start_time and end_time):
                flash("Please select a current class first.", "error")
                cur.close()
                return redirect(request.path)
            if not child_ids:
                flash("Select at least one student present in class.", "error")
                cur.close()
                return redirect(request.path)
            if not technique_ids:
                flash("Select at least one technique learned in class.", "error")
                cur.close()
                return redirect(request.path)

            cur.execute(
                """
                INSERT INTO attendance_sessions
                  (class_name, class_date, start_time, end_time, staff_user_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (class_name, class_date, start_time, end_time, session["user_id"]),
            )
            attendance_session_id = cur.lastrowid
            updated_pairs = 0
            for child_id_value in child_ids:
                child_id = int(child_id_value)
                cur.execute(
                    """
                    INSERT IGNORE INTO attendance_students (attendance_session_id, child_id)
                    VALUES (%s, %s)
                    """,
                    (attendance_session_id, child_id),
                )
                for technique_id_value in technique_ids:
                    technique_id = int(technique_id_value)
                    if _apply_learning_entry(
                        cur,
                        child_id,
                        technique_id,
                        session["user_id"],
                        increment=learned_increment,
                        notes_text=f"Attendance: {class_name} {class_date}",
                    ):
                        updated_pairs += 1

            db.commit()
            flash(
                f"Attendance saved and {updated_pairs} student-technique records updated.",
                "success",
            )
        else:
            child_id = request.form.get("child_id", type=int)
            technique_id = request.form.get("technique_id", type=int)
            notes = request.form.get("notes", "").strip()
            learned_count = request.form.get("learned_count", type=int) or 1

            if not _apply_learning_entry(
                cur,
                child_id,
                technique_id,
                session["user_id"],
                increment=learned_count,
                notes_text=notes,
            ):
                flash("Please choose a valid student and active technique.", "error")
                cur.close()
                return redirect(request.path)
            db.commit()
            flash("Progress record added.", "success")

    cur.execute(
        """
        SELECT id, child_name, program_track, belt_index
        FROM children
        ORDER BY child_name
        """
    )
    children = cur.fetchall()
    for child in children:
        child["program_track"] = _normalize_track(child.get("program_track"))
        child["belt_name"] = _belt_name_for_index(child.get("belt_index"))

    # Keep active techniques for new assignments and full list for row edits.
    cur.execute(
        """
        SELECT id, technique_name, is_active, program_track, belt_name
        FROM techniques
        ORDER BY program_track, belt_name, technique_name
        """
    )
    all_techniques = cur.fetchall()
    techniques = [t for t in all_techniques if t["is_active"]]
    today = date.today()
    cur.execute(
        """
        SELECT
            class_name,
            shift_date AS class_date,
            TIME_FORMAT(start_time, '%H:%i') AS start_label,
            TIME_FORMAT(end_time, '%H:%i') AS end_label
        FROM shifts
        WHERE shift_date = %s
        UNION ALL
        SELECT
            class_name,
            class_date,
            TIME_FORMAT(start_time, '%H:%i') AS start_label,
            TIME_FORMAT(end_time, '%H:%i') AS end_label
        FROM class_offerings
        WHERE class_date = %s
        ORDER BY class_name, class_date, start_label
        """,
        (today, today),
    )
    today_classes = cur.fetchall()
    child_summary = _fetch_child_progress_summary(cur)
    child_progress_rows = _fetch_child_progress_rows(cur, [c["id"] for c in child_summary])
    child_parent_notes = _fetch_parent_notes_rows(cur, [c["id"] for c in child_summary])
    cur.close()
    return render_template(
        "progress_screen.html",
        page_title=page_title,
        children=children,
        techniques=techniques,
        all_techniques=all_techniques,
        today_classes=today_classes,
        child_summary=child_summary,
        child_progress_rows=child_progress_rows,
        child_parent_notes=child_parent_notes,
    )


def _staff_attendance_screen(page_title):
    # Attendance from class roster with present/absent + bulk technique apply.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    attendance_endpoint = (
        "manager_attendance" if session.get("role") == "manager" else "employee_attendance"
    )

    def parse_offering_id(class_ref):
        if not class_ref or ":" not in class_ref:
            return None
        source, raw_id = class_ref.split(":", 1)
        if source != "offering" or not raw_id.isdigit():
            return None
        return int(raw_id)

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        class_ref = request.form.get("class_ref", "").strip()
        present_child_ids = {
            int(value)
            for value in request.form.getlist("present_child_ids")
            if (value or "").isdigit()
        }
        offering_id = parse_offering_id(class_ref)
        if not offering_id:
            flash("Please choose a valid class.", "error")
            cur.close()
            return redirect(request.path)

        cur.execute(
            """
            SELECT id, class_name, class_date, start_time, end_time
            FROM class_offerings
            WHERE id = %s
            """,
            (offering_id,),
        )
        class_row = cur.fetchone()
        if not class_row:
            flash("Class not found.", "error")
            cur.close()
            return redirect(request.path)

        cur.execute(
            "SELECT child_id FROM class_enrollments WHERE offering_id = %s",
            (offering_id,),
        )
        enrolled_ids = {int(row["child_id"]) for row in cur.fetchall()}
        if not enrolled_ids:
            flash("No students are enrolled in this class.", "error")
            cur.close()
            return redirect(request.path)
        if action == "mark_all_present":
            present_child_ids = set(enrolled_ids)
        else:
            present_child_ids = present_child_ids.intersection(enrolled_ids)

        cur.execute(
            """
            INSERT INTO attendance_sessions
              (offering_id, class_name, class_date, start_time, end_time, staff_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                offering_id,
                class_row["class_name"],
                class_row["class_date"],
                class_row["start_time"],
                class_row["end_time"],
                session["user_id"],
            ),
        )
        attendance_session_id = cur.lastrowid

        for child_id in enrolled_ids:
            is_present = 1 if child_id in present_child_ids else 0
            cur.execute(
                """
                INSERT INTO attendance_students (attendance_session_id, child_id, is_present)
                VALUES (%s, %s, %s)
                """,
                (attendance_session_id, child_id, is_present),
            )

        if action == "close_and_apply":
            if not present_child_ids:
                flash("No students were marked present to apply techniques.", "error")
                db.rollback()
                cur.close()
                return redirect(request.path)

            updates = 0
            for child_id in present_child_ids:
                technique_ids = [
                    int(value)
                    for value in request.form.getlist(f"technique_ids_{child_id}")
                    if (value or "").isdigit()
                ]
                learned_increment = request.form.get(
                    f"learned_increment_{child_id}",
                    type=int,
                ) or 1
                for technique_id in technique_ids:
                    if _apply_learning_entry(
                        cur,
                        child_id,
                        technique_id,
                        session["user_id"],
                        increment=learned_increment,
                        notes_text=f"Attendance: {class_row['class_name']} {class_row['class_date']}",
                    ):
                        cur.execute(
                            """
                            INSERT INTO attendance_technique_logs
                              (attendance_session_id, child_id, technique_id, learned_increment)
                            VALUES (%s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE learned_increment = VALUES(learned_increment)
                            """,
                            (
                                attendance_session_id,
                                child_id,
                                technique_id,
                                learned_increment,
                            ),
                        )
                        updates += 1
            if updates == 0:
                flash("No per-student techniques were selected to apply.", "error")
                db.rollback()
                cur.close()
                return redirect(url_for(attendance_endpoint, class_ref=class_ref))
            db.commit()
            cur.close()
            return redirect(
                url_for(
                    "attendance_summary",
                    session_id=attendance_session_id,
                )
            )
        else:
            db.commit()
            present_count = len(present_child_ids)
            absent_count = max(len(enrolled_ids) - present_count, 0)
            flash(
                f"Attendance saved. Present: {present_count}, Absent: {absent_count}.",
                "success",
            )
            cur.close()
            return redirect(url_for(attendance_endpoint, class_ref=class_ref))

    selected_class_ref = (request.args.get("class_ref") or "").strip()
    cur.execute(
        """
        SELECT
            CONCAT('offering:', co.id) AS class_ref,
            co.id AS offering_id,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(co.end_time, '%H:%i') AS end_label,
            COUNT(ce.id) AS enrolled_count
        FROM class_offerings co
        LEFT JOIN class_enrollments ce ON ce.offering_id = co.id
        WHERE co.class_date >= %s
        GROUP BY co.id, co.class_name, co.class_date, co.start_time, co.end_time
        ORDER BY co.class_date, co.start_time, co.class_name
        """,
        (date.today(),),
    )
    current_classes = cur.fetchall()
    if not selected_class_ref and current_classes:
        selected_class_ref = current_classes[0]["class_ref"]

    selected_offering_id = parse_offering_id(selected_class_ref)
    selected_class_info = None
    child_summary = []
    if selected_offering_id:
        cur.execute(
            """
            SELECT id, class_name, class_date,
                   TIME_FORMAT(start_time, '%H:%i') AS start_label,
                   TIME_FORMAT(end_time, '%H:%i') AS end_label
            FROM class_offerings
            WHERE id = %s
            """,
            (selected_offering_id,),
        )
        selected_class_info = cur.fetchone()
        cur.execute(
            """
            SELECT c.id, c.child_name, c.program_track, c.belt_index
            FROM class_enrollments ce
            JOIN children c ON c.id = ce.child_id
            WHERE ce.offering_id = %s
            ORDER BY c.child_name
            """,
            (selected_offering_id,),
        )
        child_summary = cur.fetchall()
        for child in child_summary:
            track = _normalize_track(child.get("program_track"))
            belt_index = int(child.get("belt_index") or 0)
            belt_index = max(0, min(belt_index, len(BELT_SEQUENCE) - 1))
            current_belt = _belt_name_for_index(belt_index)
            completed_skills, total_skills = _get_child_belt_progress(
                cur, child["id"], track, current_belt
            )
            child["program_track"] = track
            child["current_belt"] = current_belt
            child["belt_progress_count"] = completed_skills
            child["total_skills"] = total_skills

    cur.execute(
        """
        SELECT id, technique_name, program_track, belt_name
        FROM techniques
        WHERE is_active = 1
        ORDER BY program_track, belt_name, technique_name
        """
    )
    active_techniques = cur.fetchall()
    cur.close()

    return render_template(
        "attendance.html",
        page_title=page_title,
        current_classes=current_classes,
        selected_class_ref=selected_class_ref,
        selected_class_info=selected_class_info,
        roster_students=child_summary,
        active_techniques=active_techniques,
        belt_sequence=BELT_SEQUENCE,
        program_tracks=PROGRAM_TRACKS,
    )


# -----------------------------
# Manager views
# -----------------------------
@app.route("/manager")
@login_required
@role_required("manager")
def manager_dashboard():
    # Show all shifts and pending employee requests for manager review.
    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute(
        """
        SELECT s.id, s.shift_date, s.start_time, s.end_time, s.class_name, u.username AS employee
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        ORDER BY s.shift_date, s.start_time
        """
    )
    all_shifts = cur.fetchall()

    cur.execute(
        """
        SELECT r.id, r.request_type, r.status, r.reason, r.created_at,
               req.username AS requester,
               target.username AS requested_employee,
               s.shift_date, s.start_time, s.end_time, s.class_name
        FROM requests r
        JOIN users req ON req.id = r.requester_user_id
        LEFT JOIN users target ON target.id = r.requested_employee_id
        LEFT JOIN shifts s ON s.id = r.shift_id
        WHERE r.status = 'pending'
        ORDER BY r.created_at ASC
        """
    )
    pending_requests = cur.fetchall()

    calendar_start = date.today()
    calendar_end = calendar_start + timedelta(days=13)
    cur.execute(
        """
        SELECT
            s.id,
            s.shift_date,
            s.class_name,
            u.username AS employee,
            TIME_FORMAT(s.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(s.end_time, '%H:%i') AS end_label
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        WHERE s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time
        """,
        (calendar_start, calendar_end),
    )
    upcoming_shifts = cur.fetchall()
    calendar_weeks = _build_two_week_calendar(calendar_start, upcoming_shifts)
    cur.close()

    return render_template(
        "manager_dashboard.html",
        all_shifts=all_shifts,
        pending_requests=pending_requests,
        calendar_weeks=calendar_weeks,
    )


@app.route("/manager/schedule", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_schedule():
    # Calendar editor for next 14 days with shift assignment and creation.
    db = get_db()
    cur = db.cursor(dictionary=True)

    calendar_start = date.today()
    calendar_end = calendar_start + timedelta(days=13)

    def parse_selected_day(raw_value):
        if not raw_value:
            return calendar_start
        try:
            parsed = datetime.strptime(raw_value, "%Y-%m-%d").date()
        except ValueError:
            return calendar_start
        if parsed < calendar_start or parsed > calendar_end:
            return calendar_start
        return parsed

    def schedule_redirect(day_value):
        return redirect(url_for("manager_schedule", day=day_value.isoformat()))

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        selected_day = parse_selected_day(request.form.get("selected_day", "").strip())

        if action == "update_shift":
            # Update an existing shift's assignment and class time details.
            shift_id = request.form.get("shift_id", type=int)
            employee_id = request.form.get("employee_user_id", type=int)
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            class_name = request.form.get("class_name", "").strip()

            if not (shift_id and employee_id and start_time and end_time and class_name):
                flash("Employee, class, start time, and end time are required.", "error")
                cur.close()
                return schedule_redirect(selected_day)

            cur.execute("SELECT id FROM shifts WHERE id = %s", (shift_id,))
            shift = cur.fetchone()
            if not shift:
                flash("Shift not found.", "error")
                cur.close()
                return schedule_redirect(selected_day)

            cur.execute("SELECT id FROM users WHERE id = %s AND role = 'employee'", (employee_id,))
            employee = cur.fetchone()
            if not employee:
                flash("Employee not found.", "error")
                cur.close()
                return schedule_redirect(selected_day)

            cur.execute(
                """
                UPDATE shifts
                SET employee_user_id = %s,
                    start_time = %s,
                    end_time = %s,
                    class_name = %s
                WHERE id = %s
                """,
                (employee_id, start_time, end_time, class_name, shift_id),
            )
            db.commit()
            flash("Shift updated.", "success")
            cur.close()
            return schedule_redirect(selected_day)

        elif action == "create":
            # Create a new shift on the selected calendar day.
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            class_name = request.form.get("class_name", "").strip()
            employee_id = request.form.get("employee_user_id", type=int)
            shift_date = selected_day.isoformat()

            if not (start_time and end_time and class_name and employee_id):
                flash("All fields are required to create a shift.", "error")
                cur.close()
                return schedule_redirect(selected_day)

            cur.execute("SELECT id FROM users WHERE id = %s AND role = 'employee'", (employee_id,))
            employee = cur.fetchone()
            if not employee:
                flash("Employee not found.", "error")
                cur.close()
                return schedule_redirect(selected_day)

            cur.execute(
                """
                INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (employee_id, shift_date, start_time, end_time, class_name),
            )
            db.commit()
            flash("Shift created.", "success")
            cur.close()
            return schedule_redirect(selected_day)

        flash("Invalid schedule action.", "error")
        cur.close()
        return schedule_redirect(selected_day)

    selected_day = parse_selected_day(request.args.get("day", "").strip())

    cur.execute(
        "SELECT id, username FROM users WHERE role = 'employee' ORDER BY username"
    )
    employees = cur.fetchall()

    cur.execute(
        """
        SELECT
            s.id,
            s.employee_user_id,
            s.shift_date,
            s.start_time,
            s.end_time,
            s.class_name,
            TIME_FORMAT(s.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(s.end_time, '%H:%i') AS end_label,
            u.username AS employee
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        WHERE s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time
        """,
        (calendar_start, calendar_end),
    )
    shifts = cur.fetchall()
    cur.close()

    calendar_weeks = _build_two_week_calendar(calendar_start, shifts)
    selected_day_key = selected_day.isoformat()
    selected_day_shifts = []
    for week in calendar_weeks:
        for day in week:
            day["is_selected"] = day["iso_date"] == selected_day_key
            if day["is_selected"]:
                selected_day_shifts = day["shifts"]

    return render_template(
        "manager_schedule.html",
        calendar_weeks=calendar_weeks,
        employees=employees,
        selected_day=selected_day,
        selected_day_shifts=selected_day_shifts,
    )


@app.route("/manager/progress", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_progress():
    # Reuse shared progress page with manager-specific title text.
    return _staff_progress_screen("Student Progress Screen (Manager)")


@app.route("/manager/attendance", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_attendance():
    return _staff_attendance_screen("Attendance (Manager)")


@app.route("/manager/enroll", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_enroll():
    # Manager enrollment of students into class rosters.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        offering_id = request.form.get("offering_id", type=int)
        child_ids = [
            int(value)
            for value in request.form.getlist("child_ids")
            if (value or "").isdigit()
        ]
        if not offering_id:
            flash("Please choose a class.", "error")
            cur.close()
            return redirect(url_for("manager_enroll"))
        if not child_ids:
            flash("Please choose at least one student.", "error")
            cur.close()
            return redirect(url_for("manager_enroll", offering_id=offering_id))

        cur.execute(
            """
            SELECT id
            FROM class_offerings
            WHERE id = %s
            """,
            (offering_id,),
        )
        offering = cur.fetchone()
        if not offering:
            flash("Class not found.", "error")
            cur.close()
            return redirect(url_for("manager_enroll"))

        added = 0
        for child_id in child_ids:
            cur.execute(
                """
                INSERT IGNORE INTO class_enrollments (offering_id, child_id, enrolled_by_user_id)
                VALUES (%s, %s, %s)
                """,
                (offering_id, child_id, session["user_id"]),
            )
            added += cur.rowcount

        db.commit()
        flash(f"Added {added} student(s) to class roster.", "success")
        cur.close()
        return redirect(url_for("manager_enroll", offering_id=offering_id))

    selected_offering_id = request.args.get("offering_id", type=int)
    cur.execute(
        """
        SELECT
            co.id,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(co.end_time, '%H:%i') AS end_label
        FROM class_offerings co
        WHERE co.class_date >= %s
        ORDER BY co.class_date, co.start_time
        """,
        (date.today(),),
    )
    offerings = cur.fetchall()
    if not selected_offering_id and offerings:
        selected_offering_id = offerings[0]["id"]

    selected_roster = []
    if selected_offering_id:
        cur.execute(
            """
            SELECT c.id, c.child_name, c.program_track, c.belt_index
            FROM class_enrollments ce
            JOIN children c ON c.id = ce.child_id
            WHERE ce.offering_id = %s
            ORDER BY c.child_name
            """,
            (selected_offering_id,),
        )
        selected_roster = cur.fetchall()
        for child in selected_roster:
            child["program_track"] = _normalize_track(child.get("program_track"))
            child["current_belt"] = _belt_name_for_index(child.get("belt_index"))

    cur.execute(
        """
        SELECT c.id, c.child_name, c.program_track, c.belt_index, u.username AS parent_username
        FROM children c
        JOIN users u ON u.id = c.parent_user_id
        ORDER BY c.child_name
        """
    )
    all_students = cur.fetchall()
    for child in all_students:
        child["program_track"] = _normalize_track(child.get("program_track"))
        child["current_belt"] = _belt_name_for_index(child.get("belt_index"))

    enrolled_ids = {child["id"] for child in selected_roster}
    cur.execute(
        """
        SELECT
            co.id,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(co.end_time, '%H:%i') AS end_label,
            COUNT(ce.id) AS enrolled_count
        FROM class_offerings co
        LEFT JOIN class_enrollments ce ON ce.offering_id = co.id
        WHERE co.class_date >= %s
        GROUP BY co.id, co.class_name, co.class_date, co.start_time, co.end_time
        ORDER BY co.class_date, co.start_time
        """
        ,
        (date.today(),),
    )
    class_roster_counts = cur.fetchall()
    cur.close()
    return render_template(
        "manager_enroll.html",
        offerings=offerings,
        selected_offering_id=selected_offering_id,
        selected_roster=selected_roster,
        all_students=all_students,
        enrolled_ids=enrolled_ids,
        class_roster_counts=class_roster_counts,
    )


@app.route("/manager/classes", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_classes():
    # Manager-controlled class offerings visible for parent signup.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        class_name = request.form.get("class_name", "").strip()
        class_date = request.form.get("class_date", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        instructor_user_id = request.form.get("instructor_user_id", type=int)
        is_recurring_weekly = request.form.get("is_recurring_weekly") == "on"
        recurrence_end_date = request.form.get("recurrence_end_date", "").strip()

        if not (class_name and class_date and start_time and end_time):
            flash("Class name, date, and time are required.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))
        if is_recurring_weekly and not recurrence_end_date:
            flash("Please provide an end date for weekly recurring classes.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))

        try:
            start_day = datetime.strptime(class_date, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid start date.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))

        if is_recurring_weekly:
            try:
                end_day = datetime.strptime(recurrence_end_date, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid recurrence end date.", "error")
                cur.close()
                return redirect(url_for("manager_classes"))
        else:
            end_day = start_day

        if end_day < start_day:
            flash("Recurrence end date must be on or after start date.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))

        inserted_count = 0
        skipped_count = 0
        day_cursor = start_day
        while day_cursor <= end_day:
            cur.execute(
                """
                SELECT id
                FROM class_offerings
                WHERE class_name = %s
                  AND class_date = %s
                  AND start_time = %s
                  AND end_time = %s
                  AND (instructor_user_id <=> %s)
                LIMIT 1
                """,
                (class_name, day_cursor, start_time, end_time, instructor_user_id),
            )
            exists = cur.fetchone()
            if exists:
                skipped_count += 1
            else:
                cur.execute(
                    """
                    INSERT INTO class_offerings
                      (class_name, class_date, start_time, end_time, instructor_user_id, created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        class_name,
                        day_cursor,
                        start_time,
                        end_time,
                        instructor_user_id,
                        session["user_id"],
                    ),
                )
                inserted_count += 1

            if not is_recurring_weekly:
                break
            day_cursor += timedelta(days=7)

        db.commit()
        if is_recurring_weekly:
            flash(
                f"Recurring classes added: {inserted_count}. Skipped duplicates: {skipped_count}.",
                "success",
            )
        else:
            flash("Class offering added.", "success")
        cur.close()
        return redirect(url_for("manager_classes"))

    cur.execute(
        "SELECT id, username FROM users WHERE role = 'employee' ORDER BY username"
    )
    employees = cur.fetchall()
    cur.execute(
        """
        SELECT
            co.id,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(co.end_time, '%H:%i') AS end_label,
            u.username AS instructor_name
        FROM class_offerings co
        LEFT JOIN users u ON u.id = co.instructor_user_id
        ORDER BY co.class_date, co.start_time
        """
    )
    offerings = cur.fetchall()
    cur.close()
    return render_template(
        "manager_classes.html",
        employees=employees,
        offerings=offerings,
    )


@app.route("/techniques", methods=["GET", "POST"])
@login_required
@role_required("employee", "manager")
def techniques():
    # Manage techniques list by kid/adult + belt.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    selected_track = _normalize_track(request.args.get("track", "kid"))
    requested_belt = (request.args.get("belt", BELT_SEQUENCE[0]) or "").strip()
    selected_belt = requested_belt if requested_belt in BELT_SEQUENCE else BELT_SEQUENCE[0]

    if request.method == "POST":
        technique_name = request.form.get("technique_name", "").strip()
        description = request.form.get("description", "").strip()
        program_track = _normalize_track(request.form.get("program_track", "kid"))
        belt_name = request.form.get("belt_name", "").strip()

        if not technique_name:
            flash("Technique name is required.", "error")
            cur.close()
            return redirect(
                url_for("techniques", track=selected_track, belt=selected_belt)
            )
        if belt_name not in BELT_SEQUENCE:
            flash("Please choose a valid belt.", "error")
            cur.close()
            return redirect(
                url_for("techniques", track=selected_track, belt=selected_belt)
            )

        try:
            cur.execute(
                """
                INSERT INTO techniques
                  (technique_name, description, created_by_user_id, program_track, belt_name)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (technique_name, description or "", session["user_id"], program_track, belt_name),
            )
            db.commit()
            flash("Technique added.", "success")
        except Exception:
            db.rollback()
            flash("Technique already exists or could not be added.", "error")

    cur.execute(
        """
        SELECT
            t.id,
            t.technique_name,
            t.description,
            t.is_active,
            t.created_at,
            t.program_track,
            t.belt_name,
            u.username AS created_by
        FROM techniques t
        LEFT JOIN users u ON u.id = t.created_by_user_id
        ORDER BY t.program_track, t.belt_name, t.technique_name
        """
    )
    technique_list = cur.fetchall()
    cur.close()
    return render_template(
        "techniques.html",
        technique_list=technique_list,
        selected_track=selected_track,
        selected_belt=selected_belt,
        belt_sequence=BELT_SEQUENCE,
        program_tracks=PROGRAM_TRACKS,
    )


@app.route("/techniques/<int:technique_id>/edit", methods=["POST"])
@login_required
@role_required("employee", "manager")
def edit_technique(technique_id):
    # Update technique metadata and active/inactive state.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    technique_name = request.form.get("technique_name", "").strip()
    description = request.form.get("description", "").strip()
    extra_comment = request.form.get("extra_comment", "").strip()
    is_active = 1 if request.form.get("is_active") == "on" else 0
    program_track = _normalize_track(request.form.get("program_track", "kid"))
    belt_name = request.form.get("belt_name", "").strip()

    if not technique_name:
        flash("Technique name is required.", "error")
        cur.close()
        return redirect(
            url_for(
                "techniques",
                track=program_track,
                belt=belt_name if belt_name in BELT_SEQUENCE else BELT_SEQUENCE[0],
            )
        )
    if belt_name not in BELT_SEQUENCE:
        flash("Please choose a valid belt.", "error")
        cur.close()
        return redirect(url_for("techniques", track=program_track, belt=BELT_SEQUENCE[0]))

    cur.execute("SELECT id, description FROM techniques WHERE id = %s", (technique_id,))
    existing = cur.fetchone()
    if not existing:
        cur.close()
        flash("Technique not found.", "error")
        return redirect(url_for("techniques"))

    final_description = description
    if extra_comment:
        # Append a simple audit-style comment line into description text.
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        comment_line = f"[{timestamp} {session['username']}] {extra_comment}"
        if final_description:
            final_description = f"{final_description}\n{comment_line}"
        else:
            final_description = comment_line

    try:
        cur.execute(
            """
            UPDATE techniques
            SET technique_name = %s,
                description = %s,
                is_active = %s,
                program_track = %s,
                belt_name = %s
            WHERE id = %s
            """,
            (technique_name, final_description, is_active, program_track, belt_name, technique_id),
        )
        db.commit()
        flash("Technique updated.", "success")
    except Exception:
        db.rollback()
        flash("Could not update technique.", "error")
    finally:
        cur.close()

    return redirect(url_for("techniques", track=program_track, belt=belt_name))


@app.route("/techniques/<int:technique_id>/delete", methods=["POST"])
@login_required
@role_required("employee", "manager")
def delete_technique(technique_id):
    # Delete a technique if it is not currently referenced by child progress records.
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("DELETE FROM techniques WHERE id = %s", (technique_id,))
        if cur.rowcount == 0:
            flash("Technique not found.", "error")
        else:
            db.commit()
            flash("Technique deleted.", "success")
    except Exception:
        db.rollback()
        flash("Technique could not be deleted (it may be in use).", "error")
    finally:
        cur.close()

    return redirect(request.referrer or url_for("techniques"))


@app.route("/progress/<int:progress_id>/toggle", methods=["POST"])
@login_required
@role_required("employee", "manager")
def toggle_progress(progress_id):
    # Increment learned count by 1 (max 3) for quick class updates.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    cur.execute(
        "SELECT id, learned_count FROM child_skill_progress WHERE id = %s",
        (progress_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        flash("Progress item not found.", "error")
        return redirect(url_for("dashboard"))

    new_count = min(LEARNED_TARGET, int(row.get("learned_count") or 0) + 1)
    new_completed = 1 if new_count >= LEARNED_TARGET else 0
    cur.execute(
        """
        UPDATE child_skill_progress
        SET learned_count = %s,
            completed = %s,
            completed_at = CASE WHEN %s = 1 THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = %s
        """,
        (new_count, new_completed, new_completed, progress_id),
    )
    db.commit()
    cur.close()
    flash("Progress updated.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/progress/<int:progress_id>/edit", methods=["POST"])
@login_required
@role_required("employee", "manager")
def edit_progress(progress_id):
    # Update technique and notes for an assigned student progress row.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    technique_id = request.form.get("technique_id", type=int)
    notes = request.form.get("notes", "").strip()
    learned_count = request.form.get("learned_count", type=int) or 0
    learned_count = max(0, min(learned_count, LEARNED_TARGET))

    if not technique_id:
        cur.close()
        flash("Technique is required.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    cur.execute("SELECT id FROM techniques WHERE id = %s", (technique_id,))
    technique = cur.fetchone()
    if not technique:
        cur.close()
        flash("Technique not found.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    completed = 1 if learned_count >= LEARNED_TARGET else 0
    cur.execute(
        """
        UPDATE child_skill_progress
        SET technique_id = %s,
            notes = %s,
            learned_count = %s,
            completed = %s,
            completed_at = CASE WHEN %s = 1 THEN COALESCE(completed_at, CURRENT_TIMESTAMP) ELSE NULL END
        WHERE id = %s
        """,
        (technique_id, notes or None, learned_count, completed, completed, progress_id),
    )
    if cur.rowcount == 0:
        cur.close()
        flash("Progress item not found.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    db.commit()
    cur.close()
    flash("Progress row updated.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/progress/<int:progress_id>/delete", methods=["POST"])
@login_required
@role_required("employee", "manager")
def delete_progress(progress_id):
    # Remove an assigned student progress row.
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("DELETE FROM child_skill_progress WHERE id = %s", (progress_id,))
    if cur.rowcount == 0:
        cur.close()
        flash("Progress item not found.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    db.commit()
    cur.close()
    flash("Progress row deleted.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/manager/requests/<int:request_id>/<action>", methods=["POST"])
@login_required
@role_required("manager")
def process_request(request_id, action):
    # Approve/reject switch and call-out requests, applying shift changes on approval.
    if action not in {"approve", "reject"}:
        flash("Invalid action.", "error")
        return redirect(url_for("manager_dashboard"))

    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute(
        """
        SELECT id, request_type, shift_id, requested_employee_id, status
        FROM requests
        WHERE id = %s
        """,
        (request_id,),
    )
    req = cur.fetchone()

    if not req:
        cur.close()
        flash("Request not found.", "error")
        return redirect(url_for("manager_dashboard"))

    if req["status"] != "pending":
        cur.close()
        flash("Request already processed.", "error")
        return redirect(url_for("manager_dashboard"))

    new_status = "approved" if action == "approve" else "rejected"

    cur.execute("UPDATE requests SET status = %s WHERE id = %s", (new_status, request_id))

    # Apply schedule changes only on approval.
    if action == "approve":
        if req["request_type"] == "switch" and req["requested_employee_id"]:
            cur.execute(
                "UPDATE shifts SET employee_user_id = %s WHERE id = %s",
                (req["requested_employee_id"], req["shift_id"]),
            )
        elif req["request_type"] == "callout":
            cur.execute(
                "UPDATE shifts SET class_name = CONCAT(class_name, ' (CALL-OUT)') WHERE id = %s",
                (req["shift_id"],),
            )

    db.commit()
    cur.close()

    flash(f"Request {new_status}.", "success")
    return redirect(url_for("manager_dashboard"))


# -----------------------------
# Parent views
# -----------------------------
@app.route("/attendance/session/<int:session_id>/summary")
@login_required
@role_required("employee", "manager")
def attendance_summary(session_id):
    # Show post-class save confirmation with student attendance and techniques summary.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    cur.execute(
        """
        SELECT
            ats.id,
            ats.offering_id,
            ats.class_name,
            ats.class_date,
            ats.start_time,
            ats.end_time,
            TIME_FORMAT(ats.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(ats.end_time, '%H:%i') AS end_label,
            ats.created_at,
            u.username AS staff_username
        FROM attendance_sessions ats
        JOIN users u ON u.id = ats.staff_user_id
        WHERE ats.id = %s
        """,
        (session_id,),
    )
    session_row = cur.fetchone()
    if not session_row:
        cur.close()
        flash("Attendance session not found.", "error")
        return redirect(url_for("dashboard"))

    cur.execute(
        """
        SELECT
            ast.child_id,
            c.child_name,
            c.program_track,
            c.belt_index,
            ast.is_present
        FROM attendance_students ast
        JOIN children c ON c.id = ast.child_id
        WHERE ast.attendance_session_id = %s
        ORDER BY c.child_name
        """,
        (session_id,),
    )
    students = cur.fetchall()

    cur.execute(
        """
        SELECT
            atl.child_id,
            t.technique_name,
            t.program_track,
            t.belt_name,
            atl.learned_increment
        FROM attendance_technique_logs atl
        JOIN techniques t ON t.id = atl.technique_id
        WHERE atl.attendance_session_id = %s
        ORDER BY t.technique_name
        """,
        (session_id,),
    )
    logs = cur.fetchall()
    logs_by_child = {}
    for row in logs:
        logs_by_child.setdefault(row["child_id"], []).append(row)

    for student in students:
        student["program_track"] = _normalize_track(student.get("program_track"))
        student["current_belt"] = _belt_name_for_index(student.get("belt_index"))
        student["techniques"] = logs_by_child.get(student["child_id"], [])

    next_class_ref = None
    if session_row.get("offering_id"):
        cur.execute(
            """
            SELECT CONCAT('offering:', co.id) AS class_ref
            FROM class_offerings co
            WHERE (co.class_date > %s)
               OR (co.class_date = %s AND co.start_time > %s)
            ORDER BY co.class_date, co.start_time
            LIMIT 1
            """,
            (
                session_row["class_date"],
                session_row["class_date"],
                session_row["start_time"],
            ),
        )
        next_row = cur.fetchone()
        if next_row:
            next_class_ref = next_row["class_ref"]
    cur.close()

    attendance_endpoint = (
        "manager_attendance" if session.get("role") == "manager" else "employee_attendance"
    )
    next_class_url = (
        url_for(attendance_endpoint, class_ref=next_class_ref)
        if next_class_ref
        else url_for(attendance_endpoint)
    )
    return render_template(
        "attendance_summary.html",
        session_row=session_row,
        students=students,
        next_class_url=next_class_url,
    )


@app.route("/parent/signup/<int:offering_id>/<int:child_id>", methods=["POST"])
@login_required
@role_required("parent")
def parent_signup(offering_id, child_id):
    # Parent signup with 3-classes-per-week validation.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    cur.execute(
        """
        SELECT id, class_date
        FROM class_offerings
        WHERE id = %s
        """,
        (offering_id,),
    )
    offering = cur.fetchone()
    if not offering:
        cur.close()
        flash("Class offering not found.", "error")
        return redirect(url_for("parent_dashboard"))

    cur.execute(
        "SELECT id FROM children WHERE id = %s AND parent_user_id = %s",
        (child_id, session["user_id"]),
    )
    child = cur.fetchone()
    if not child:
        cur.close()
        flash("Student not found for this parent account.", "error")
        return redirect(url_for("parent_dashboard"))

    cur.execute(
        """
        SELECT COUNT(*) AS weekly_count
        FROM class_enrollments ce
        JOIN class_offerings co ON co.id = ce.offering_id
        WHERE ce.child_id = %s
          AND YEARWEEK(co.class_date, 1) = YEARWEEK(%s, 1)
        """,
        (child_id, offering["class_date"]),
    )
    week_count = int((cur.fetchone() or {}).get("weekly_count") or 0)
    if week_count >= MAX_CLASSES_PER_WEEK:
        cur.close()
        flash(
            f"Weekly limit reached: a student can only sign up for {MAX_CLASSES_PER_WEEK} classes.",
            "error",
        )
        return redirect(url_for("parent_dashboard"))

    try:
        cur.execute(
            """
            INSERT INTO class_enrollments (offering_id, child_id, enrolled_by_user_id)
            VALUES (%s, %s, %s)
            """,
            (offering_id, child_id, session["user_id"]),
        )
        db.commit()
        flash("Class signup successful.", "success")
    except Exception:
        db.rollback()
        flash("Class signup failed (you may already be enrolled).", "error")
    finally:
        cur.close()
    return redirect(url_for("parent_dashboard"))


@app.route("/parent")
@login_required
@role_required("parent")
def parent_dashboard():
    # Show parent-facing academy schedule plus linked-student progress details.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    children = _fetch_child_progress_summary(cur, parent_user_id=session["user_id"])
    calendar_start = date.today()
    calendar_end = calendar_start + timedelta(days=13)
    cur.execute(
        """
        SELECT
            s.shift_date,
            s.start_time,
            s.end_time,
            s.class_name,
            u.username AS employee
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        ORDER BY s.shift_date, s.start_time
        """
    )
    academy_schedule = cur.fetchall()

    cur.execute(
        """
        SELECT
            s.shift_date,
            s.class_name,
            u.username AS employee,
            TIME_FORMAT(s.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(s.end_time, '%H:%i') AS end_label
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        WHERE s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time
        """,
        (calendar_start, calendar_end),
    )
    academy_calendar_weeks = _build_two_week_calendar(calendar_start, cur.fetchall())

    cur.execute(
        """
        SELECT
            co.id,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%H:%i') AS start_label,
            TIME_FORMAT(co.end_time, '%H:%i') AS end_label,
            u.username AS instructor_name
        FROM class_offerings co
        LEFT JOIN users u ON u.id = co.instructor_user_id
        WHERE co.class_date >= %s
        ORDER BY co.class_date, co.start_time
        """,
        (date.today(),),
    )
    signup_classes = cur.fetchall()

    weekly_counts = {}
    for child in children:
        cur.execute(
            """
            SELECT
                YEARWEEK(co.class_date, 1) AS week_key,
                COUNT(*) AS class_count
            FROM class_enrollments ce
            JOIN class_offerings co ON co.id = ce.offering_id
            WHERE ce.child_id = %s
            GROUP BY YEARWEEK(co.class_date, 1)
            """,
            (child["id"],),
        )
        weekly_counts[child["id"]] = {
            row["week_key"]: int(row["class_count"]) for row in cur.fetchall()
        }

    child_progress_rows = _fetch_child_progress_rows(cur, [c["id"] for c in children])
    child_parent_notes = _fetch_parent_notes_rows(cur, [c["id"] for c in children])
    cur.close()
    return render_template(
        "parent_dashboard.html",
        children=children,
        child_progress_rows=child_progress_rows,
        academy_schedule=academy_schedule,
        academy_calendar_weeks=academy_calendar_weeks,
        signup_classes=signup_classes,
        weekly_counts=weekly_counts,
        max_classes_per_week=MAX_CLASSES_PER_WEEK,
        child_parent_notes=child_parent_notes,
    )


if __name__ == "__main__":
    app.run(debug=True)
