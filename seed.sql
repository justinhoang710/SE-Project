USE karate_academy;

-- Cleanup seed
-- Clears classes, staff schedules, employees, parents, and kids while keeping
-- manager accounts and curriculum techniques.

START TRANSACTION;

-- Drop records that depend on classes, schedules, children, or staff/parent users.
DELETE FROM attendance_technique_logs;
DELETE FROM attendance_students;
DELETE FROM attendance_sessions;
DELETE FROM class_enrollments;
DELETE FROM staff_class_signups;
DELETE FROM parent_notes;
DELETE FROM child_skill_progress;
DELETE FROM child_schedule;
DELETE FROM schedule_history;
DELETE FROM requests;
DELETE FROM staff_weekly_connections;
DELETE FROM outgoing_emails
WHERE to_user_id IN (
  SELECT id FROM users WHERE role IN ('employee', 'parent')
);

-- Clear staff-created technique ownership so employee accounts can be removed.
UPDATE techniques
SET created_by_user_id = NULL
WHERE created_by_user_id IN (
  SELECT id FROM users WHERE role IN ('employee', 'parent')
);

-- Clear classes and staff schedules.
DELETE FROM class_offerings;
DELETE FROM shifts;

-- Clear kids and their parent/employee accounts.
DELETE FROM children;
DELETE FROM users
WHERE role IN ('employee', 'parent');

COMMIT;
