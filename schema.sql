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
