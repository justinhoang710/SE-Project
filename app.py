import os
import hashlib
import hmac
import re
from datetime import date, datetime, timedelta
from functools import wraps
from secrets import token_urlsafe

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
TRACK_LABELS = {
    "little_dragons": "Little Dragons (Age 4)",
    "kids_martial_arts": "Kids Martial Arts (Ages 5-14)",
    "adult_martial_arts": "Teen & Adult Martial Arts (Ages 15+)",
}
TRACK_NORMALIZATION = {
    "kid": "kids_martial_arts",
    "adult": "adult_martial_arts",
    "kids": "kids_martial_arts",
    "teens": "adult_martial_arts",
    "teen": "adult_martial_arts",
    "little dragons": "little_dragons",
    "kids martial arts": "kids_martial_arts",
    "teen martial arts": "adult_martial_arts",
    "adult martial arts": "adult_martial_arts",
}
PROGRAM_TRACKS = tuple(TRACK_LABELS.keys())
MAX_CLASSES_PER_WEEK = 3
LEARNED_TARGET = 3
EMPLOYEE_TITLES = ("Assistant", "Assistant Instructor", "Instructor")
MIN_CHILD_AGE = 4
MAX_CHILD_AGE = 99


def _belt_name_for_index(belt_index):
    safe_idx = max(0, min(int(belt_index or 0), len(BELT_SEQUENCE) - 1))
    return BELT_SEQUENCE[safe_idx]


def _belt_bounds_for_offering(offering_row):
    max_belt_allowed = len(BELT_SEQUENCE) - 1
    raw_min_belt = offering_row.get("min_belt_index")
    raw_max_belt = offering_row.get("max_belt_index")
    min_belt = int(raw_min_belt) if raw_min_belt not in (None, "") else 0
    max_belt = (
        int(raw_max_belt) if raw_max_belt not in (None, "") else max_belt_allowed
    )
    min_belt = max(0, min(min_belt, max_belt_allowed))
    max_belt = max(0, min(max_belt, max_belt_allowed))
    if min_belt > max_belt:
        min_belt, max_belt = max_belt, min_belt
    return min_belt, max_belt


def _belt_names_for_offering(offering_row):
    min_belt, max_belt = _belt_bounds_for_offering(offering_row)
    return BELT_SEQUENCE[min_belt : max_belt + 1]


def _normalize_track(value):
    normalized = (value or "").strip().lower().replace("-", "_")
    normalized = TRACK_NORMALIZATION.get(normalized, normalized)
    return normalized if normalized in PROGRAM_TRACKS else "kids_martial_arts"


def _track_label(value):
    normalized = _normalize_track(value)
    return TRACK_LABELS.get(normalized, normalized.replace("_", " ").title())


def _week_start_monday(target_day):
    # Return the Monday date for the provided day.
    safe_day = target_day if isinstance(target_day, date) else date.today()
    return safe_day - timedelta(days=safe_day.weekday())


def _ensure_feature_schema(cur):
    # Keep old local databases compatible by creating/altering new tables on demand.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS class_offerings (
          id INT AUTO_INCREMENT PRIMARY KEY,
          program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts',
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
        "ALTER TABLE children ADD COLUMN program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts'",
        "ALTER TABLE children ADD COLUMN belt_index INT NOT NULL DEFAULT 0",
        "ALTER TABLE children ADD COLUMN child_age INT NOT NULL DEFAULT 14",
        "ALTER TABLE techniques ADD COLUMN program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts'",
        "ALTER TABLE techniques ADD COLUMN belt_name VARCHAR(40) NOT NULL DEFAULT 'White'",
        "ALTER TABLE child_skill_progress ADD COLUMN learned_count TINYINT NOT NULL DEFAULT 0",
        "ALTER TABLE attendance_students ADD COLUMN is_present TINYINT(1) NOT NULL DEFAULT 1",
        "ALTER TABLE attendance_students ADD COLUMN was_signed_up TINYINT(1) NOT NULL DEFAULT 1",
        "ALTER TABLE attendance_sessions ADD COLUMN offering_id INT NULL",
        "ALTER TABLE children ADD COLUMN guardian_name VARCHAR(120) NULL",
        "ALTER TABLE children ADD COLUMN contact_phone VARCHAR(40) NULL",
        "ALTER TABLE requests ADD COLUMN switch_target_status ENUM('pending','accepted','rejected') NOT NULL DEFAULT 'pending'",
        "ALTER TABLE requests ADD COLUMN replacement_employee_id INT NULL",
        "ALTER TABLE requests ADD COLUMN request_date DATE NULL",
        "ALTER TABLE requests MODIFY COLUMN request_type ENUM('switch', 'callout', 'time_off') NOT NULL",
        "ALTER TABLE requests MODIFY COLUMN shift_id INT NULL",
        "ALTER TABLE requests ADD CONSTRAINT fk_requests_replacement_employee FOREIGN KEY (replacement_employee_id) REFERENCES users(id)",
        "ALTER TABLE class_offerings ADD COLUMN program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts'",
        "ALTER TABLE class_offerings ADD COLUMN min_age INT NOT NULL DEFAULT 5",
        "ALTER TABLE class_offerings ADD COLUMN max_age INT NOT NULL DEFAULT 14",
        "ALTER TABLE class_offerings ADD COLUMN min_belt_index INT NOT NULL DEFAULT 0",
        "ALTER TABLE class_offerings ADD COLUMN max_belt_index INT NOT NULL DEFAULT 9",
        "ALTER TABLE shifts ADD COLUMN program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts'",
        "ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL UNIQUE",
        "ALTER TABLE users ADD COLUMN email_verified TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN email_verification_token VARCHAR(120) NULL",
        "ALTER TABLE users ADD COLUMN employee_title VARCHAR(40) NOT NULL DEFAULT 'Assistant'",
    ]
    for statement in alter_statements:
        try:
            cur.execute(statement)
        except Exception:
            pass

    # Backfill old track values and align enum definitions for older databases.
    track_migration_sql = [
        "UPDATE children SET program_track = 'kids_martial_arts' WHERE program_track = 'kid'",
        "UPDATE children SET program_track = 'adult_martial_arts' WHERE program_track = 'adult'",
        "UPDATE children SET program_track = 'adult_martial_arts' WHERE program_track = 'teen_martial_arts'",
        "UPDATE techniques SET program_track = 'kids_martial_arts' WHERE program_track = 'kid'",
        "UPDATE techniques SET program_track = 'adult_martial_arts' WHERE program_track = 'adult'",
        "UPDATE techniques SET program_track = 'adult_martial_arts' WHERE program_track = 'teen_martial_arts'",
        "UPDATE class_offerings SET program_track = 'adult_martial_arts' WHERE program_track = 'teen_martial_arts'",
        "ALTER TABLE children MODIFY COLUMN program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts'",
        "ALTER TABLE techniques MODIFY COLUMN program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts'",
    ]
    for statement in track_migration_sql:
        try:
            cur.execute(statement)
        except Exception:
            pass

    # Collapse duplicate progress rows before enforcing unique pair constraint.
    try:
        cur.execute(
            """
            UPDATE child_skill_progress keep_row
            JOIN (
              SELECT
                child_id,
                technique_id,
                MAX(id) AS keep_id,
                MAX(learned_count) AS max_learned_count,
                MAX(completed) AS max_completed,
                MAX(completed_at) AS max_completed_at
              FROM child_skill_progress
              GROUP BY child_id, technique_id
              HAVING COUNT(*) > 1
            ) dup
              ON keep_row.id = dup.keep_id
            SET
              keep_row.learned_count = GREATEST(keep_row.learned_count, dup.max_learned_count),
              keep_row.completed = GREATEST(keep_row.completed, dup.max_completed),
              keep_row.completed_at = COALESCE(dup.max_completed_at, keep_row.completed_at)
            """
        )
    except Exception:
        pass

    try:
        cur.execute(
            """
            DELETE drop_row
            FROM child_skill_progress drop_row
            JOIN child_skill_progress keep_row
              ON drop_row.child_id = keep_row.child_id
             AND drop_row.technique_id = keep_row.technique_id
             AND drop_row.id < keep_row.id
            """
        )
    except Exception:
        pass

    try:
        cur.execute(
            "ALTER TABLE child_skill_progress ADD UNIQUE KEY uq_child_technique_progress (child_id, technique_id)"
        )
    except Exception:
        pass

    # Create separate SQL views for kid/adult belt placement.
    cur.execute(
        """
        CREATE OR REPLACE VIEW kid_belt_students AS
        SELECT id, child_name, child_name AS student_name, belt_index
        FROM children
        WHERE program_track IN ('little_dragons', 'kids_martial_arts')
        """
    )
    cur.execute(
        """
        CREATE OR REPLACE VIEW adult_belt_students AS
        SELECT id, child_name, child_name AS student_name, belt_index
        FROM children
        WHERE program_track = 'adult_martial_arts'
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
          was_signed_up TINYINT(1) NOT NULL DEFAULT 1,
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_weekly_connections (
          id INT AUTO_INCREMENT PRIMARY KEY,
          user_id INT NOT NULL,
          week_start_date DATE NOT NULL,
          connection_text TEXT NOT NULL,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uq_staff_weekly_connection (user_id, week_start_date),
          FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_class_signups (
          id INT AUTO_INCREMENT PRIMARY KEY,
          offering_id INT NOT NULL,
          staff_user_id INT NOT NULL,
          signed_up_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_staff_class_signup (offering_id, staff_user_id),
          FOREIGN KEY (offering_id) REFERENCES class_offerings(id),
          FOREIGN KEY (staff_user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_history (
          id INT AUTO_INCREMENT PRIMARY KEY,
          shift_id INT NOT NULL,
          activity_type VARCHAR(40) NOT NULL,
          details_text TEXT NOT NULL,
          activity_date DATE NOT NULL,
          actor_user_id INT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (shift_id) REFERENCES shifts(id),
          FOREIGN KEY (actor_user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS outgoing_emails (
          id INT AUTO_INCREMENT PRIMARY KEY,
          to_user_id INT NOT NULL,
          to_email VARCHAR(255) NOT NULL,
          subject_line VARCHAR(255) NOT NULL,
          body_text TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (to_user_id) REFERENCES users(id)
        )
        """
    )


def _predict_test_ready_date(progress_row):
    # Estimate test readiness date from learned_count progression (3 = test ready).
    learned_count = int(progress_row.get("learned_count") or 0)
    if learned_count >= LEARNED_TARGET:
        progress_row["predicted_test_date"] = None
        progress_row["prediction_label"] = "Ready now"
        return

    remaining = max(LEARNED_TARGET - learned_count, 1)
    anchor = progress_row.get("assigned_at")
    if not isinstance(anchor, datetime):
        anchor = datetime.now()
    predicted = (anchor.date() + timedelta(days=remaining * 7))
    progress_row["predicted_test_date"] = predicted
    progress_row["prediction_label"] = predicted.strftime("%b %d, %Y")


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
        _predict_test_ready_date(row)
        grouped[row["child_id"]].append(row)
    return grouped


def _build_two_week_calendar(start_date, shifts, day_count=14):
    # Build a calendar payload grouped into week-sized rows for UI rendering.
    shifts_by_date = {}
    for shift in shifts:
        key = shift["shift_date"].isoformat()
        shifts_by_date.setdefault(key, []).append(shift)

    days = []
    for offset in range(day_count):
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

    return [days[index : index + 7] for index in range(0, len(days), 7)]


def _parse_time_value(value):
    raw_value = (value or "").strip()
    if not raw_value:
        return None
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(raw_value.upper(), fmt).time()
        except ValueError:
            continue
    return None


def _format_time_12h(value):
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = _parse_time_value(value)
        if not parsed:
            return value
        value = parsed
    try:
        return value.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return str(value)


def _format_date_label(value):
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value or "")


