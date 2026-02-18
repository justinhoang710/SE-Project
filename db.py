import os
from flask import g
import mysql.connector


def get_db():
    if "db" not in g:
        g.db = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "karate_academy"),
            autocommit=False,
        )
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None and db.is_connected():
        db.close()
