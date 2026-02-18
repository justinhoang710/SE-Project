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

    cur.execute(
        "SELECT id, child_name FROM children WHERE parent_user_id = %s ORDER BY child_name",
        (session["user_id"],),
    )
    children = cur.fetchall()

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

    cur.close()
    return render_template(
        "parent_dashboard.html", children=children, child_schedules=child_schedules
    )


if __name__ == "__main__":
    app.run(debug=True)
