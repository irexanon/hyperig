"""Tiny async key-value store with two backends.

Used for three things: the Instagram session, the per-thread read cursors,
and the Instagram-thread -> Telegram-topic mapping.

* MongoStore  - used when MONGO_URI is set (survives redeploys / ephemeral disks)
* FileStore   - default, JSON files under DATA_DIR (mount it as a volume in Docker)
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from utils.logger import get_logger

log = get_logger("store")


class Store:
    async def get(self, key: str, default: Any = None) -> Any:
        raise NotImplementedError

    async def set(self, key: str, value: Any) -> None:
        raise NotImplementedError


class FileStore(Store):
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def _read(self, key: str, default: Any) -> Any:
        try:
            return json.loads(self._path(key).read_text("utf-8"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            log.error("Corrupt %s, ignoring it", self._path(key))
            return default

    def _write(self, key: str, value: Any) -> None:
        path = self._path(key)
        if value is None:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2, default=str), "utf-8")
        os.chmod(tmp, 0o600)  # session data is as sensitive as a password
        os.replace(tmp, path)  # atomic: never leaves a half-written session

    async def get(self, key, default=None):
        return await asyncio.to_thread(self._read, key, default)

    async def set(self, key, value):
        await asyncio.to_thread(self._write, key, value)


class MongoStore(Store):
    def __init__(self, uri: str, db_name: str, collection_name: str = "kv"):
        from pymongo import MongoClient

        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        self.db = self.client[db_name]
        self.col = self.db[collection_name]
        self.client.admin.command("ping")  # fail fast on a bad URI
        log.info("Connected to MongoDB (%s/%s)", db_name, collection_name)

    def _read(self, key, default):
        doc = self.col.find_one({"_id": key})
        return json.loads(doc["json"]) if doc and doc.get("json") is not None else default

    def _write(self, key, value):
        if value is None:
            self.col.delete_one({"_id": key})
            return
        # Stored as a JSON string: session settings contain arbitrary keys that Mongo field names may reject.
        self.col.replace_one({"_id": key}, {"_id": key, "json": json.dumps(value, default=str)}, upsert=True)

    async def get(self, key, default=None):
        return await asyncio.to_thread(self._read, key, default)

    async def set(self, key, value):
        await asyncio.to_thread(self._write, key, value)


def sanitize_username(raw: str) -> str:
    import re
    clean = re.sub(r"[^a-z0-9._-]", "", (raw or "").strip().lower().lstrip("@"))
    return clean[:64]


def create_store(cfg) -> Store:
    if cfg.mongo_uri:
        try:
            return MongoStore(cfg.mongo_uri, cfg.mongo_db)
        except Exception as e:
            log.warning("Could not connect to MongoDB (%s), falling back to JSON files in %s", e, cfg.data_dir)
    log.info("MONGO_URI not set, using JSON files in %s", cfg.data_dir)
    return FileStore(cfg.data_dir)


def create_user_store(base_cfg, username: str) -> Store:
    uname = sanitize_username(username)
    if not uname:
        raise ValueError("Invalid username")
    if base_cfg.mongo_uri:
        try:
            store = MongoStore(base_cfg.mongo_uri, base_cfg.mongo_db, collection_name=f"user_{uname}")
            store.db["users_index"].replace_one({"_id": uname}, {"_id": uname}, upsert=True)
            return store
        except Exception as e:
            log.warning("Could not connect to MongoDB for user %s (%s), using local files", uname, e)
    user_dir = Path(base_cfg.data_dir) / "users" / uname
    return FileStore(user_dir)


def list_registered_users(base_cfg) -> list[str]:
    users: set[str] = set()
    if base_cfg.mongo_uri:
        try:
            from pymongo import MongoClient
            client = MongoClient(base_cfg.mongo_uri, serverSelectionTimeoutMS=5000)
            db = client[base_cfg.mongo_db]
            for doc in db["users_index"].find({}):
                if doc.get("_id"):
                    users.add(str(doc["_id"]))
        except Exception:
            pass
    users_dir = Path(base_cfg.data_dir) / "users"
    if users_dir.exists() and users_dir.is_dir():
        for child in users_dir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                users.add(child.name)
    return sorted(users)