def _parse_date_value(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _schedule_window(raw_date=None, raw_view=None):
    anchor = _parse_date_value(raw_date) or date.today()
    view = (raw_view or "biweekly").strip().lower()
    if view not in {"day", "week", "biweekly", "month"}:
        view = "biweekly"

    if view == "day":
        start_day = anchor
        day_count = 1
        label = anchor.strftime("%b %d, %Y")
    elif view == "week":
        start_day = _week_start_monday(anchor)
        day_count = 7
        label = f"{start_day.strftime('%b %d')} - {(start_day + timedelta(days=6)).strftime('%b %d, %Y')}"
    elif view == "month":
        start_day = anchor.replace(day=1)
        if start_day.month == 12:
            next_month = start_day.replace(year=start_day.year + 1, month=1)
        else:
            next_month = start_day.replace(month=start_day.month + 1)
        day_count = (next_month - start_day).days
        label = start_day.strftime("%B %Y")
    else:
        start_day = anchor
        day_count = 14
        label = f"{start_day.strftime('%b %d')} - {(start_day + timedelta(days=13)).strftime('%b %d, %Y')}"

    return {
        "anchor": anchor,
        "view": view,
        "start": start_day,
        "end": start_day + timedelta(days=day_count - 1),
        "day_count": day_count,
        "label": label,
    }


def _shift_hours(start_time, end_time):
    if not start_time or not end_time:
        return 0.0
    if isinstance(start_time, timedelta):
        start_seconds = int(start_time.total_seconds())
    elif isinstance(start_time, str):
        parsed_start = _parse_time_value(start_time)
        if not parsed_start:
            return 0.0
        start_seconds = (parsed_start.hour * 3600) + (parsed_start.minute * 60) + parsed_start.second
    else:
        start_seconds = (start_time.hour * 3600) + (start_time.minute * 60) + start_time.second

    if isinstance(end_time, timedelta):
        end_seconds = int(end_time.total_seconds())
    elif isinstance(end_time, str):
        parsed_end = _parse_time_value(end_time)
        if not parsed_end:
            return 0.0
        end_seconds = (parsed_end.hour * 3600) + (parsed_end.minute * 60) + parsed_end.second
    else:
        end_seconds = (end_time.hour * 3600) + (end_time.minute * 60) + end_time.second

    if end_seconds <= start_seconds:
        return 0.0
    return round((end_seconds - start_seconds) / 3600.0, 2)


def _default_age_range_for_track(track):
    normalized = _normalize_track(track)
    if normalized == "little_dragons":
        return 4, 4
    if normalized == "kids_martial_arts":
        return 5, 14
    return 15, 99


def _queue_parent_email(cur, parent_user_id, subject_line, body_text):
    cur.execute(
        """
        SELECT id, email
        FROM users
        WHERE id = %s
          AND role = 'parent'
        LIMIT 1
        """,
        (parent_user_id,),
    )
    parent = cur.fetchone()
    if not parent or not parent.get("email"):
        return
    cur.execute(
        """
        INSERT INTO outgoing_emails (to_user_id, to_email, subject_line, body_text)
        VALUES (%s, %s, %s, %s)
        """,
        (parent["id"], parent["email"], subject_line, body_text),
    )


def _is_child_eligible_for_offering(child_row, offering_row):
    child_track = _normalize_track(child_row.get("program_track"))
    class_track = _normalize_track(offering_row.get("program_track"))
    if child_track != class_track:
        return False, "Track must match class track"

    child_belt = int(child_row.get("belt_index") or 0)
    min_belt, max_belt = _belt_bounds_for_offering(offering_row)
    if child_belt < min_belt or child_belt > max_belt:
        return False, f"Belt must be {BELT_SEQUENCE[min_belt]}-{BELT_SEQUENCE[max_belt]}"
    return True, ""


def _cleanup_schedule_history(cur):
    cur.execute(
        "DELETE FROM schedule_history WHERE activity_date < %s",
        (date.today(),),
    )


def _user_label_map(cur, user_ids):
    clean_ids = sorted({int(user_id) for user_id in user_ids if user_id})
    if not clean_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(clean_ids))
    cur.execute(
        f"SELECT id, username FROM users WHERE id IN ({placeholders})",
        tuple(clean_ids),
    )
    return {int(row["id"]): row["username"] for row in cur.fetchall()}


def _user_label(user_id, user_labels):
    if not user_id:
        return "unassigned"
    return user_labels.get(int(user_id), f"unknown user #{user_id}")


def _replace_user_ids_in_schedule_history(cur, schedule_history):
    user_ids = set()
    for row in schedule_history:
        details_text = row.get("details_text") or ""
        user_ids.update(
            int(match)
            for match in re.findall(r"\buser\s+(\d+)\b", details_text, re.IGNORECASE)
        )
    user_labels = _user_label_map(cur, user_ids)
    for row in schedule_history:
        details_text = row.get("details_text") or ""

        def replace_match(match):
            user_id = int(match.group(1))
            return user_labels.get(user_id, match.group(0))

        row["details_text"] = re.sub(
            r"\buser\s+(\d+)\b",
            replace_match,
            details_text,
            flags=re.IGNORECASE,
        )
    return schedule_history


def _log_schedule_activity(cur, shift_id, activity_type, details_text, actor_user_id=None):
    cur.execute(
        """
        SELECT shift_date
        FROM shifts
        WHERE id = %s
        """,
        (shift_id,),
    )
    shift = cur.fetchone()
    if not shift:
        return
    cur.execute(
        """
        INSERT INTO schedule_history (shift_id, activity_type, details_text, activity_date, actor_user_id)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (shift_id, activity_type, details_text, shift["shift_date"], actor_user_id),
    )


def _is_valid_time_window(start_time_value, end_time_value):
    start_time = _parse_time_value(start_time_value)
    end_time = _parse_time_value(end_time_value)
    if not start_time or not end_time:
        return False
    return start_time < end_time


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


@app.template_filter("track_label")
def track_label_filter(value):
    return _track_label(value)


@app.template_filter("time12")
def time12_filter(value):
    return _format_time_12h(value)


@app.context_processor
def inject_track_metadata():
    return {
        "track_labels": TRACK_LABELS,
        "program_tracks": PROGRAM_TRACKS,
        "employee_titles": EMPLOYEE_TITLES,
    }


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
        _ensure_feature_schema(cur)
        cur.execute(
            """
            SELECT id, username, password_hash, role, email_verified
            FROM users
            WHERE LOWER(TRIM(username)) = LOWER(%s)
            """,
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
        session["email_verified"] = int(user.get("email_verified") or 0)
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    # Public registration only for parent accounts.
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        email = request.form.get("email", "").strip().lower()
        child_age = request.form.get("child_age", type=int) or 0
        role = "parent"
        student_name = request.form.get("student_name", "").strip()
        if not student_name:
            student_name = request.form.get("child_name", "").strip()

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        if "@" not in email or "." not in email:
            flash("A valid email is required.", "error")
            return render_template("register.html")
        if child_age < MIN_CHILD_AGE or child_age > MAX_CHILD_AGE:
            flash(f"Student age must be between {MIN_CHILD_AGE} and {MAX_CHILD_AGE}.", "error")
            return render_template("register.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        if not student_name:
            flash("Parent/student registration requires a student name.", "error")
            return render_template("register.html")

        db = get_db()
        cur = db.cursor(dictionary=True)
        _ensure_feature_schema(cur)
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()
        if existing:
            cur.close()
            flash("Username already exists. Choose a different username.", "error")
            return render_template("register.html")

        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        email_in_use = cur.fetchone()
        if email_in_use:
            cur.close()
            flash("Email already exists. Use a different email.", "error")
            return render_template("register.html")

        verification_token = token_urlsafe(32)
        cur.execute(
            """
            INSERT INTO users (username, password_hash, role, email, email_verified, email_verification_token)
            VALUES (%s, %s, %s, %s, 0, %s)
            """,
            (username, hash_password(password), role, email, verification_token),
        )
        user_id = cur.lastrowid

        if role == "parent":
            cur.execute(
                """
                INSERT INTO children (child_name, parent_user_id, program_track, child_age)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    student_name,
                    user_id,
                    "little_dragons"
                    if child_age == 4
                    else ("adult_martial_arts" if child_age >= 15 else "kids_martial_arts"),
                    child_age,
                ),
            )

        db.commit()
        cur.close()

        flash(
            f"Registration successful. Verify email: {url_for('verify_email', token=verification_token, _external=False)}",
            "success",
        )
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
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))

    flash("Unknown role.", "error")
    return redirect(url_for("logout"))


