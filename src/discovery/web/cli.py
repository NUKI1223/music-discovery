"""Local serving and explicit account maintenance; passwords never appear in argv."""

import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from discovery.catalog.io import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.contracts import Catalog
from discovery.storage import Store

from .app import create_app, default_data_dir
from .auth import Accounts
from .schemas import Profile


def import_legacy(data_dir: Path, username: str, source: Path, *, catalog: Catalog | None = None) -> Path:
    """Import once into an unused account; preserve both databases with SQLite backups."""
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("Исходная база не найдена.")
    accounts = Accounts(Path(data_dir) / "accounts.sqlite3")
    user = accounts.by_name(username)
    if user is None:
        raise ValueError("Сначала зарегистрируйте выбранный аккаунт.")
    if catalog is None:
        catalog_path = Path(os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
        catalog = load_catalog(catalog_path if catalog_path.is_absolute() else PROJECT_ROOT / catalog_path)
    target = Store(Path(data_dir) / "users" / user["id"] / "discovery.sqlite3")
    if source == target.path.resolve():
        raise ValueError("Источник и назначение должны различаться.")
    # Read through a consistent SQLite backup, including any committed WAL content.
    backup_dir = Path(data_dir) / "backups" / uuid4().hex
    backup_dir.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(backup_dir / "legacy.sqlite3") as dest:
            src.backup(dest)
    legacy = Store(backup_dir / "legacy.sqlite3")
    exported = legacy.export()  # Validates catalog, decision and feedback models.
    if any(item["is_fixture"] != catalog.is_fixture for item in exported["catalogs"]):
        raise ValueError("Демонстрационную и реальную историю нельзя смешивать.")
    target.backup(backup_dir / "account-before.sqlite3")
    with target.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM settings WHERE key='legacy_import'").fetchone():
            raise ValueError("Этот аккаунт уже импортировал историю.")
        if conn.execute("SELECT 1 FROM decisions LIMIT 1").fetchone():
            raise ValueError("Импорт возможен только в аккаунт без рекомендаций.")
        for table, fields in (("catalogs", "hash,snapshot_id,payload"),
                              ("decisions", "id,catalog_hash,payload"),
                              ("impressions", "decision_id,release_id,shown_at"),
                              ("feedback", "seq,id,decision_id,payload"),
                              ("revoked_feedback", "id,revoked_at"),
                              ("hidden_collection", "release_id,through_seq,exclude_from_recs")):
            with legacy.connection() as src:
                rows = src.execute(f"SELECT {fields} FROM {table}").fetchall()
            placeholders = ",".join("?" for _ in fields.split(","))
            conn.executemany(f"INSERT INTO {table} ({fields}) VALUES ({placeholders})", rows)
        base = exported["base_profile"]
        web = Profile(onboarded=True, seed_artist_ids=base["seed_artist_ids_by_genre"].get("hip-hop", []),
                      avoid_tag_ids=base["avoid_tags_by_genre"].get("hip-hop", []))
        for key, payload in (("profile", json.dumps(base)), ("web_profile", web.model_dump_json()),
                             ("legacy_import", json.dumps({"source": str(source), "backup": str(backup_dir)}))):
            conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, payload))
    return backup_dir


def main():
    load_project_env()
    parser = argparse.ArgumentParser(description="Локальный Music Discovery")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    reset = commands.add_parser("reset-password")
    reset.add_argument("username")
    legacy = commands.add_parser("import-legacy")
    legacy.add_argument("username")
    legacy.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            import uvicorn
            uvicorn.run(create_app(data_dir=args.data_dir), host=args.host, port=args.port,
                        proxy_headers=False)
        elif args.command == "reset-password":
            accounts = Accounts(args.data_dir / "accounts.sqlite3")
            user = accounts.by_name(args.username)
            if user is None:
                raise ValueError("Аккаунт не найден.")
            password = getpass.getpass("Новый пароль: ")
            if password != getpass.getpass("Повторите пароль: "):
                raise ValueError("Пароли не совпадают.")
            accounts.reset_password(user["id"], password)
            print("Пароль изменён; все сессии аккаунта отозваны.")
        else:
            backup = import_legacy(args.data_dir, args.username, args.source)
            print(f"История импортирована. Резервные копии: {backup}")
    except (ValueError, OSError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
