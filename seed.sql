USE karate_academy;

-- Rename older demo usernames to the simplified format when possible.
UPDATE users u
LEFT JOIN users x ON x.username = 'manager1'
SET u.username = 'manager1'
WHERE u.username = 'test_manager1'
AND x.id IS NULL;

UPDATE users u
LEFT JOIN users x ON x.username = 'employee1'
SET u.username = 'employee1'
WHERE u.username = 'test_employee1'
AND x.id IS NULL;

UPDATE users u
LEFT JOIN users x ON x.username = 'employee2'
SET u.username = 'employee2'
WHERE u.username = 'test_employee2'
AND x.id IS NULL;

UPDATE users u
LEFT JOIN users x ON x.username = 'parent1'
SET u.username = 'parent1'
WHERE u.username = 'test_parent1'
AND x.id IS NULL;

-- Demo users
INSERT INTO users (username, password_hash, role)
VALUES
  ('manager1', 'sha256$866485796cfa8d7c0cf7111640205b83076433547577511d81f8030ae99ecea5', 'manager'),
  ('employee1', 'sha256$5b2f8e27e2e5b4081c03ce70b288c87bd1263140cbd1bd9ae078123509b7caff', 'employee'),
  ('employee2', 'sha256$5b2f8e27e2e5b4081c03ce70b288c87bd1263140cbd1bd9ae078123509b7caff', 'employee'),
  ('parent1', 'sha256$82e3edf5f5f3a46b5f94579b61817fd9a1f356adcef5ee22da3b96ef775c4860', 'parent')
ON DUPLICATE KEY UPDATE
  password_hash = VALUES(password_hash),
  role = VALUES(role);

-- Child for parent account
INSERT INTO children (child_name, parent_user_id)
SELECT 'child1', u.id
FROM users u
WHERE u.username = 'parent1'
AND NOT EXISTS (
  SELECT 1 FROM children c WHERE c.child_name = 'child1' AND c.parent_user_id = u.id
);

-- Employee shifts
INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
SELECT u.id, '2026-02-18', '16:00:00', '18:00:00', 'Class 1'
FROM users u WHERE u.username = 'employee1'
AND NOT EXISTS (
  SELECT 1 FROM shifts s WHERE s.employee_user_id = u.id AND s.shift_date = '2026-02-18' AND s.start_time = '16:00:00'
);

INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
SELECT u.id, '2026-02-19', '18:00:00', '20:00:00', 'Class 2'
FROM users u WHERE u.username = 'employee1'
AND NOT EXISTS (
  SELECT 1 FROM shifts s WHERE s.employee_user_id = u.id AND s.shift_date = '2026-02-19' AND s.start_time = '18:00:00'
);

INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
SELECT u.id, '2026-02-18', '18:00:00', '20:00:00', 'Class 3'
FROM users u WHERE u.username = 'employee2'
AND NOT EXISTS (
  SELECT 1 FROM shifts s WHERE s.employee_user_id = u.id AND s.shift_date = '2026-02-18' AND s.start_time = '18:00:00'
);

-- Child class schedule
INSERT INTO child_schedule (child_id, class_date, start_time, end_time, class_title, instructor_name)
SELECT c.id, '2026-02-18', '16:00:00', '17:00:00', 'Class 1', 'employee1'
FROM children c
WHERE c.child_name = 'child1'
AND NOT EXISTS (
  SELECT 1 FROM child_schedule cs WHERE cs.child_id = c.id AND cs.class_date = '2026-02-18' AND cs.start_time = '16:00:00'
);

INSERT INTO child_schedule (child_id, class_date, start_time, end_time, class_title, instructor_name)
SELECT c.id, '2026-02-20', '16:00:00', '17:00:00', 'Class 2', 'employee2'
FROM children c
WHERE c.child_name = 'child1'
AND NOT EXISTS (
  SELECT 1 FROM child_schedule cs WHERE cs.child_id = c.id AND cs.class_date = '2026-02-20' AND cs.start_time = '16:00:00'
);