@app.route("/verify-email/<token>")
def verify_email(token):
    if not token:
        flash("Invalid verification token.", "error")
        return redirect(url_for("login"))
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    cur.execute(
        """
        SELECT id
        FROM users
        WHERE email_verification_token = %s
        LIMIT 1
        """,
        (token,),
    )
    user = cur.fetchone()
    if not user:
        cur.close()
        flash("Verification link is invalid or expired.", "error")
        return redirect(url_for("login"))
    cur.execute(
        """
        UPDATE users
        SET email_verified = 1,
            email_verification_token = NULL
        WHERE id = %s
        """,
        (user["id"],),
    )
    db.commit()
    cur.close()
    flash("Email verified.", "success")
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account_settings():
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    user_id = session["user_id"]

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        if action == "update_username":
            username = request.form.get("username", "").strip()
            if len(username) < 3:
                flash("Username must be at least 3 characters.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            cur.execute(
                """
                SELECT id
                FROM users
                WHERE LOWER(TRIM(username)) = LOWER(%s)
                  AND id != %s
                """,
                (username, user_id),
            )
            existing_username = cur.fetchone()
            if existing_username:
                flash("Username already exists. Choose a different username.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            cur.execute(
                "UPDATE users SET username = %s WHERE id = %s",
                (username, user_id),
            )
            db.commit()
            session["username"] = username
            flash("Username updated.", "success")
            cur.close()
            return redirect(url_for("account_settings"))

        if action == "update_email":
            email = request.form.get("email", "").strip().lower()
            if "@" not in email or "." not in email:
                flash("Valid email is required.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            cur.execute("SELECT id FROM users WHERE email = %s AND id != %s", (email, user_id))
            existing_email = cur.fetchone()
            if existing_email:
                flash("Email already exists.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            verification_token = token_urlsafe(32)
            cur.execute(
                """
                UPDATE users
                SET email = %s,
                    email_verified = 0,
                    email_verification_token = %s
                WHERE id = %s
                """,
                (email, verification_token, user_id),
            )
            db.commit()
            session["email_verified"] = 0
            flash(
                f"Email updated. Verify email: {url_for('verify_email', token=verification_token, _external=False)}",
                "success",
            )
            cur.close()
            return redirect(url_for("account_settings"))

        if action == "update_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            if new_password != confirm_password:
                flash("Password confirmation does not match.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            cur.execute(
                "SELECT password_hash FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row or not verify_password(row["password_hash"], current_password):
                flash("Current password is incorrect.", "error")
                cur.close()
                return redirect(url_for("account_settings"))
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hash_password(new_password), user_id),
            )
            db.commit()
            flash("Password updated.", "success")
            cur.close()
            return redirect(url_for("account_settings"))

        flash("Invalid account action.", "error")
        cur.close()
        return redirect(url_for("account_settings"))

    cur.execute(
        """
        SELECT username, role, email, email_verified, employee_title
        FROM users
        WHERE id = %s
        """,
        (user_id,),
    )
    account = cur.fetchone()
    cur.close()
    return render_template("account.html", account=account)


@app.route("/manager/staff-accounts", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_staff_accounts():
    # Manager-only staff account creation for employee/manager roles.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        action = request.form.get("action", "create").strip()
        if action == "update_title":
            staff_user_id = request.form.get("staff_user_id", type=int)
            employee_title = request.form.get("employee_title", "").strip()
            if employee_title not in EMPLOYEE_TITLES:
                flash("Please choose a valid employee title.", "error")
                cur.close()
                return redirect(url_for("manager_staff_accounts"))
            cur.execute(
                "UPDATE users SET employee_title = %s WHERE id = %s AND role = 'employee'",
                (employee_title, staff_user_id),
            )
            db.commit()
            flash("Employee title updated.", "success")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "").strip()
        employee_title = request.form.get("employee_title", "").strip() or EMPLOYEE_TITLES[0]

        if role not in {"employee", "manager"}:
            flash("Please choose employee or manager account type.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))
        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))
        if "@" not in email or "." not in email:
            flash("Valid email is required.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))
        if role == "employee" and employee_title not in EMPLOYEE_TITLES:
            flash("Please choose a valid employee title.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))

        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()
        if existing:
            flash("Username already exists.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        existing_email = cur.fetchone()
        if existing_email:
            flash("Email already exists.", "error")
            cur.close()
            return redirect(url_for("manager_staff_accounts"))

        verification_token = token_urlsafe(32)
        cur.execute(
            """
            INSERT INTO users
              (username, password_hash, role, email, email_verified, email_verification_token, employee_title)
            VALUES (%s, %s, %s, %s, 0, %s, %s)
            """,
            (
                username,
                hash_password(password),
                role,
                email,
                verification_token,
                employee_title if role == "employee" else EMPLOYEE_TITLES[0],
            ),
        )
        db.commit()
        flash(f"{role.title()} account created for {username}.", "success")
        cur.close()
        return redirect(url_for("manager_staff_accounts"))

    cur.execute(
        """
        SELECT id, username, role, email, email_verified, employee_title
        FROM users
        WHERE role IN ('manager', 'employee')
        ORDER BY role, username
        """
    )
    staff_accounts = cur.fetchall()
    cur.close()
    return render_template(
        "manager_staff_accounts.html",
        staff_accounts=staff_accounts,
        employee_titles=EMPLOYEE_TITLES,
    )


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
    _ensure_feature_schema(cur)
    cur.execute(
        """
        SELECT s.id, s.shift_date, s.start_time, s.end_time, s.class_name, s.program_track, u.username AS assigned_to
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
        SELECT r.id, r.request_type, r.status, r.reason, r.created_at, r.request_date,
               r.switch_target_status,
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

    cur.execute(
        """
        SELECT
            r.id,
            r.reason,
            r.created_at,
            req.username AS requester,
            s.shift_date,
            s.start_time,
            s.end_time,
            s.class_name,
            s.program_track
        FROM requests r
        JOIN users req ON req.id = r.requester_user_id
        JOIN shifts s ON s.id = r.shift_id
        WHERE r.request_type = 'switch'
          AND r.requested_employee_id = %s
          AND r.switch_target_status = 'pending'
          AND r.status = 'pending'
        ORDER BY r.created_at DESC
        """,
        (session["user_id"],),
    )
    incoming_switch_requests = cur.fetchall()

    schedule_window = _schedule_window(
        request.args.get("schedule_date"),
        request.args.get("schedule_view"),
    )
    calendar_start = schedule_window["start"]
    calendar_end = schedule_window["end"]
    cur.execute(
        """
        SELECT
            s.id,
            s.shift_date,
            s.start_time,
            s.end_time,
            s.class_name,
            TIME_FORMAT(s.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(s.end_time, '%h:%i %p') AS end_label,
            s.program_track
        FROM shifts s
        WHERE s.employee_user_id = %s
          AND s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time
        """,
        (session["user_id"], calendar_start, calendar_end),
    )
    upcoming_shifts = cur.fetchall()
    for row in upcoming_shifts:
        row["hours"] = _shift_hours(row.get("start_time"), row.get("end_time"))
    calendar_weeks = _build_two_week_calendar(
        calendar_start,
        upcoming_shifts,
        schedule_window["day_count"],
    )
    weekly_hours = 0.0
    if upcoming_shifts:
        weekly_hours = round(sum(float(row.get("hours") or 0) for row in upcoming_shifts[:7]), 2)
    cur.close()

    return render_template(
        "employee_dashboard.html",
        my_shifts=my_shifts,
        my_requests=my_requests,
        incoming_switch_requests=incoming_switch_requests,
        calendar_weeks=calendar_weeks,
        schedule_window=schedule_window,
        weekly_hours=weekly_hours,
    )


@app.route("/employee/schedule")
@login_required
@role_required("employee")
def employee_schedule():
    # Render a schedule-only view for the logged-in employee.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    cur.execute(
        """
        SELECT s.shift_date, s.start_time, s.end_time, s.class_name, s.program_track
        FROM shifts s
        WHERE s.employee_user_id = %s
        ORDER BY s.shift_date, s.start_time
        """,
        (session["user_id"],),
    )
    my_shifts = cur.fetchall()
    for shift in my_shifts:
        shift["start_label"] = _format_time_12h(shift.get("start_time"))
        shift["end_label"] = _format_time_12h(shift.get("end_time"))
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
    _ensure_feature_schema(cur)

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
            INSERT INTO requests (request_type, requester_user_id, shift_id, requested_employee_id, reason, status, switch_target_status)
            VALUES ('switch', %s, %s, %s, %s, 'pending', 'pending')
            """,
            (session["user_id"], shift_id, requested_employee_id, reason),
        )
        db.commit()
        cur.close()

        flash("Shift switch request submitted.", "success")
        return redirect(url_for("employee_dashboard"))

    cur.execute(
        """
        SELECT id, shift_date, start_time, end_time, class_name, program_track
        FROM shifts
        WHERE employee_user_id = %s AND shift_date >= %s
        ORDER BY shift_date, start_time
        """,
        (session["user_id"], date.today()),
    )
    my_upcoming_shifts = cur.fetchall()
    for shift in my_upcoming_shifts:
        shift["start_label"] = _format_time_12h(shift.get("start_time"))
        shift["end_label"] = _format_time_12h(shift.get("end_time"))
    shift_ids = {row["id"] for row in my_upcoming_shifts}
    prefill_shift_id = request.args.get("shift_id", type=int)
    prefill_shift_date = (request.args.get("shift_date") or "").strip()
    selected_shift_id = prefill_shift_id if prefill_shift_id in shift_ids else None

    if not selected_shift_id and prefill_shift_date:
        for row in my_upcoming_shifts:
            if str(row.get("shift_date")) == prefill_shift_date:
                selected_shift_id = row["id"]
                break

    cur.execute(
        "SELECT id, username FROM users WHERE role = 'employee' AND id != %s ORDER BY username",
        (session["user_id"],),
    )
    employees = cur.fetchall()
    cur.close()

    return render_template(
        "request_switch.html",
        my_upcoming_shifts=my_upcoming_shifts,
        employees=employees,
        selected_shift_id=selected_shift_id,
    )


@app.route("/employee/request-callout", methods=["GET", "POST"])
@login_required
@role_required("employee")
def request_callout():
    # Let an employee request off for any date, optionally tied to an assigned shift.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        shift_id = request.form.get("shift_id") or None
        request_date = _parse_date_value(request.form.get("request_date", ""))
        reason = request.form.get("reason", "").strip()

        if not request_date:
            flash("Please choose a valid request-off date.", "error")
            cur.close()
            return redirect(url_for("request_callout"))

        if shift_id:
            cur.execute(
                "SELECT id FROM shifts WHERE id = %s AND employee_user_id = %s",
                (shift_id, session["user_id"]),
            )
            owned_shift = cur.fetchone()
            if not owned_shift:
                flash("You can only attach your own shifts to request-off submissions.", "error")
                cur.close()
                return redirect(url_for("request_callout"))

        cur.execute(
            """
            INSERT INTO requests (request_type, requester_user_id, shift_id, request_date, reason, status)
            VALUES ('time_off', %s, %s, %s, %s, 'pending')
            """,
            (session["user_id"], shift_id, request_date, reason),
        )
        db.commit()
        cur.close()

        flash("Request-off submitted.", "success")
        return redirect(url_for("employee_dashboard"))

    cur.execute(
        """
        SELECT id, shift_date, start_time, end_time, class_name, program_track
        FROM shifts
        WHERE employee_user_id = %s AND shift_date >= %s
        ORDER BY shift_date, start_time
        """,
        (session["user_id"], date.today()),
    )
    my_upcoming_shifts = cur.fetchall()
    for shift in my_upcoming_shifts:
        shift["start_label"] = _format_time_12h(shift.get("start_time"))
        shift["end_label"] = _format_time_12h(shift.get("end_time"))
    shift_ids = {row["id"] for row in my_upcoming_shifts}
    prefill_shift_id = request.args.get("shift_id", type=int)
    prefill_shift_date = (request.args.get("shift_date") or "").strip()
    selected_shift_id = prefill_shift_id if prefill_shift_id in shift_ids else None

    if not selected_shift_id and prefill_shift_date:
        for row in my_upcoming_shifts:
            if str(row.get("shift_date")) == prefill_shift_date:
                selected_shift_id = row["id"]
                break

    selected_request_date = _parse_date_value(prefill_shift_date) or date.today()
    if selected_shift_id:
        for row in my_upcoming_shifts:
            if row["id"] == selected_shift_id:
                selected_request_date = row["shift_date"]
                break
    cur.close()

    return render_template(
        "request_callout.html",
        my_upcoming_shifts=my_upcoming_shifts,
        selected_shift_id=selected_shift_id,
        selected_request_date=selected_request_date,
    )


@app.route("/employee/requests/<int:request_id>/<action>", methods=["POST"])
@login_required
@role_required("employee")
def respond_switch_request(request_id, action):
    # Target employee accepts/rejects shift takeover before manager review.
    if action not in {"accept", "reject"}:
        flash("Invalid action.", "error")
        return redirect(url_for("employee_dashboard"))

    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    cur.execute(
        """
        SELECT id, request_type, status, switch_target_status
        FROM requests
        WHERE id = %s
          AND request_type = 'switch'
          AND requested_employee_id = %s
        """,
        (request_id, session["user_id"]),
    )
    req = cur.fetchone()
    if not req:
        cur.close()
        flash("Shift request not found.", "error")
        return redirect(url_for("employee_dashboard"))

    if req["status"] != "pending" or req.get("switch_target_status") != "pending":
        cur.close()
        flash("This shift request was already responded to.", "info")
        return redirect(url_for("employee_dashboard"))

    if action == "accept":
        cur.execute(
            """
            UPDATE requests
            SET switch_target_status = 'accepted'
            WHERE id = %s
            """,
            (request_id,),
        )
        db.commit()
        flash("You accepted the shift takeover request. Manager review is now required.", "success")
    else:
        cur.execute(
            """
            UPDATE requests
            SET switch_target_status = 'rejected',
                status = 'rejected'
            WHERE id = %s
            """,
            (request_id,),
        )
        db.commit()
        flash("You rejected the shift takeover request.", "info")

    cur.close()
    return redirect(url_for("employee_dashboard"))


@app.route("/staff/class-signup", methods=["GET", "POST"])
@login_required
@role_required("employee", "manager")
def staff_class_signup():
    # Allow staff to sign up for published classes as participants.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        offering_id = request.form.get("offering_id", type=int)
        if not offering_id:
            flash("Please choose a valid class.", "error")
            cur.close()
            return redirect(url_for("staff_class_signup"))

        cur.execute(
            """
            SELECT id, class_date
            FROM class_offerings
            WHERE id = %s
              AND program_track = 'adult_martial_arts'
            """,
            (offering_id,),
        )
        offering = cur.fetchone()
        if not offering:
            flash("Staff can only sign up for Teen & Adult classes.", "error")
            cur.close()
            return redirect(url_for("staff_class_signup"))
        if offering["class_date"] < date.today():
            flash("Cannot sign up for a class that already happened.", "error")
            cur.close()
            return redirect(url_for("staff_class_signup"))

        if action == "cancel":
            cur.execute(
                """
                DELETE FROM staff_class_signups
                WHERE offering_id = %s
                  AND staff_user_id = %s
                """,
                (offering_id, session["user_id"]),
            )
            db.commit()
            flash("Staff class signup removed.", "success")
            cur.close()
            return redirect(url_for("staff_class_signup"))

        week_start = _week_start_monday(offering["class_date"])
        week_end = week_start + timedelta(days=6)
        cur.execute(
            """
            SELECT COUNT(*) AS signup_count
            FROM staff_class_signups scs
            JOIN class_offerings co ON co.id = scs.offering_id
            WHERE scs.staff_user_id = %s
              AND co.class_date BETWEEN %s AND %s
              AND co.program_track = 'adult_martial_arts'
              AND co.id != %s
            """,
            (session["user_id"], week_start, week_end, offering_id),
        )
        signup_count = int((cur.fetchone() or {}).get("signup_count") or 0)
        if signup_count >= MAX_CLASSES_PER_WEEK:
            flash("You can only sign up for 3 Teen & Adult classes per week.", "error")
            cur.close()
            return redirect(url_for("staff_class_signup"))

        cur.execute(
            """
            INSERT IGNORE INTO staff_class_signups (offering_id, staff_user_id)
            VALUES (%s, %s)
            """,
            (offering_id, session["user_id"]),
        )
        db.commit()
        if cur.rowcount:
            flash("Signed up for class.", "success")
        else:
            flash("You are already signed up for this class.", "info")
        cur.close()
        return redirect(url_for("staff_class_signup"))

    cur.execute(
        """
        SELECT
            co.id,
            co.class_name,
            co.class_date,
            co.program_track,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label
        FROM class_offerings co
        WHERE co.class_date >= %s
          AND co.program_track = 'adult_martial_arts'
        ORDER BY co.class_date, co.start_time, co.class_name
        """,
        (date.today(),),
    )
    upcoming_classes = cur.fetchall()

    cur.execute(
        """
        SELECT offering_id
        FROM staff_class_signups
        WHERE staff_user_id = %s
        """,
        (session["user_id"],),
    )
    signed_up_ids = {int(row["offering_id"]) for row in cur.fetchall()}

    cur.execute(
        """
        SELECT
            scs.offering_id,
            co.class_name,
            co.class_date,
            co.program_track,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label,
            scs.signed_up_at
        FROM staff_class_signups scs
        JOIN class_offerings co ON co.id = scs.offering_id
        WHERE scs.staff_user_id = %s
          AND co.class_date >= %s
          AND co.program_track = 'adult_martial_arts'
        ORDER BY co.class_date, co.start_time
        """,
        (session["user_id"], date.today()),
    )
    my_class_signups = cur.fetchall()
    cur.close()
    return render_template(
        "staff_class_signup.html",
        upcoming_classes=upcoming_classes,
        my_class_signups=my_class_signups,
        signed_up_ids=signed_up_ids,
    )


@app.route("/staff/connections", methods=["GET", "POST"])
@login_required
@role_required("employee", "manager")
def staff_connections():
    # Shared weekly board: staff update their own weekly connection, staff/manager view all.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    week_start = _week_start_monday(date.today())

    # Keep only current-week entries so the board clears each Monday.
    cur.execute(
        "DELETE FROM staff_weekly_connections WHERE week_start_date < %s",
        (week_start,),
    )
    db.commit()

    if request.method == "POST":
        if session.get("role") != "employee":
            flash("Only staff members can edit personal connections.", "error")
            cur.close()
            return redirect(url_for("staff_connections"))

        connection_text = request.form.get("connection_text", "").strip()
        if not connection_text:
            flash("Please enter your personal connection text.", "error")
            cur.close()
            return redirect(url_for("staff_connections"))

        cur.execute(
            """
            INSERT INTO staff_weekly_connections (user_id, week_start_date, connection_text)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE connection_text = VALUES(connection_text)
            """,
            (session["user_id"], week_start, connection_text),
        )
        db.commit()
        flash("Weekly personal connection saved.", "success")
        cur.close()
        return redirect(url_for("staff_connections"))

    my_connection = ""
    if session.get("role") == "employee":
        cur.execute(
            """
            SELECT connection_text
            FROM staff_weekly_connections
            WHERE user_id = %s
              AND week_start_date = %s
            LIMIT 1
            """,
            (session["user_id"], week_start),
        )
        my_row = cur.fetchone()
        my_connection = (my_row or {}).get("connection_text", "")

    cur.execute(
        """
        SELECT
            u.username,
            swc.connection_text,
            swc.updated_at
        FROM users u
        LEFT JOIN staff_weekly_connections swc
          ON swc.user_id = u.id
         AND swc.week_start_date = %s
        WHERE u.role = 'employee'
        ORDER BY u.username
        """,
        (week_start,),
    )
    board_rows = cur.fetchall()
    cur.close()
    return render_template(
        "staff_connections.html",
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        my_connection=my_connection,
        board_rows=board_rows,
    )


def _staff_progress_screen(page_title):
    # Shared employee/manager student-progress entry and listing screen.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    if request.method == "POST":
        # Route multiple form intents from a single staff progress page.
        action = request.form.get("action", "").strip()

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
            cur.execute(
                "SELECT parent_user_id, child_name FROM children WHERE id = %s",
                (child_id,),
            )
            child_parent = cur.fetchone() or {}

            _ensure_parent_notes_table(cur)
            cur.execute(
                """
                INSERT INTO parent_notes (child_id, author_user_id, note_text)
                VALUES (%s, %s, %s)
                """,
                (child_id, session["user_id"], parent_note),
            )
            if child_parent.get("parent_user_id"):
                _queue_parent_email(
                    cur,
                    child_parent["parent_user_id"],
                    f"New instructor note for {child_parent.get('child_name', 'your student')}",
                    parent_note,
                )
            db.commit()
            flash("Parent note sent.", "success")
        else:
            flash("Progress entries are now created from Attendance only.", "info")
            cur.close()
            return redirect(request.path)

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

    # Keep full technique list for row edits.
    cur.execute(
        """
        SELECT id, technique_name, is_active, program_track, belt_name
        FROM techniques
        ORDER BY program_track, belt_name, technique_name
        """
    )
    all_techniques = cur.fetchall()
    child_summary = _fetch_child_progress_summary(cur)
    child_progress_rows = _fetch_child_progress_rows(cur, [c["id"] for c in child_summary])
    child_parent_notes = _fetch_parent_notes_rows(cur, [c["id"] for c in child_summary])
    cur.close()
    return render_template(
        "progress_screen.html",
        page_title=page_title,
        children=children,
        all_techniques=all_techniques,
        belt_sequence=BELT_SEQUENCE,
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

    def fetch_linked_offering_ids(class_row):
        # Include duplicate offerings with same class signature so roster stays consistent.
        cur.execute(
            """
            SELECT id
            FROM class_offerings
            WHERE class_name = %s
              AND program_track = %s
              AND class_date = %s
              AND start_time = %s
              AND end_time = %s
            """,
            (
                class_row["class_name"],
                class_row["program_track"],
                class_row["class_date"],
                class_row["start_time"],
                class_row["end_time"],
            ),
        )
        return [int(row["id"]) for row in cur.fetchall()]

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        class_ref = request.form.get("class_ref", "").strip()
        present_child_ids = {
            int(value)
            for value in request.form.getlist("present_child_ids")
            if (value or "").isdigit()
        }
        walk_in_child_ids = {
            int(value)
            for value in request.form.getlist("walk_in_child_ids")
            if (value or "").isdigit()
        }
        offering_id = parse_offering_id(class_ref)
        if not offering_id:
            flash("Please choose a valid class.", "error")
            cur.close()
            return redirect(request.path)

        cur.execute(
            """
            SELECT
                id,
                class_name,
                class_date,
                start_time,
                end_time,
                program_track,
                min_belt_index,
                max_belt_index
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
        class_row["program_track"] = _normalize_track(class_row.get("program_track"))
        class_belt_names = _belt_names_for_offering(class_row)
        belt_placeholders = ", ".join(["%s"] * len(class_belt_names))

        cur.execute(
            f"""
            SELECT id
            FROM techniques
            WHERE is_active = 1
              AND program_track = %s
              AND belt_name IN ({belt_placeholders})
            """,
            (class_row["program_track"], *class_belt_names),
        )
        allowed_technique_ids = {int(row["id"]) for row in cur.fetchall()}

        linked_offering_ids = fetch_linked_offering_ids(class_row)
        if not linked_offering_ids:
            linked_offering_ids = [offering_id]
        linked_placeholders = ", ".join(["%s"] * len(linked_offering_ids))
        cur.execute(
            f"SELECT child_id FROM class_enrollments WHERE offering_id IN ({linked_placeholders})",
            tuple(linked_offering_ids),
        )
        enrolled_ids = {int(row["child_id"]) for row in cur.fetchall()}
        if action == "mark_all_present":
            present_child_ids = set(enrolled_ids)
        else:
            present_child_ids = present_child_ids.intersection(enrolled_ids)
        walk_in_child_ids = walk_in_child_ids.difference(enrolled_ids)
        valid_walk_in_ids = set()
        if walk_in_child_ids:
            walk_in_placeholders = ", ".join(["%s"] * len(walk_in_child_ids))
            cur.execute(
                f"""
                SELECT id, program_track, belt_index
                FROM children
                WHERE id IN ({walk_in_placeholders})
                """,
                tuple(walk_in_child_ids),
            )
            for child_row in cur.fetchall():
                eligible, _ = _is_child_eligible_for_offering(child_row, class_row)
                if eligible:
                    valid_walk_in_ids.add(int(child_row["id"]))
        if not enrolled_ids and not valid_walk_in_ids:
            flash("Please choose at least one valid walk-in student.", "error")
            cur.close()
            return redirect(url_for(attendance_endpoint, class_ref=class_ref))
        present_child_ids = present_child_ids.union(valid_walk_in_ids)

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
        for child_id in sorted(valid_walk_in_ids):
            cur.execute(
                """
                INSERT INTO attendance_students (attendance_session_id, child_id, is_present, was_signed_up)
                VALUES (%s, %s, 1, 0)
                """,
                (attendance_session_id, child_id),
            )
        # Queue parent email notices for this attendance record.
        all_attendance_ids = sorted(enrolled_ids.union(valid_walk_in_ids))
        if all_attendance_ids:
            placeholders = ", ".join(["%s"] * len(all_attendance_ids))
            cur.execute(
                f"""
                SELECT id, child_name, parent_user_id
                FROM children
                WHERE id IN ({placeholders})
                """,
                tuple(all_attendance_ids),
            )
            child_map = {int(row["id"]): row for row in cur.fetchall()}
            for child_id in all_attendance_ids:
                child_row = child_map.get(child_id)
                if not child_row:
                    continue
                is_present = child_id in present_child_ids
                status_text = "Present" if is_present else "Absent"
                _queue_parent_email(
                    cur,
                    child_row["parent_user_id"],
                    f"Attendance update: {child_row['child_name']}",
                    f"{class_row['class_date']} {class_row['class_name']} - {status_text}",
                )

        if action == "close_and_apply":
            if not present_child_ids:
                flash("No students were marked present to apply techniques.", "error")
                db.rollback()
                cur.close()
                return redirect(request.path)

            bulk_technique_ids = {
                int(value)
                for value in request.form.getlist("bulk_technique_ids")
                if (value or "").isdigit()
            }
            bulk_technique_ids = bulk_technique_ids.intersection(allowed_technique_ids)
            bulk_learned_increment = request.form.get("bulk_learned_increment", type=int) or 1
            updates = 0
            for child_id in present_child_ids:
                per_student_technique_ids = {
                    int(value)
                    for value in request.form.getlist(f"technique_ids_{child_id}")
                    if (value or "").isdigit()
                }
                per_student_technique_ids = per_student_technique_ids.intersection(
                    allowed_technique_ids
                )
                technique_ids = sorted(per_student_technique_ids.union(bulk_technique_ids))
                for technique_id in technique_ids:
                    learned_increment = (
                        request.form.get(f"learned_increment_{child_id}", type=int) or 1
                    )
                    if technique_id in bulk_technique_ids:
                        learned_increment = bulk_learned_increment
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
                flash("No techniques were selected to apply.", "error")
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
            present_enrolled_count = len(present_child_ids.intersection(enrolled_ids))
            absent_count = max(len(enrolled_ids) - present_enrolled_count, 0)
            walk_in_count = len(valid_walk_in_ids)
            flash(
                f"Attendance saved. Present: {present_count}, Absent: {absent_count}, Didn't sign up: {walk_in_count}.",
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
            co.program_track,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label,
            (
                SELECT COUNT(*)
                FROM class_enrollments ce2
                JOIN class_offerings co2 ON co2.id = ce2.offering_id
                WHERE co2.class_name = co.class_name
                  AND co2.program_track = co.program_track
                  AND co2.class_date = co.class_date
                  AND co2.start_time = co.start_time
                  AND co2.end_time = co.end_time
            ) AS enrolled_count
        FROM class_offerings co
        WHERE co.class_date >= %s
        ORDER BY co.class_date, co.start_time, co.class_name
        """,
        (date.today(),),
    )
    current_classes = cur.fetchall()
    class_map = {row["class_ref"]: row for row in current_classes}
    first_with_enrollments = next(
        (cls for cls in current_classes if int(cls.get("enrolled_count") or 0) > 0),
        None,
    )
    if not selected_class_ref and current_classes:
        selected_class_ref = (
            first_with_enrollments["class_ref"]
            if first_with_enrollments
            else current_classes[0]["class_ref"]
        )
    elif selected_class_ref and first_with_enrollments:
        selected_row = class_map.get(selected_class_ref)
        if not selected_row or int(selected_row.get("enrolled_count") or 0) == 0:
            selected_class_ref = first_with_enrollments["class_ref"]

    selected_offering_id = parse_offering_id(selected_class_ref)
    selected_class_info = None
    child_summary = []
    walk_in_candidates = []
    if selected_offering_id:
        cur.execute(
            """
            SELECT id, program_track, class_name, class_date,
                   min_belt_index, max_belt_index,
                   TIME_FORMAT(start_time, '%h:%i %p') AS start_label,
                   TIME_FORMAT(end_time, '%h:%i %p') AS end_label,
                   start_time, end_time
            FROM class_offerings
            WHERE id = %s
            """,
            (selected_offering_id,),
        )
        selected_class_info = cur.fetchone()
        if selected_class_info:
            selected_class_info["program_track"] = _normalize_track(
                selected_class_info.get("program_track")
            )
            min_belt_index, max_belt_index = _belt_bounds_for_offering(selected_class_info)
            selected_class_info["min_belt_name"] = BELT_SEQUENCE[min_belt_index]
            selected_class_info["max_belt_name"] = BELT_SEQUENCE[max_belt_index]
            linked_offering_ids = fetch_linked_offering_ids(selected_class_info)
            if not linked_offering_ids:
                linked_offering_ids = [selected_offering_id]
            linked_placeholders = ", ".join(["%s"] * len(linked_offering_ids))
            cur.execute(
                f"""
                SELECT c.id, c.child_name, c.program_track, c.belt_index
                FROM class_enrollments ce
                JOIN children c ON c.id = ce.child_id
                WHERE ce.offering_id IN ({linked_placeholders})
                GROUP BY c.id, c.child_name, c.program_track, c.belt_index
                ORDER BY c.child_name
                """,
                tuple(linked_offering_ids),
            )
            enrolled_child_ids = set()
        else:
            linked_offering_ids = []
        child_summary = cur.fetchall()
        enrolled_child_ids = {int(child["id"]) for child in child_summary}
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
        if selected_class_info:
            cur.execute(
                """
                SELECT c.id, c.child_name, c.program_track, c.belt_index
                FROM children c
                WHERE c.program_track = %s
                ORDER BY c.child_name
                """,
                (selected_class_info["program_track"],),
            )
            all_track_children = cur.fetchall()
            for child in all_track_children:
                if int(child["id"]) in enrolled_child_ids:
                    continue
                eligible, _ = _is_child_eligible_for_offering(child, selected_class_info)
                if not eligible:
                    continue
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
                walk_in_candidates.append(child)

    if selected_class_info and selected_class_info.get("program_track"):
        class_belt_names = _belt_names_for_offering(selected_class_info)
        belt_placeholders = ", ".join(["%s"] * len(class_belt_names))
        cur.execute(
            f"""
            SELECT id, technique_name, program_track, belt_name
            FROM techniques
            WHERE is_active = 1
              AND program_track = %s
              AND belt_name IN ({belt_placeholders})
            ORDER BY belt_name, technique_name
            """,
            (selected_class_info["program_track"], *class_belt_names),
        )
    else:
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
        walk_in_candidates=walk_in_candidates,
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
    # Show all shifts and manager review queues for shift changes and time-off.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    _cleanup_schedule_history(cur)
    db.commit()

    cur.execute(
        """
        SELECT s.id, s.shift_date, s.start_time, s.end_time, s.class_name, s.program_track,
               u.username AS employee, u.employee_title
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        ORDER BY s.shift_date, s.start_time
        """
    )
    all_shifts = cur.fetchall()

    cur.execute(
        """
        SELECT r.id, r.status, r.reason, r.created_at,
               req.username AS requester,
               target.username AS requested_employee,
               s.shift_date, s.start_time, s.end_time, s.class_name
        FROM requests r
        JOIN users req ON req.id = r.requester_user_id
        LEFT JOIN users target ON target.id = r.requested_employee_id
        LEFT JOIN shifts s ON s.id = r.shift_id
        WHERE r.request_type = 'switch'
          AND r.status = 'pending'
          AND r.switch_target_status = 'accepted'
        ORDER BY r.created_at ASC
        """
    )
    pending_switch_requests = cur.fetchall()

    cur.execute(
        """
        SELECT r.id, r.status, r.reason, r.created_at,
               req.username AS requester,
               repl.username AS replacement_employee,
               r.request_type, r.request_date,
               s.shift_date, s.start_time, s.end_time, s.class_name
        FROM requests r
        JOIN users req ON req.id = r.requester_user_id
        LEFT JOIN shifts s ON s.id = r.shift_id
        LEFT JOIN users repl ON repl.id = r.replacement_employee_id
        WHERE r.request_type IN ('callout', 'time_off')
          AND r.status = 'pending'
        ORDER BY COALESCE(r.request_date, s.shift_date), r.created_at ASC
        """
    )
    pending_time_off_requests = cur.fetchall()

    cur.execute(
        """
        SELECT r.id, r.status, r.reason, r.created_at,
               req.username AS requester,
               repl.username AS replacement_employee,
               r.request_type, r.request_date,
               s.shift_date, s.start_time, s.end_time, s.class_name
        FROM requests r
        JOIN users req ON req.id = r.requester_user_id
        LEFT JOIN shifts s ON s.id = r.shift_id
        LEFT JOIN users repl ON repl.id = r.replacement_employee_id
        WHERE r.request_type IN ('callout', 'time_off')
        ORDER BY r.created_at DESC
        LIMIT 25
        """
    )
    recent_time_off_requests = cur.fetchall()

    schedule_window = _schedule_window(
        request.args.get("schedule_date"),
        request.args.get("schedule_view"),
    )
    calendar_start = schedule_window["start"]
    calendar_end = schedule_window["end"]
    cur.execute(
        """
        SELECT
            s.id,
            s.shift_date,
            s.class_name,
            s.program_track,
            u.username AS employee,
            u.employee_title,
            TIME_FORMAT(s.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(s.end_time, '%h:%i %p') AS end_label
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        WHERE s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time, u.username
        """,
        (calendar_start, calendar_end),
    )
    upcoming_shifts = cur.fetchall()
    block_map = {}
    for row in upcoming_shifts:
        block_key = (row["shift_date"].isoformat(), row["start_label"], row["end_label"])
        block = block_map.get(block_key)
        if not block:
            block = {
                "shift_date": row["shift_date"],
                "start_label": row["start_label"],
                "end_label": row["end_label"],
                "employees": [],
            }
            block_map[block_key] = block
        label = row["employee"]
        if row.get("employee_title"):
            label = f"{label} ({row['employee_title']})"
        block["employees"].append(label)
    grouped_upcoming_blocks = sorted(
        block_map.values(),
        key=lambda b: (b["shift_date"], b["start_label"], b["end_label"]),
    )
    cur.execute(
        """
        SELECT
            u.id AS employee_user_id,
            u.username,
            u.employee_title,
            ROUND(SUM(TIMESTAMPDIFF(MINUTE, s.start_time, s.end_time)) / 60.0, 2) AS total_hours
        FROM users u
        LEFT JOIN shifts s
          ON s.employee_user_id = u.id
         AND YEARWEEK(s.shift_date, 1) = YEARWEEK(%s, 1)
        WHERE u.role = 'employee'
        GROUP BY u.id, u.username, u.employee_title
        ORDER BY u.username
        """,
        (date.today(),),
    )
    weekly_hours_by_employee = cur.fetchall()
    cur.execute(
        """
        SELECT
            sh.activity_type,
            sh.details_text,
            sh.activity_date,
            sh.created_at,
            actor.username AS actor_username
        FROM schedule_history sh
        LEFT JOIN users actor ON actor.id = sh.actor_user_id
        ORDER BY sh.activity_date DESC, sh.created_at DESC
        LIMIT 60
        """
    )
    schedule_history = cur.fetchall()
    _replace_user_ids_in_schedule_history(cur, schedule_history)
    calendar_weeks = _build_two_week_calendar(
        calendar_start,
        grouped_upcoming_blocks,
        schedule_window["day_count"],
    )
    cur.close()

    return render_template(
        "manager_dashboard.html",
        all_shifts=all_shifts,
        pending_switch_requests=pending_switch_requests,
        pending_time_off_requests=pending_time_off_requests,
        recent_time_off_requests=recent_time_off_requests,
        calendar_weeks=calendar_weeks,
        schedule_window=schedule_window,
        weekly_hours_by_employee=weekly_hours_by_employee,
        schedule_history=schedule_history,
    )


@app.route("/manager/schedule", methods=["GET", "POST"])
@login_required
@role_required("manager")
def manager_schedule():
    # Calendar editor for shared time blocks with multi-employee assignment.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    def parse_selected_day(raw_value):
        return _parse_date_value(raw_value) or date.today()

    def schedule_redirect(day_value, view_value="biweekly"):
        return redirect(
            url_for(
                "manager_schedule",
                day=day_value.isoformat(),
                schedule_view=view_value,
            )
        )

    def _employee_overlap(employee_id, shift_day, start_db, end_db, excluded_ids=None):
        excluded_ids = excluded_ids or []
        if excluded_ids:
            placeholders = ", ".join(["%s"] * len(excluded_ids))
            cur.execute(
                f"""
                SELECT id
                FROM shifts
                WHERE employee_user_id = %s
                  AND shift_date = %s
                  AND id NOT IN ({placeholders})
                  AND NOT (end_time <= %s OR start_time >= %s)
                LIMIT 1
                """,
                (employee_id, shift_day, *excluded_ids, start_db, end_db),
            )
        else:
            cur.execute(
                """
                SELECT id
                FROM shifts
                WHERE employee_user_id = %s
                  AND shift_date = %s
                  AND NOT (end_time <= %s OR start_time >= %s)
                LIMIT 1
                """,
                (employee_id, shift_day, start_db, end_db),
            )
        return cur.fetchone() is not None

    if request.method == "POST":
        action = request.form.get("action", "").strip()
        selected_day = parse_selected_day(
            request.form.get("schedule_date", "").strip()
            or request.form.get("selected_day", "").strip()
        )
        selected_view = request.form.get("schedule_view", "biweekly").strip()
        shift_day = selected_day.isoformat()

        if action == "create_schedule":
            employee_id = request.form.get("employee_user_id", type=int)
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            repeat_weekly = request.form.get("repeat_weekly") == "on"
            repeat_until = selected_day
            if repeat_weekly:
                repeat_until = _parse_date_value(request.form.get("repeat_until", "")) or selected_day

            parsed_start = _parse_time_value(start_time)
            parsed_end = _parse_time_value(end_time)
            if not (employee_id and parsed_start and parsed_end):
                flash("Date, employee, start, and end are required.", "error")
                cur.close()
                return schedule_redirect(selected_day, selected_view)
            start_db = parsed_start.strftime("%H:%M")
            end_db = parsed_end.strftime("%H:%M")
            if not _is_valid_time_window(start_db, end_db):
                flash("End time must be later than start time.", "error")
                cur.close()
                return schedule_redirect(selected_day, selected_view)
            if repeat_until < selected_day:
                flash("Repeat-until date must be on or after the schedule date.", "error")
                cur.close()
                return schedule_redirect(selected_day, selected_view)

            cur.execute(
                "SELECT id, username FROM users WHERE id = %s AND role = 'employee'",
                (employee_id,),
            )
            employee = cur.fetchone()
            if not employee:
                flash("Please choose a valid employee.", "error")
                cur.close()
                return schedule_redirect(selected_day, selected_view)

            occurrence_days = []
            current_day = selected_day
            while current_day <= repeat_until and len(occurrence_days) < 53:
                occurrence_days.append(current_day)
                if not repeat_weekly:
                    break
                current_day = current_day + timedelta(days=7)

            conflicts = [
                day_value
                for day_value in occurrence_days
                if _employee_overlap(employee_id, day_value.isoformat(), start_db, end_db)
            ]
            if conflicts:
                conflict_label = ", ".join(day_value.strftime("%Y-%m-%d") for day_value in conflicts[:5])
                flash(f"Employee already has an overlapping shift on: {conflict_label}.", "error")
                cur.close()
                return schedule_redirect(selected_day, selected_view)

            first_shift_id = None
            for day_value in occurrence_days:
                cur.execute(
                    """
                    INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name, program_track)
                    VALUES (%s, %s, %s, %s, 'Scheduled Shift', 'kids_martial_arts')
                    """,
                    (employee_id, day_value.isoformat(), start_db, end_db),
                )
                if not first_shift_id:
                    first_shift_id = cur.lastrowid

            if first_shift_id:
                repeat_text = "weekly" if repeat_weekly else "once"
                _log_schedule_activity(
                    cur,
                    first_shift_id,
                    "shift_created",
                    (
                        f"Created {repeat_text} schedule for {employee['username']}: "
                        f"{shift_day} {start_db}-{end_db} ({len(occurrence_days)} shift(s))"
                    ),
                    session["user_id"],
                )
            db.commit()
            flash("Schedule created.", "success")
            cur.close()
            return schedule_redirect(selected_day, selected_view)

        if action == "delete_shift":
            shift_id = request.form.get("shift_id", type=int)
            cur.execute(
                """
                SELECT id
                FROM shifts
                WHERE id = %s
                  AND shift_date = %s
                """,
                (shift_id, shift_day),
            )
            shift = cur.fetchone()
            if not shift:
                flash("Shift not found for the selected date.", "error")
                cur.close()
                return schedule_redirect(selected_day, selected_view)
            _log_schedule_activity(
                cur,
                shift_id,
                "shift_deleted",
                f"Deleted shift {shift_day}",
                session["user_id"],
            )
            cur.execute("DELETE FROM shifts WHERE id = %s", (shift_id,))
            db.commit()
            flash("Shift deleted.", "success")
            cur.close()
            return schedule_redirect(selected_day, selected_view)

        flash("Invalid schedule action.", "error")
        cur.close()
        return schedule_redirect(selected_day, selected_view)

    schedule_window = _schedule_window(
        request.args.get("day"),
        request.args.get("schedule_view"),
    )
    selected_day = schedule_window["anchor"]
    calendar_start = schedule_window["start"]
    calendar_end = schedule_window["end"]

    cur.execute(
        "SELECT id, username, employee_title FROM users WHERE role = 'employee' ORDER BY username"
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
            TIME_FORMAT(s.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(s.end_time, '%h:%i %p') AS end_label,
            u.username AS employee,
            u.employee_title
        FROM shifts s
        JOIN users u ON u.id = s.employee_user_id
        WHERE s.shift_date BETWEEN %s AND %s
        ORDER BY s.shift_date, s.start_time, u.username
        """,
        (calendar_start, calendar_end),
    )
    assignments = cur.fetchall()

    block_map = {}
    for row in assignments:
        block_key = (
            row["shift_date"].isoformat(),
            row["start_time"],
            row["end_time"],
        )
        block = block_map.get(block_key)
        if not block:
            block = {
                "shift_date": row["shift_date"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "start_label": row["start_label"],
                "end_label": row["end_label"],
                "employees": [],
            }
            block_map[block_key] = block
        label = row["employee"]
        if row.get("employee_title"):
            label = f"{label} ({row['employee_title']})"
        block["employees"].append(label)

    blocks = sorted(
        block_map.values(),
        key=lambda b: (b["shift_date"], b["start_time"], b["end_time"]),
    )
    calendar_weeks = _build_two_week_calendar(
        calendar_start,
        blocks,
        schedule_window["day_count"],
    )
    selected_day_key = selected_day.isoformat()
    selected_day_assignments = []
    for week in calendar_weeks:
        for day in week:
            day["is_selected"] = day["iso_date"] == selected_day_key
    for row in assignments:
        if row["shift_date"].isoformat() == selected_day_key:
            label = row["employee"]
            if row.get("employee_title"):
                label = f"{label} ({row['employee_title']})"
            selected_day_assignments.append(
                {
                    "id": row["id"],
                    "start_label": row["start_label"],
                    "end_label": row["end_label"],
                    "employee": label,
                }
            )

    cur.execute(
        """
        SELECT
            u.id AS employee_user_id,
            u.username,
            u.employee_title,
            ROUND(SUM(TIMESTAMPDIFF(MINUTE, s.start_time, s.end_time)) / 60.0, 2) AS total_hours
        FROM users u
        LEFT JOIN shifts s
          ON s.employee_user_id = u.id
         AND YEARWEEK(s.shift_date, 1) = YEARWEEK(%s, 1)
        WHERE u.role = 'employee'
        GROUP BY u.id, u.username, u.employee_title
        ORDER BY u.username
        """,
        (selected_day,),
    )
    weekly_hours_by_employee = cur.fetchall()
    cur.close()

    return render_template(
        "manager_schedule.html",
        calendar_weeks=calendar_weeks,
        employees=employees,
        selected_day=selected_day,
        schedule_window=schedule_window,
        selected_day_assignments=selected_day_assignments,
        weekly_hours_by_employee=weekly_hours_by_employee,
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
        action = request.form.get("action", "enroll_students").strip()
        if action == "create_student":
            child_name = request.form.get("child_name", "").strip()
            parent_user_id = request.form.get("parent_user_id", type=int)
            program_track = _normalize_track(request.form.get("program_track", "kids_martial_arts"))
            belt_index = request.form.get("belt_index", type=int) or 0
            child_age = request.form.get("child_age", type=int) or 0
            guardian_name = request.form.get("guardian_name", "").strip()
            contact_phone = request.form.get("contact_phone", "").strip()

            belt_index = max(0, min(belt_index, len(BELT_SEQUENCE) - 1))
            if not (child_name and parent_user_id and contact_phone and child_age):
                flash("Student name, age, parent account, and contact phone are required.", "error")
                cur.close()
                return redirect(url_for("manager_enroll"))
            if child_age < MIN_CHILD_AGE or child_age > MAX_CHILD_AGE:
                flash(f"Student age must be between {MIN_CHILD_AGE} and {MAX_CHILD_AGE}.", "error")
                cur.close()
                return redirect(url_for("manager_enroll"))
            if program_track == "little_dragons" and child_age != 4:
                flash("Little Dragons track is only for age 4.", "error")
                cur.close()
                return redirect(url_for("manager_enroll"))
            if program_track == "kids_martial_arts" and child_age > 14:
                flash("Kids Martial Arts is for ages up to 14.", "error")
                cur.close()
                return redirect(url_for("manager_enroll"))
            if program_track == "adult_martial_arts" and child_age < 15:
                flash("Teen & Adult Martial Arts starts at age 15.", "error")
                cur.close()
                return redirect(url_for("manager_enroll"))

            cur.execute(
                "SELECT id FROM users WHERE id = %s AND role = 'parent'",
                (parent_user_id,),
            )
            parent_user = cur.fetchone()
            if not parent_user:
                flash("Selected parent account is invalid.", "error")
                cur.close()
                return redirect(url_for("manager_enroll"))

            cur.execute(
                """
                INSERT INTO children
                  (child_name, parent_user_id, program_track, belt_index, child_age, guardian_name, contact_phone)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    child_name,
                    parent_user_id,
                    program_track,
                    belt_index,
                    child_age,
                    guardian_name or None,
                    contact_phone,
                ),
            )
            db.commit()
            cur.close()
            flash("Student profile created.", "success")
            return redirect(url_for("manager_enroll"))

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
            SELECT id, program_track, min_age, max_age, min_belt_index, max_belt_index
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
        skipped = 0
        for child_id in child_ids:
            cur.execute(
                """
                SELECT id, program_track, belt_index, child_age
                FROM children
                WHERE id = %s
                """,
                (child_id,),
            )
            child = cur.fetchone()
            eligible, _ = _is_child_eligible_for_offering(child or {}, offering)
            if not eligible:
                skipped += 1
                continue
            cur.execute(
                """
                INSERT IGNORE INTO class_enrollments (offering_id, child_id, enrolled_by_user_id)
                VALUES (%s, %s, %s)
                """,
                (offering_id, child_id, session["user_id"]),
            )
            added += cur.rowcount

        db.commit()
        flash(f"Added {added} student(s) to class roster. Skipped {skipped} not eligible.", "success")
        cur.close()
        return redirect(url_for("manager_enroll", offering_id=offering_id))

    selected_offering_id = request.args.get("offering_id", type=int)
    cur.execute(
        """
        SELECT
            co.id,
            co.program_track,
            co.class_name,
            co.class_date,
            co.min_age,
            co.max_age,
            co.min_belt_index,
            co.max_belt_index,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label
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
            SELECT c.id, c.child_name, c.program_track, c.belt_index, c.child_age
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
        SELECT c.id, c.child_name, c.program_track, c.belt_index, c.child_age, u.username AS parent_username
        FROM children c
        JOIN users u ON u.id = c.parent_user_id
        ORDER BY c.child_name
        """
    )
    all_students = cur.fetchall()
    selected_offering = next((row for row in offerings if row["id"] == selected_offering_id), None)
    for child in all_students:
        child["program_track"] = _normalize_track(child.get("program_track"))
        child["current_belt"] = _belt_name_for_index(child.get("belt_index"))
        if selected_offering:
            eligible, reason = _is_child_eligible_for_offering(child, selected_offering)
            child["eligible_for_selected"] = eligible
            child["ineligible_reason"] = reason
        else:
            child["eligible_for_selected"] = True
            child["ineligible_reason"] = ""

    enrolled_ids = {child["id"] for child in selected_roster}
    cur.execute(
        "SELECT id, username FROM users WHERE role = 'parent' ORDER BY username"
    )
    parent_accounts = cur.fetchall()
    cur.execute(
        """
        SELECT
            co.id,
            co.program_track,
            co.class_name,
            co.class_date,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label,
            COUNT(ce.id) AS enrolled_count
        FROM class_offerings co
        LEFT JOIN class_enrollments ce ON ce.offering_id = co.id
        WHERE co.class_date >= %s
        GROUP BY co.id, co.program_track, co.class_name, co.class_date, co.start_time, co.end_time
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
        parent_accounts=parent_accounts,
        belt_sequence=BELT_SEQUENCE,
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
    current_track = _normalize_track(request.form.get("program_track") or request.args.get("track") or "kids_martial_arts")

    if request.method == "POST":
        action = request.form.get("action", "create_class").strip()
        if action == "delete_class":
            offering_id = request.form.get("offering_id", type=int)
            if not offering_id:
                flash("Please choose a valid class offering.", "error")
                cur.close()
                return redirect(url_for("manager_classes"))

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
                flash("Class offering not found.", "error")
                cur.close()
                return redirect(url_for("manager_classes"))
            if offering["class_date"] < date.today():
                flash("Cannot delete a class that already happened.", "error")
                cur.close()
                return redirect(url_for("manager_classes"))

            cur.execute(
                """
                SELECT id
                FROM attendance_sessions
                WHERE offering_id = %s
                LIMIT 1
                """,
                (offering_id,),
            )
            if cur.fetchone():
                flash("Cannot delete after attendance has been recorded.", "error")
                cur.close()
                return redirect(url_for("manager_classes"))

            try:
                cur.execute("DELETE FROM staff_class_signups WHERE offering_id = %s", (offering_id,))
                cur.execute("DELETE FROM class_enrollments WHERE offering_id = %s", (offering_id,))
                cur.execute("DELETE FROM class_offerings WHERE id = %s", (offering_id,))
                db.commit()
                flash("Class offering deleted.", "success")
            except Exception:
                db.rollback()
                flash("Class offering could not be deleted.", "error")
            finally:
                cur.close()
            return redirect(url_for("manager_classes"))

        if action != "create_class":
            flash("Invalid class action.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))

        program_track = _normalize_track(request.form.get("program_track", "kids_martial_arts"))
        current_track = program_track
        class_name = request.form.get("class_name", "").strip()
        class_date = request.form.get("class_date", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        min_belt_index = request.form.get("min_belt_index", type=int)
        max_belt_index = request.form.get("max_belt_index", type=int)
        is_recurring_weekly = request.form.get("is_recurring_weekly") == "on"
        recurrence_end_date = request.form.get("recurrence_end_date", "").strip()
        default_min_age, default_max_age = _default_age_range_for_track(program_track)
        min_age = default_min_age
        max_age = default_max_age
        min_belt_index = min_belt_index if min_belt_index is not None else 0
        max_belt_index = max_belt_index if max_belt_index is not None else len(BELT_SEQUENCE) - 1

        if not (class_name and class_date and start_time and end_time):
            flash("Class name, date, and time are required.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))
        parsed_start = _parse_time_value(start_time)
        parsed_end = _parse_time_value(end_time)
        if not parsed_start or not parsed_end:
            flash("Please enter valid start and end times (example: 4:30 PM).", "error")
            cur.close()
            return redirect(url_for("manager_classes"))
        start_db = parsed_start.strftime("%H:%M")
        end_db = parsed_end.strftime("%H:%M")
        if not _is_valid_time_window(start_db, end_db):
            flash("End time must be later than start time.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))
        if min_age < MIN_CHILD_AGE or max_age > MAX_CHILD_AGE or min_age > max_age:
            flash("Invalid age range.", "error")
            cur.close()
            return redirect(url_for("manager_classes"))
        max_belt_allowed = len(BELT_SEQUENCE) - 1
        if min_belt_index < 0 or max_belt_index > max_belt_allowed or min_belt_index > max_belt_index:
            flash("Invalid belt range.", "error")
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
        if start_day < date.today():
            flash("Cannot create class offerings in the past.", "error")
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
                  AND program_track = %s
                  AND class_date = %s
                  AND start_time = %s
                  AND end_time = %s
                LIMIT 1
                """,
                (class_name, program_track, day_cursor, start_db, end_db),
            )
            exists = cur.fetchone()
            if exists:
                skipped_count += 1
            else:
                cur.execute(
                    """
                    INSERT INTO class_offerings
                      (program_track, class_name, class_date, start_time, end_time, min_age, max_age, min_belt_index, max_belt_index, instructor_user_id, created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        program_track,
                        class_name,
                        day_cursor,
                        start_db,
                        end_db,
                        min_age,
                        max_age,
                        min_belt_index,
                        max_belt_index,
                        None,
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
        """
        SELECT
            co.id,
            co.program_track,
            co.class_name,
            co.class_date,
            co.min_age,
            co.max_age,
            co.min_belt_index,
            co.max_belt_index,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label
        FROM class_offerings co
        ORDER BY co.class_date, co.start_time
        """
    )
    offerings = cur.fetchall()
    cur.close()
    return render_template(
        "manager_classes.html",
        offerings=offerings,
        selected_track=current_track,
        belt_sequence=BELT_SEQUENCE,
    )


@app.route("/techniques", methods=["GET", "POST"])
@login_required
@role_required("manager")
def techniques():
    # Manage techniques list by kid/adult + belt.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    selected_track = _normalize_track(request.args.get("track", "kids_martial_arts"))
    requested_belt = (request.args.get("belt", BELT_SEQUENCE[0]) or "").strip()
    selected_belt = requested_belt if requested_belt in BELT_SEQUENCE else BELT_SEQUENCE[0]

    if request.method == "POST":
        technique_name = request.form.get("technique_name", "").strip()
        description = request.form.get("description", "").strip()
        program_track = _normalize_track(request.form.get("program_track", "kids_martial_arts"))
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
        WHERE t.program_track = %s
          AND t.belt_name = %s
        ORDER BY t.technique_name
        """
        ,
        (selected_track, selected_belt),
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
@role_required("manager")
def edit_technique(technique_id):
    # Update technique metadata and active/inactive state.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    technique_name = request.form.get("technique_name", "").strip()
    description = request.form.get("description", "").strip()
    extra_comment = request.form.get("extra_comment", "").strip()
    is_active = 1 if request.form.get("is_active") == "on" else 0
    program_track = _normalize_track(request.form.get("program_track", "kids_martial_arts"))
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
@role_required("manager")
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
    # Approve/reject switch, legacy call-out, and request-off submissions.
    if action not in {"approve", "reject"}:
        flash("Invalid action.", "error")
        return redirect(url_for("manager_dashboard"))

    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    cur.execute(
        """
        SELECT id, request_type, shift_id, requested_employee_id, replacement_employee_id,
               request_date, status, switch_target_status
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

    if req["request_type"] == "switch" and req.get("switch_target_status") != "accepted":
        cur.close()
        flash("Target employee must accept the shift takeover before manager approval.", "error")
        return redirect(url_for("manager_dashboard"))

    new_status = "approved" if action == "approve" else "rejected"

    cur.execute("UPDATE requests SET status = %s WHERE id = %s", (new_status, request_id))

    # Apply schedule changes only on approval.
    if action == "approve":
        if req["request_type"] == "switch" and req["requested_employee_id"]:
            cur.execute(
                "SELECT employee_user_id FROM shifts WHERE id = %s",
                (req["shift_id"],),
            )
            shift_before = cur.fetchone() or {}
            user_labels = _user_label_map(
                cur,
                [shift_before.get("employee_user_id"), req["requested_employee_id"]],
            )
            cur.execute(
                "UPDATE shifts SET employee_user_id = %s WHERE id = %s",
                (req["requested_employee_id"], req["shift_id"]),
            )
            _log_schedule_activity(
                cur,
                req["shift_id"],
                "switch_approved",
                (
                    "Shift moved from "
                    f"{_user_label(shift_before.get('employee_user_id'), user_labels)} "
                    f"to {_user_label(req['requested_employee_id'], user_labels)}"
                ),
                session["user_id"],
            )
        elif req["request_type"] == "callout":
            replacement_id = req.get("replacement_employee_id")
            if replacement_id:
                cur.execute(
                    "SELECT employee_user_id FROM shifts WHERE id = %s",
                    (req["shift_id"],),
                )
                shift_before = cur.fetchone() or {}
                user_labels = _user_label_map(
                    cur,
                    [shift_before.get("employee_user_id"), replacement_id],
                )
                cur.execute(
                    "UPDATE shifts SET employee_user_id = %s WHERE id = %s",
                    (replacement_id, req["shift_id"]),
                )
                _log_schedule_activity(
                    cur,
                    req["shift_id"],
                    "callout_approved",
                    (
                        "Shift moved from "
                        f"{_user_label(shift_before.get('employee_user_id'), user_labels)} "
                        f"to replacement {_user_label(replacement_id, user_labels)}"
                    ),
                    session["user_id"],
                )
            else:
                _log_schedule_activity(
                    cur,
                    req["shift_id"],
                    "callout_approved",
                    "Callout approved without replacement employee",
                    session["user_id"],
                )
        elif req["request_type"] == "time_off":
            pass
        else:
            cur.execute(
                "SELECT id FROM shifts WHERE id = %s",
                (req["shift_id"],),
            )
    else:
        if req.get("shift_id"):
            _log_schedule_activity(
                cur,
                req["shift_id"],
                f"{req['request_type']}_rejected",
                f"Request {request_id} rejected",
                session["user_id"],
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
            TIME_FORMAT(ats.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(ats.end_time, '%h:%i %p') AS end_label,
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
            ast.is_present,
            ast.was_signed_up
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
        SELECT
            id,
            class_name,
            class_date,
            start_time,
            end_time,
            program_track,
            min_age,
            max_age,
            min_belt_index,
            max_belt_index
        FROM class_offerings
        WHERE id = %s
        """,
        (offering_id,),
    )
    offering = cur.fetchone()
    if not offering:
        cur.close()
        flash("Class offering not found.", "error")
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))
    if offering["class_date"] < date.today():
        cur.close()
        flash("Cannot sign up for a class that already happened.", "error")
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))

    cur.execute(
        """
        SELECT id, program_track, belt_index, child_age
        FROM children
        WHERE id = %s
          AND parent_user_id = %s
        """,
        (child_id, session["user_id"]),
    )
    child = cur.fetchone()
    if not child:
        cur.close()
        flash("Student not found for this parent account.", "error")
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))

    cur.execute(
        """
        SELECT ce.id
        FROM class_enrollments ce
        JOIN class_offerings co ON co.id = ce.offering_id
        WHERE ce.child_id = %s
          AND co.class_name = %s
          AND co.program_track = %s
          AND co.class_date = %s
          AND co.start_time = %s
          AND co.end_time = %s
        LIMIT 1
        """,
        (
            child_id,
            offering["class_name"],
            offering["program_track"],
            offering["class_date"],
            offering["start_time"],
            offering["end_time"],
        ),
    )
    existing_enrollment = cur.fetchone()
    if existing_enrollment:
        cur.close()
        flash("Student is already enrolled in this class.", "info")
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))

    eligible, reason = _is_child_eligible_for_offering(child, offering)
    if not eligible:
        cur.close()
        flash(f"Student not eligible for this class: {reason}.", "error")
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))

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
        return redirect(url_for("parent_dashboard", _anchor="class-signup"))

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
    except Exception as exc:
        db.rollback()
        if getattr(exc, "errno", None) == 1062:
            flash("Student is already enrolled in this class.", "info")
        else:
            flash(f"Class signup failed: {exc}", "error")
    finally:
        cur.close()
    return redirect(url_for("parent_dashboard", _anchor="class-signup"))


