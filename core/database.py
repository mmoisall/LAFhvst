import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from . import utils
from .models import Base

DB_PATH = os.path.join(utils.project_root(), "lafhvst.db")
DB_URL = "sqlite:///" + DB_PATH.replace(os.sep, "/")

engine = create_engine(DB_URL, connect_args={"check_same_thread": False}, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

_MIGRATIONS = {
    "folders": {
        "config": "TEXT DEFAULT '{}'",
        "cyc_option": "INTEGER DEFAULT 0",
        "cyc": "INTEGER DEFAULT 1",
        "log_level": "VARCHAR(20) DEFAULT 'INFO'",
        "tag_list": "TEXT DEFAULT ''",
        "memo": "TEXT",
        "profile_group_id": "INTEGER",
        "profile_id": "INTEGER",
        "order_index": "INTEGER",
    },
    "sources": {
        "site": "VARCHAR(200)",
        "key": "VARCHAR(200)",
        "config": "TEXT DEFAULT '{}'",
        "cyc_option": "INTEGER DEFAULT 0",
        "cyc": "INTEGER DEFAULT 1",
        "log_level": "VARCHAR(20) DEFAULT 'INFO'",
        "tag_list": "TEXT DEFAULT ''",
        "memo": "TEXT",
        "status": "VARCHAR(20) DEFAULT 'idle'",
        "recent_file_date": "DATETIME",
        "recent_file_mtime": "DATETIME",
        "recent_run_at": "DATETIME",
        "last_attempt_at": "DATETIME",
        "last_success_at": "DATETIME",
        "oldest_err_at": "DATETIME",
        "profile_group_id": "INTEGER",
        "profile_id": "INTEGER",
        "learned_upload_cycle": "INTEGER DEFAULT 60",
        "last_new_file_at": "DATETIME",
        "upload_time_history": "TEXT DEFAULT '[]'",
        "order_index": "INTEGER",
    },
    "gdl_error_logs": {
        "error_code": "VARCHAR(40)",
        "profile_id": "INTEGER",
        "folder_id": "INTEGER",
        "level": "VARCHAR(20) DEFAULT 'ERROR'",
        "category": "VARCHAR(40) DEFAULT ''",
        "scope": "VARCHAR(20) DEFAULT 'system'",
        "status": "VARCHAR(20) DEFAULT 'open'",
        "resolution": "TEXT",
        "resolved_at": "DATETIME",
        "fingerprint": "VARCHAR(64)",
        "repeat_count": "INTEGER DEFAULT 1",
        "context": "TEXT DEFAULT ''",
        "last_at": "DATETIME",
    },
    "profiles": {
        "name": "VARCHAR(255) DEFAULT ''",
        "site": "VARCHAR(200)",
        "context_dir": "VARCHAR(1024) DEFAULT ''",
        "proxy_url": "VARCHAR(1024)",
        "account_key": "VARCHAR(200)",
        "account_url": "VARCHAR(1024)",
        "status": "VARCHAR(20) DEFAULT '정상'",
        "is_site_default": "BOOLEAN DEFAULT 0",
        "cooldown_until": "DATETIME",
        "learned_capacity": "INTEGER DEFAULT 500",
        "learned_cooldown_minutes": "INTEGER DEFAULT 15",
        "recent_usage_count": "INTEGER DEFAULT 0",
        "consecutive_errors": "INTEGER DEFAULT 0",
        "last_error_at": "DATETIME",
        "last_stable_at": "DATETIME",
        "observations": "INTEGER DEFAULT 0",
        "learned_rate_per_hour": "FLOAT DEFAULT 0",
        "window_started_at": "DATETIME",
        "cooldown_probe_at": "DATETIME",
        "capacity_lo": "INTEGER",
        "capacity_hi": "INTEGER",
        "success_streak": "INTEGER DEFAULT 0",
        "observed_recovery_minutes": "INTEGER",
        "last_error_kind": "VARCHAR(40)",
        "order_index": "INTEGER",
    },
    "profile_groups": {
        "name": "VARCHAR(255) DEFAULT ''",
        "site": "VARCHAR(200)",
        "account_key": "VARCHAR(200)",
        "account_url": "VARCHAR(1024)",
        "cooldown_until": "DATETIME",
        "concurrent_limit": "INTEGER DEFAULT 1",
        "pace_seconds": "FLOAT DEFAULT 0",
        "shared_limit": "BOOLEAN DEFAULT 1",
        "order_index": "INTEGER",
    },
    "reactive_rules": {
        "name": "VARCHAR(255) DEFAULT ''",
        "site": "VARCHAR(200)",
        "keyword_pattern": "TEXT DEFAULT '[]'",
        "target_folder_id": "INTEGER",
        "is_active": "BOOLEAN DEFAULT 1",
    },
}


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def _migrate() -> None:
    with engine.begin() as connection:
        for table, columns in _MIGRATIONS.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(" + table + ")")
            }
            if not existing:
                continue
            for name, ddl in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        "ALTER TABLE " + table + " ADD COLUMN " + name + " " + ddl
                    )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate()


def get_session():
    return SessionLocal()
