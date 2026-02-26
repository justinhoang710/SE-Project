import os
import hashlib
import hmac
from datetime import date
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for

from db import close_db, get_db


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-change-me")
app.teardown_appcontext(close_db)


def _fetch_child_progress_summary(cur, parent_user_id=None):
    query = """
        SELECT
            c.id,
            c.child_name,
            COUNT(csp.id) AS total_skills,
            COALESCE(SUM(CASE WHEN csp.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_skills,
            ROUND(
                COALESCE(
                    SUM(CASE WHEN csp.completed = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(csp.id), 0),
                    0
                ),
                0
            ) AS progress_percent
        FROM children c
        LEFT JOIN child_skill_progress csp ON csp.child_id = c.id
    """
    params = ()
    if parent_user_id is not None:
        query += " WHERE c.parent_user_id = %s"
        params = (parent_user_id,)

    query += " GROUP BY c.id, c.child_name ORDER BY c.child_name"
    cur.execute(query, params)
    return cur.fetchall()


def _fetch_child_progress_rows(cur, child_ids):
    if not child_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(child_ids))
    cur.execute(
        f"""
        SELECT
            csp.id,
            csp.child_id,
            csp.completed,
            csp.assigned_at,
            csp.completed_at,
            csp.notes,
            t.technique_name,
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


def hash_password(raw_password: str) -> str:
    digest = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
    return f"sha256${digest}"


def verify_password(stored_hash: str, candidate: str) -> bool:
    if stored_hash.startswith("sha256$"):
        expected = stored_hash.split("$", 1)[1]
        actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, actual)
    return hmac.compare_digest(stored_hash, candidate)


# -----------------------------
# Auth helpers
# -----------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*allowed_roles):
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
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = %s",
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
            flash("Parent registration requires a child name.", "error")
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
    cur.close()

    return render_template(
        "employee_dashboard.html", my_shifts=my_shifts, my_requests=my_requests
    )


@app.route("/employee/schedule")
@login_required
@role_required("employee")
def employee_schedule():
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


@app.route("/employee/request-switch", methods=["GET", "POST"])
@login_required
@role_required("employee")
def request_switch():
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
    db = get_db()
    cur = db.cursor(dictionary=True)

    if request.method == "POST":
        child_id = request.form.get("child_id", type=int)
        technique_id = request.form.get("technique_id", type=int)
        notes = request.form.get("notes", "").strip()
        completed = 1 if request.form.get("completed") == "on" else 0

        cur.execute("SELECT id FROM children WHERE id = %s", (child_id,))
        child = cur.fetchone()
        cur.execute("SELECT id FROM techniques WHERE id = %s AND is_active = 1", (technique_id,))
        technique = cur.fetchone()

        if not child or not technique:
            flash("Please choose a valid child and active technique.", "error")
            cur.close()
            return redirect(request.path)

        cur.execute(
            """
            INSERT INTO child_skill_progress
                (child_id, technique_id, assigned_by_user_id, completed, completed_at, notes)
            VALUES
                (%s, %s, %s, %s, CASE WHEN %s = 1 THEN CURRENT_TIMESTAMP ELSE NULL END, %s)
            """,
            (child_id, technique_id, session["user_id"], completed, completed, notes or None),
        )
        db.commit()
        flash("Progress record added.", "success")

    cur.execute("SELECT id, child_name FROM children ORDER BY child_name")
    children = cur.fetchall()
    cur.execute("SELECT id, technique_name FROM techniques WHERE is_active = 1 ORDER BY technique_name")
    techniques = cur.fetchall()
    child_summary = _fetch_child_progress_summary(cur)
    child_progress_rows = _fetch_child_progress_rows(cur, [c["id"] for c in child_summary])
    cur.close()
    return render_template(
        "progress_screen.html",
        page_title=page_title,
        children=children,
        techniques=techniques,
        child_summary=child_summary,
        child_progress_rows=child_progress_rows,
    )


# -----------------------------
# Manager views
# -----------------------------
@app.route("/manager")
@login_required
@role_required("manager")
def manager_dashboard():
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
    cur.close()

    return render_template(
        "manager_dashboard.html", all_shifts=all_shifts, pending_requests=pending_requests
    )


@app.route("/manager/schedule")
@login_required
@role_required("manager")
def manager_schedule():
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT s.shift_date, s.start_time, s.end_time, s.class_name, u.username AS employee
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        ORDER BY s.shift_date, s.start_time
        """
    )
    all_shifts = cur.fetchall()
    cur.close()
    return render_template("manager_schedule.html", all_shifts=all_shifts)


