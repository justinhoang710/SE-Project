# Modesto's Karate Academies - Basic Management App

Minimal Flask + MySQL app with:
- Role-based login (`manager`, `employee`, `parent`)
- Self-registration for new `employee` and `parent` accounts
- Employee schedule view
- Employee shift-switch requests
- Employee call-out requests
- Manager approval/rejection of requests
- Parent view of child schedule

## 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Create database tables and seed data

```bash
mysql -u root -p < schema.sql
mysql -u root -p < seed.sql
```

If your MySQL user/database differ, set env vars before running app:

```bash
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=your_password
export MYSQL_DATABASE=karate_academy
export FLASK_SECRET_KEY=replace_this
```

## 3. Run app

```bash
python app.py
```

Open: `http://127.0.0.1:5000`

New users can register from the login page (`/register`).

## Demo logins

- Manager: `manager1` / `manager123`
- Employee: `employee1` / `employee123`
- Employee: `employee2` / `employee123`
- Parent: `parent1` / `parent123`

## Notes

- Password storage in this basic version uses `sha256$...` hash format for easy setup.
- For production, use stronger password hashing (`argon2` or `bcrypt`) and CSRF protection.
