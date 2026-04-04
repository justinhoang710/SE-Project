CREATE DATABASE IF NOT EXISTS karate_academy;
USE karate_academy;

CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(80) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role ENUM('manager', 'employee', 'parent') NOT NULL
);

CREATE TABLE IF NOT EXISTS children (
  id INT AUTO_INCREMENT PRIMARY KEY,
  child_name VARCHAR(100) NOT NULL,
  parent_user_id INT NOT NULL,
  program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts',
  belt_index INT NOT NULL DEFAULT 0,
  guardian_name VARCHAR(120) NULL,
  contact_phone VARCHAR(40) NULL,
  FOREIGN KEY (parent_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS shifts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  employee_user_id INT NOT NULL,
  shift_date DATE NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  class_name VARCHAR(120) NOT NULL,
  FOREIGN KEY (employee_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS requests (
  id INT AUTO_INCREMENT PRIMARY KEY,
  request_type ENUM('switch', 'callout') NOT NULL,
  requester_user_id INT NOT NULL,
  shift_id INT NOT NULL,
  requested_employee_id INT NULL,
  reason TEXT NOT NULL,
  status ENUM('pending', 'approved', 'rejected') NOT NULL DEFAULT 'pending',
  switch_target_status ENUM('pending', 'accepted', 'rejected') NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (requester_user_id) REFERENCES users(id),
  FOREIGN KEY (shift_id) REFERENCES shifts(id),
  FOREIGN KEY (requested_employee_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS child_schedule (
  id INT AUTO_INCREMENT PRIMARY KEY,
  child_id INT NOT NULL,
  class_date DATE NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  class_title VARCHAR(120) NOT NULL,
  instructor_name VARCHAR(100) NOT NULL,
  FOREIGN KEY (child_id) REFERENCES children(id)
);

CREATE TABLE IF NOT EXISTS techniques (
  id INT AUTO_INCREMENT PRIMARY KEY,
  technique_name VARCHAR(120) NOT NULL UNIQUE,
  description TEXT NOT NULL,
  program_track ENUM('little_dragons', 'kids_martial_arts', 'teen_martial_arts', 'adult_martial_arts') NOT NULL DEFAULT 'kids_martial_arts',
  belt_name VARCHAR(40) NOT NULL DEFAULT 'White',
  created_by_user_id INT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  FOREIGN KEY (created_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS child_skill_progress (
  id INT AUTO_INCREMENT PRIMARY KEY,
  child_id INT NOT NULL,
  technique_id INT NOT NULL,
  assigned_by_user_id INT NOT NULL,
  assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  learned_count TINYINT NOT NULL DEFAULT 0,
  completed TINYINT(1) NOT NULL DEFAULT 0,
  completed_at TIMESTAMP NULL,
  notes TEXT NULL,
  FOREIGN KEY (child_id) REFERENCES children(id),
  FOREIGN KEY (technique_id) REFERENCES techniques(id),
  FOREIGN KEY (assigned_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS parent_notes (
  id INT AUTO_INCREMENT PRIMARY KEY,
  child_id INT NOT NULL,
  author_user_id INT NOT NULL,
  note_text TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (child_id) REFERENCES children(id),
  FOREIGN KEY (author_user_id) REFERENCES users(id)
);

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
);

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
);

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
);

CREATE TABLE IF NOT EXISTS attendance_students (
  id INT AUTO_INCREMENT PRIMARY KEY,
  attendance_session_id INT NOT NULL,
  child_id INT NOT NULL,
  is_present TINYINT(1) NOT NULL DEFAULT 1,
  UNIQUE KEY uq_attendance_student (attendance_session_id, child_id),
  FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id),
  FOREIGN KEY (child_id) REFERENCES children(id)
);

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
);

CREATE OR REPLACE VIEW kid_belt_students AS
SELECT id, child_name, belt_index
FROM children
WHERE program_track IN ('little_dragons', 'kids_martial_arts', 'teen_martial_arts');

CREATE OR REPLACE VIEW adult_belt_students AS
SELECT id, child_name, belt_index
FROM children
WHERE program_track = 'adult_martial_arts';