@app.route("/manager/progress", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_progress():
    return _staff_progress_screen("Child Progress Screen (Manager)")


@app.route("/techniques", methods=["GET", "POST"])
@login_required
@role_required("employee", "manager")
def techniques():
    db = get_db()
    cur = db.cursor(dictionary=True)

    if request.method == "POST":
        technique_name = request.form.get("technique_name", "").strip()
        description = request.form.get("description", "").strip()

        if not technique_name:
            flash("Technique name is required.", "error")
            cur.close()
            return redirect(url_for("techniques"))

        try:
            cur.execute(
                """
                INSERT INTO techniques (technique_name, description, created_by_user_id)
                VALUES (%s, %s, %s)
                """,
                (technique_name, description or "", session["user_id"]),
            )
            db.commit()
            flash("Technique added.", "success")
        except Exception:
            db.rollback()
            flash("Technique already exists or could not be added.", "error")

    cur.execute(
        """
        SELECT t.id, t.technique_name, t.description, t.is_active, t.created_at, u.username AS created_by
        FROM techniques t
        LEFT JOIN users u ON u.id = t.created_by_user_id
        ORDER BY t.technique_name
        """
    )
    technique_list = cur.fetchall()
    cur.close()
    return render_template("techniques.html", technique_list=technique_list)


@app.route("/manager/techniques/<int:technique_id>/edit", methods=["POST"])
@login_required
@role_required("manager")
def edit_technique(technique_id):
    db = get_db()
    cur = db.cursor(dictionary=True)

    technique_name = request.form.get("technique_name", "").strip()
    description = request.form.get("description", "").strip()
    is_active = 1 if request.form.get("is_active") == "on" else 0

    if not technique_name:
        flash("Technique name is required.", "error")
        cur.close()
        return redirect(url_for("techniques"))

    try:
        cur.execute(
            """
            UPDATE techniques
            SET technique_name = %s, description = %s, is_active = %s
            WHERE id = %s
            """,
            (technique_name, description, is_active, technique_id),
        )
        db.commit()
        flash("Technique updated.", "success")
    except Exception:
        db.rollback()
        flash("Could not update technique.", "error")
    finally:
        cur.close()

    return redirect(url_for("techniques"))


@app.route("/progress/<int:progress_id>/toggle", methods=["POST"])
@login_required
@role_required("employee", "manager")
def toggle_progress(progress_id):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id, completed FROM child_skill_progress WHERE id = %s",
        (progress_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        flash("Progress item not found.", "error")
        return redirect(url_for("dashboard"))

    new_completed = 0 if row["completed"] else 1
    cur.execute(
        """
        UPDATE child_skill_progress
        SET completed = %s,
            completed_at = CASE WHEN %s = 1 THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = %s
        """,
        (new_completed, new_completed, progress_id),
    )
    db.commit()
    cur.close()
    flash("Progress updated.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/manager/requests/<int:request_id>/<action>", methods=["POST"])
@login_required
@role_required("manager")
def process_request(request_id, action):
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
@app.route("/parent")
@login_required
@role_required("parent")
def parent_dashboard():
    db = get_db()
    cur = db.cursor(dictionary=True)

    children = _fetch_child_progress_summary(cur, parent_user_id=session["user_id"])
    child_schedules = {}
    for child in children:
        cur.execute(
            """
            SELECT class_date, start_time, end_time, class_title, instructor_name
            FROM child_schedule
            WHERE child_id = %s
            ORDER BY class_date, start_time
            """,
            (child["id"],),
        )
        child_schedules[child["id"]] = cur.fetchall()

    child_progress_rows = _fetch_child_progress_rows(cur, [c["id"] for c in children])
    cur.close()
    return render_template(
        "parent_dashboard.html",
        children=children,
        child_schedules=child_schedules,
        child_progress_rows=child_progress_rows,
    )


if __name__ == "__main__":
    app.run(debug=True)
