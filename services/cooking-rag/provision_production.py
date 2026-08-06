from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql

from product_config import CONFIG

SERVICE_DIR = Path(__file__).resolve().parent
POSTGRES_ENV = CONFIG.database.credentials_file
DATABASE_NAME = CONFIG.database.name
CREDENTIAL_DIR = CONFIG.paths.config_dir / "roles"
ROLE_FILES = {
    "cooking_rag_ingest": CREDENTIAL_DIR / "cooking-ingest.env",
    "cooking_rag_writer": CREDENTIAL_DIR / "cooking-writer.env",
    "cooking_rag_runtime": CREDENTIAL_DIR / "cooking-runtime.env",
}


def admin_settings() -> dict[str, str]:
    raw = dotenv_values(POSTGRES_ENV)
    mapping = {
        "dbname": raw.get("POSTGRES_DB") or raw.get("RAG_DATABASE_NAME") or CONFIG.database.name,
        "user": raw.get("POSTGRES_USER") or raw.get("RAG_DATABASE_USER") or CONFIG.database.user,
        "password": raw.get("POSTGRES_PASSWORD") or raw.get("RAG_DATABASE_PASSWORD"),
    }
    if not all(mapping.values()):
        raise RuntimeError("admin settings are incomplete")
    return {name: str(value) for name, value in mapping.items()}


def admin_connection(*, database: str | None = None) -> psycopg.Connection:
    settings = admin_settings()
    return psycopg.connect(
        host=CONFIG.database.host,
        port=CONFIG.database.port,
        dbname=database or settings["dbname"],
        user=settings["user"],
        password=settings["password"],
        connect_timeout=10,
        autocommit=True,
    )


def write_env_file(path: Path, role: str, password: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = (
        "PGHOST=127.0.0.1\n"
        "PGPORT=5432\n"
        f"PGDATABASE={DATABASE_NAME}\n"
        f"PGUSER={role}\n"
        f"PGPASSWORD={password}\n"
    ).encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_absent() -> None:
    existing_files = [path for path in ROLE_FILES.values() if path.exists()]
    if existing_files:
        raise RuntimeError("credential file already exists")

    with admin_connection() as connection:
        database_exists = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = %s)",
            (DATABASE_NAME,),
        ).fetchone()[0]
        role_names = tuple(ROLE_FILES)
        existing_roles = connection.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(role_names),),
        ).fetchall()
    if not database_exists:
        raise RuntimeError("configured rag-favorite database does not exist")
    if existing_roles:
        raise RuntimeError("cooking compatibility role already exists")


def provision() -> None:
    ensure_absent()
    CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    passwords = {role: secrets.token_urlsafe(48) for role in ROLE_FILES}
    created_roles: list[str] = []
    try:
        with admin_connection() as connection:
            for role, password in passwords.items():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                created_roles.append(role)
        with admin_connection(database=DATABASE_NAME) as connection:
            for migration in (
                SERVICE_DIR / "migrations/0001-schema.sql",
                SERVICE_DIR / "migrations/0002-role-grants.sql",
            ):
                connection.execute(migration.read_text(encoding="utf-8"), prepare=False)

        os.chmod(CREDENTIAL_DIR, 0o700)
        for role, path in ROLE_FILES.items():
            write_env_file(path, role, passwords[role])
    except Exception:
        for path in ROLE_FILES.values():
            if path.exists():
                path.unlink()
        with admin_connection() as connection:
            for role in reversed(created_roles):
                connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )
        raise


def main() -> int:
    try:
        provision()
    except Exception as exc:
        print(f"PROVISION_FAILED={type(exc).__name__}")
        return 1
    print("COOKING_SCHEMA_INSTALLED=true")
    print("COOKING_ROLES_CREATED=3")
    print("COOKING_CREDENTIAL_FILES=3")
    print("CREDENTIAL_VALUES_PRINTED=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