@app.route("/parent/signup/<int:offering_id>/<int:child_id>/cancel", methods=["POST"])
@login_required
@role_required("parent")
def parent_cancel_signup(offering_id, child_id):
    # Parent-owned signup cancellation before attendance has been recorded.
    return_to = request.form.get("return_to", "").strip()
    redirect_target = (
        url_for("parent_children_dashboard")
        if return_to == "children"
        else url_for("parent_dashboard", _anchor="class-signup")
    )

    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)

    cur.execute(
        """
        SELECT
            co.class_name,
            co.class_date,
            co.start_time,
            co.end_time,
            co.program_track
        FROM class_enrollments ce
        JOIN class_offerings co ON co.id = ce.offering_id
        JOIN children c ON c.id = ce.child_id
        WHERE ce.offering_id = %s
          AND ce.child_id = %s
          AND c.parent_user_id = %s
        LIMIT 1
        """,
        (offering_id, child_id, session["user_id"]),
    )
    enrollment = cur.fetchone()
    if not enrollment:
        cur.close()
        flash("Class signup not found for this parent account.", "error")
        return redirect(redirect_target)

    if enrollment["class_date"] < date.today():
        cur.close()
        flash("Cannot cancel a class that already happened.", "error")
        return redirect(redirect_target)

    cur.execute(
        """
        SELECT ast.id
        FROM attendance_students ast
        JOIN attendance_sessions ats ON ats.id = ast.attendance_session_id
        JOIN class_offerings co ON co.id = ats.offering_id
        WHERE co.class_name = %s
          AND co.program_track = %s
          AND co.class_date = %s
          AND co.start_time = %s
          AND co.end_time = %s
          AND ast.child_id = %s
        LIMIT 1
        """,
        (
            enrollment["class_name"],
            enrollment["program_track"],
            enrollment["class_date"],
            enrollment["start_time"],
            enrollment["end_time"],
            child_id,
        ),
    )
    if cur.fetchone():
        cur.close()
        flash("Cannot cancel after attendance has been recorded.", "error")
        return redirect(redirect_target)

    try:
        cur.execute(
            """
            DELETE ce
            FROM class_enrollments ce
            JOIN children c ON c.id = ce.child_id
            WHERE ce.offering_id = %s
              AND ce.child_id = %s
              AND c.parent_user_id = %s
            """,
            (offering_id, child_id, session["user_id"]),
        )
        db.commit()
        flash("Class signup canceled.", "success")
    except Exception as exc:
        db.rollback()
        flash(f"Class signup cancellation failed: {exc}", "error")
    finally:
        cur.close()

    return redirect(redirect_target)


