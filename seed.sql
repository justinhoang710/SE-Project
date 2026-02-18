USE karate_academy;

-- Demo users
INSERT INTO users (username, password_hash, role)
VALUES
  ('manager1', 'sha256$866485796cfa8d7c0cf7111640205b83076433547577511d81f8030ae99ecea5', 'manager'),
  ('employee1', 'sha256$5b2f8e27e2e5b4081c03ce70b288c87bd1263140cbd1bd9ae078123509b7caff', 'employee'),
  ('employee2', 'sha256$5b2f8e27e2e5b4081c03ce70b288c87bd1263140cbd1bd9ae078123509b7caff', 'employee'),
  ('parent1', 'sha256$82e3edf5f5f3a46b5f94579b61817fd9a1f356adcef5ee22da3b96ef775c4860', 'parent')
ON DUPLICATE KEY UPDATE username = VALUES(username);

-- Child for parent account
INSERT INTO children (child_name, parent_user_id)
SELECT 'Jamie Modesto', u.id
FROM users u
WHERE u.username = 'parent1'
AND NOT EXISTS (
  SELECT 1 FROM children c WHERE c.child_name = 'Jamie Modesto' AND c.parent_user_id = u.id
);

-- Employee shifts
INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
SELECT u.id, '2026-02-18', '16:00:00', '18:00:00', 'Beginner Kids'
FROM users u WHERE u.username = 'employee1'
AND NOT EXISTS (
  SELECT 1 FROM shifts s WHERE s.employee_user_id = u.id AND s.shift_date = '2026-02-18' AND s.start_time = '16:00:00'
);

INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
SELECT u.id, '2026-02-19', '18:00:00', '20:00:00', 'Intermediate Teens'
FROM users u WHERE u.username = 'employee1'
AND NOT EXISTS (
  SELECT 1 FROM shifts s WHERE s.employee_user_id = u.id AND s.shift_date = '2026-02-19' AND s.start_time = '18:00:00'
);

INSERT INTO shifts (employee_user_id, shift_date, start_time, end_time, class_name)
SELECT u.id, '2026-02-18', '18:00:00', '20:00:00', 'Adult Fundamentals'
FROM users u WHERE u.username = 'employee2'
AND NOT EXISTS (
  SELECT 1 FROM shifts s WHERE s.employee_user_id = u.id AND s.shift_date = '2026-02-18' AND s.start_time = '18:00:00'
);

-- Child class schedule
INSERT INTO child_schedule (child_id, class_date, start_time, end_time, class_title, instructor_name)
SELECT c.id, '2026-02-18', '16:00:00', '17:00:00', 'White Belt Basics', 'Sensei Mark'
FROM children c
WHERE c.child_name = 'Jamie Modesto'
AND NOT EXISTS (
  SELECT 1 FROM child_schedule cs WHERE cs.child_id = c.id AND cs.class_date = '2026-02-18' AND cs.start_time = '16:00:00'
);

INSERT INTO child_schedule (child_id, class_date, start_time, end_time, class_title, instructor_name)
SELECT c.id, '2026-02-20', '16:00:00', '17:00:00', 'Kata Practice', 'Sensei Gavin'
FROM children c
WHERE c.child_name = 'Jamie Modesto'
AND NOT EXISTS (
  SELECT 1 FROM child_schedule cs WHERE cs.child_id = c.id AND cs.class_date = '2026-02-20' AND cs.start_time = '16:00:00'
);