-- Normalize existing schedule labels so rerunning seed updates older data.
UPDATE shifts s
JOIN users u ON u.id = s.employee_user_id
SET s.class_name = CASE
  WHEN u.username = 'employee1' AND s.shift_date = '2026-02-18' AND s.start_time = '16:00:00' THEN 'Class 1'
  WHEN u.username = 'employee1' AND s.shift_date = '2026-02-19' AND s.start_time = '18:00:00' THEN 'Class 2'
  WHEN u.username = 'employee2' AND s.shift_date = '2026-02-18' AND s.start_time = '18:00:00' THEN 'Class 3'
  ELSE s.class_name
END
WHERE u.username IN ('employee1', 'employee2');

UPDATE child_schedule cs
JOIN children c ON c.id = cs.child_id
SET
  cs.class_title = CASE
    WHEN cs.class_date = '2026-02-18' AND cs.start_time = '16:00:00' THEN 'Class 1'
    WHEN cs.class_date = '2026-02-20' AND cs.start_time = '16:00:00' THEN 'Class 2'
    ELSE cs.class_title
  END,
  cs.instructor_name = CASE
    WHEN cs.class_date = '2026-02-18' AND cs.start_time = '16:00:00' THEN 'employee1'
    WHEN cs.class_date = '2026-02-20' AND cs.start_time = '16:00:00' THEN 'employee2'
    ELSE cs.instructor_name
  END
WHERE c.child_name = 'child1';

-- Technique list
INSERT INTO techniques (technique_name, description, created_by_user_id)
SELECT 'Technique 1', 'Test technique 1 description.', u.id
FROM users u
WHERE u.username = 'manager1'
AND NOT EXISTS (SELECT 1 FROM techniques t WHERE t.technique_name = 'Technique 1');

INSERT INTO techniques (technique_name, description, created_by_user_id)
SELECT 'Technique 2', 'Test technique 2 description.', u.id
FROM users u
WHERE u.username = 'employee1'
AND NOT EXISTS (SELECT 1 FROM techniques t WHERE t.technique_name = 'Technique 2');

INSERT INTO techniques (technique_name, description, created_by_user_id)
SELECT 'Technique 3', 'Test technique 3 description.', u.id
FROM users u
WHERE u.username = 'manager1'
AND NOT EXISTS (SELECT 1 FROM techniques t WHERE t.technique_name = 'Technique 3');

-- Child progress tracking
INSERT INTO child_skill_progress (child_id, technique_id, assigned_by_user_id, completed, completed_at, notes)
SELECT c.id, t.id, u.id, 1, CURRENT_TIMESTAMP, 'Test note 1.'
FROM children c
JOIN techniques t ON t.technique_name = 'Technique 1'
JOIN users u ON u.username = 'employee1'
WHERE c.child_name = 'child1'
AND NOT EXISTS (
  SELECT 1
  FROM child_skill_progress p
  WHERE p.child_id = c.id AND p.technique_id = t.id
);

INSERT INTO child_skill_progress (child_id, technique_id, assigned_by_user_id, completed, notes)
SELECT c.id, t.id, u.id, 0, 'Test note 2.'
FROM children c
JOIN techniques t ON t.technique_name = 'Technique 2'
JOIN users u ON u.username = 'manager1'
WHERE c.child_name = 'child1'
AND NOT EXISTS (
  SELECT 1
  FROM child_skill_progress p
  WHERE p.child_id = c.id AND p.technique_id = t.id
);

-- Notes to parent
CREATE TABLE IF NOT EXISTS parent_notes (
  id INT AUTO_INCREMENT PRIMARY KEY,
  child_id INT NOT NULL,
  author_user_id INT NOT NULL,
  note_text TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (child_id) REFERENCES children(id),
  FOREIGN KEY (author_user_id) REFERENCES users(id)
);

INSERT INTO parent_notes (child_id, author_user_id, note_text)
SELECT c.id, u.id, 'Great focus this week. Keep practicing front kick at home.'
FROM children c
JOIN users u ON u.username = 'employee1'
WHERE c.child_name = 'child1'
AND NOT EXISTS (
  SELECT 1
  FROM parent_notes pn
  WHERE pn.child_id = c.id AND pn.note_text = 'Great focus this week. Keep practicing front kick at home.'
);
