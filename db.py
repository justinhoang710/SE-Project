import os
from pathlib import Path

from flask import g
import mysql.connector


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            # Make local project .env authoritative for this dev app.
            os.environ[key] = value


def get_db():
    if "db" not in g:
        _load_env_file()

        config = {
            "host": os.getenv("MYSQL_HOST", "localhost"),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root"),
            "database": os.getenv("MYSQL_DATABASE", "karate_academy"),
            "autocommit": False,
        }

        password = os.getenv("MYSQL_PASSWORD", "")
        if password:
            config["password"] = password

        try:
            g.db = mysql.connector.connect(**config)
        except mysql.connector.Error as exc:
            if getattr(exc, "errno", None) == 1045:
                raise RuntimeError(
                    "MySQL login failed (1045). Set MYSQL_USER and MYSQL_PASSWORD in a .env file "
                    "or export them in your shell before running python app.py."
                ) from exc
            raise
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None and db.is_connected():
        db.close()