def _build_parent_children_payload(cur, parent_user_id):
    signup_start = date.today()
    signup_end = signup_start + timedelta(days=13)
    cur.execute(
        """
        SELECT id, child_name, program_track, belt_index, child_age
        FROM children
        WHERE parent_user_id = %s
        ORDER BY child_name
        """,
        (parent_user_id,),
    )
    children = cur.fetchall()
    for child in children:
        child["program_track"] = _normalize_track(child.get("program_track"))
        child["current_belt"] = _belt_name_for_index(child.get("belt_index"))

    cur.execute(
        """
        SELECT
            co.id,
            co.program_track,
            co.class_name,
            co.class_date,
            DATE_FORMAT(co.class_date, '%W') AS class_day_name,
            YEARWEEK(co.class_date, 1) AS week_key,
            co.min_age,
            co.max_age,
            co.min_belt_index,
            co.max_belt_index,
            co.start_time,
            co.end_time,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label
        FROM class_offerings co
        WHERE co.class_date BETWEEN %s AND %s
        ORDER BY co.class_date, co.start_time
        """,
        (signup_start, signup_end),
    )
    signup_classes = cur.fetchall()
    child_ids = [c["id"] for c in children]
    signed_up_classes_by_child = {child_id: [] for child_id in child_ids}
    enrolled_lookup = {}
    cancelable_enrollment_lookup = {}
    enrollment_offering_lookup = {}
    weekly_counts = {}
    signup_block_reasons = {}

    def offering_signature(row):
        return (
            row.get("class_name"),
            _normalize_track(row.get("program_track")),
            row.get("class_date"),
            row.get("start_time"),
            row.get("end_time"),
        )

    signup_classes_by_signature = {}
    for cls in signup_classes:
        cls["program_track"] = _normalize_track(cls.get("program_track"))
        min_belt, max_belt = _belt_bounds_for_offering(cls)
        cls["min_belt_index"] = min_belt
        cls["max_belt_index"] = max_belt
        signup_classes_by_signature.setdefault(offering_signature(cls), []).append(cls)

    if child_ids:
        placeholders = ", ".join(["%s"] * len(child_ids))
        cur.execute(
            f"""
            SELECT
                ce.child_id,
                YEARWEEK(co.class_date, 1) AS week_key,
                COUNT(*) AS week_count
            FROM class_enrollments ce
            JOIN class_offerings co ON co.id = ce.offering_id
            WHERE ce.child_id IN ({placeholders})
            GROUP BY ce.child_id, YEARWEEK(co.class_date, 1)
            """,
            tuple(child_ids),
        )
        for row in cur.fetchall():
            weekly_counts[(int(row["child_id"]), int(row["week_key"]))] = int(
                row["week_count"] or 0
            )

        cur.execute(
            f"""
            SELECT
                ce.child_id,
                co.id AS offering_id,
                co.program_track,
                co.class_name,
                co.class_date,
                co.min_age,
                co.max_age,
                co.min_belt_index,
                co.max_belt_index,
                co.start_time,
                co.end_time,
                TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
                TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label,
                ce.created_at AS enrolled_at
            FROM class_enrollments ce
            JOIN class_offerings co ON co.id = ce.offering_id
            WHERE ce.child_id IN ({placeholders})
            ORDER BY co.class_date DESC, co.start_time DESC
            """,
            tuple(child_ids),
        )
        signup_rows = cur.fetchall()
        for row in signup_rows:
            row["attendance_status"] = "Not Recorded"
            row["can_cancel"] = bool(
                row.get("class_date") and row["class_date"] >= date.today()
            )
            signed_up_classes_by_child[row["child_id"]].append(row)
            key_child = int(row["child_id"])
            key_offering = int(row["offering_id"])
            enrolled_lookup.setdefault(key_child, set()).add(key_offering)
            linked_classes = signup_classes_by_signature.get(offering_signature(row), [])
            if not linked_classes:
                linked_classes = [{"id": key_offering}]
            for linked_cls in linked_classes:
                linked_offering_id = int(linked_cls["id"])
                linked_key = f"{key_child}:{linked_offering_id}"
                enrolled_lookup.setdefault(key_child, set()).add(linked_offering_id)
                cancelable_enrollment_lookup[linked_key] = row["can_cancel"]
                enrollment_offering_lookup[linked_key] = key_offering

        cur.execute(
            f"""
            SELECT
                ast.child_id,
                ats.offering_id,
                ast.is_present,
                ast.was_signed_up,
                ats.created_at,
                co.class_name,
                co.class_date,
                co.start_time,
                co.end_time,
                co.program_track
            FROM attendance_students ast
            JOIN attendance_sessions ats ON ats.id = ast.attendance_session_id
            JOIN class_offerings co ON co.id = ats.offering_id
            WHERE ast.child_id IN ({placeholders})
              AND ats.offering_id IS NOT NULL
            ORDER BY ats.created_at DESC
            """,
            tuple(child_ids),
        )
        attendance_rows = cur.fetchall()
        attendance_lookup = {}
        attendance_signature_lookup = {}
        for row in attendance_rows:
            key = (row["child_id"], row["offering_id"])
            signature_key = (row["child_id"], offering_signature(row))
            if row["is_present"] and not row.get("was_signed_up", 1):
                status = "Present (Didn't sign up)"
            else:
                status = "Present" if row["is_present"] else "Absent"
            if key not in attendance_lookup:
                attendance_lookup[key] = status
            if signature_key not in attendance_signature_lookup:
                attendance_signature_lookup[signature_key] = status

        for child_id, rows in signed_up_classes_by_child.items():
            for row in rows:
                status = attendance_lookup.get(
                    (child_id, row["offering_id"])
                ) or attendance_signature_lookup.get((child_id, offering_signature(row)))
                if status:
                    row["attendance_status"] = status
                    row["can_cancel"] = False
                    linked_classes = signup_classes_by_signature.get(
                        offering_signature(row), []
                    )
                    if not linked_classes:
                        linked_classes = [{"id": row["offering_id"]}]
                    for linked_cls in linked_classes:
                        cancelable_enrollment_lookup[
                            f"{child_id}:{int(linked_cls['id'])}"
                        ] = False

    for cls in signup_classes:
        class_id = int(cls["id"])
        class_week_key = int(cls.get("week_key") or 0)
        class_date = cls.get("class_date")
        for child in children:
            child_id = int(child["id"])
            key = f"{child_id}:{class_id}"
            child_enrolled = enrolled_lookup.get(child_id, set())
            if class_id in child_enrolled:
                signup_block_reasons[key] = "Already enrolled"
                continue
            if class_date and class_date < date.today():
                signup_block_reasons[key] = "Class already happened"
                continue
            week_count = weekly_counts.get((child_id, class_week_key), 0)
            if week_count >= MAX_CLASSES_PER_WEEK:
                signup_block_reasons[key] = f"Weekly limit ({MAX_CLASSES_PER_WEEK}) reached"
                continue
            eligible, reason = _is_child_eligible_for_offering(child, cls)
            if not eligible:
                signup_block_reasons[key] = reason

    return {
        "children": children,
        "signup_classes": signup_classes,
        "signed_up_classes_by_child": signed_up_classes_by_child,
        "enrolled_lookup": enrolled_lookup,
        "cancelable_enrollment_lookup": cancelable_enrollment_lookup,
        "enrollment_offering_lookup": enrollment_offering_lookup,
        "signup_block_reasons": signup_block_reasons,
    }


@app.route("/parent")
@login_required
@role_required("parent")
def parent_dashboard():
    # Parent dashboard: academy schedule, class signup, and instructor notes.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    payload = _build_parent_children_payload(cur, session["user_id"])

    calendar_start = date.today()
    calendar_end = calendar_start + timedelta(days=13)
    cur.execute(
        """
        SELECT
            co.class_date AS shift_date,
            DATE_FORMAT(co.class_date, '%W') AS shift_day_name,
            co.class_name,
            co.program_track,
            TIME_FORMAT(co.start_time, '%h:%i %p') AS start_label,
            TIME_FORMAT(co.end_time, '%h:%i %p') AS end_label
        FROM class_offerings co
        WHERE co.class_date BETWEEN %s AND %s
        ORDER BY co.class_date, co.start_time
        """,
        (calendar_start, calendar_end),
    )
    academy_schedule = cur.fetchall()
    cur.execute(
        """
        SELECT id, child_name
        FROM children
        WHERE parent_user_id = %s
        ORDER BY child_name
        """,
        (session["user_id"],),
    )
    children = cur.fetchall()
    child_ids = [int(child["id"]) for child in children]
    child_notes = _fetch_parent_notes_rows(cur, child_ids)
    notes_feed = []
    for child in children:
        for note in child_notes.get(child["id"], []):
            notes_feed.append(
                {
                    "child_name": child["child_name"],
                    "author_username": note.get("author_username"),
                    "author_role": note.get("author_role"),
                    "created_at": note.get("created_at"),
                    "note_text": note.get("note_text"),
                }
            )
    notes_feed.sort(key=lambda row: row.get("created_at") or datetime.min, reverse=True)
    cur.close()
    return render_template(
        "parent_dashboard.html",
        academy_schedule=academy_schedule,
        children=payload["children"],
        signup_classes=payload["signup_classes"],
        signup_block_reasons=payload["signup_block_reasons"],
        cancelable_enrollment_lookup=payload["cancelable_enrollment_lookup"],
        enrollment_offering_lookup=payload["enrollment_offering_lookup"],
        max_classes_per_week=MAX_CLASSES_PER_WEEK,
        belt_sequence=BELT_SEQUENCE,
        notes_feed=notes_feed,
    )


@app.route("/parent/children")
@login_required
@role_required("parent")
def parent_children_dashboard():
    # Child dashboard: each child's signed-up classes.
    db = get_db()
    cur = db.cursor(dictionary=True)
    _ensure_feature_schema(cur)
    payload = _build_parent_children_payload(cur, session["user_id"])
    cur.close()
    return render_template(
        "parent_children_dashboard.html",
        children=payload["children"],
        signed_up_classes_by_child=payload["signed_up_classes_by_child"],
    )


if __name__ == "__main__":
    app.run(debug=True)
