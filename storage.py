"""Простое потокобезопасное JSON-хранилище заявок.

Никаких внешних зависимостей и БД — файл легко перенести на Bothost
(при наличии постоянного тома укажите DATA_DIR=/app/data).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any


class ApplicationStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"next_id": 1, "applications": {}}
        self._load()

    # --- внутреннее ------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("applications"), dict):
                data.setdefault("next_id", 1)
                self._data = data
        except (OSError, ValueError):
            # Повреждённый файл не должен ронять бота: сохраняем бэкап.
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass

    def save(self) -> None:
        """Атомарная запись, чтобы перезапуск хостинга не обрезал файл."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # --- публичный API ---------------------------------------------------
    def create(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            app_id = int(self._data["next_id"])
            self._data["next_id"] = app_id + 1
            app: dict[str, Any] = {
                "id": app_id,
                "status": "pending",
                "decided_by": None,
                "decided_by_name": "",
                "decided_at": None,
                "reason": "",
                "admin_messages": {},
            }
            app.update(fields)
            self._data["applications"][str(app_id)] = app
            self.save()
            return dict(app)

    def get(self, app_id: int) -> dict[str, Any] | None:
        with self._lock:
            app = self._data["applications"].get(str(app_id))
            return dict(app) if app else None

    def update(self, app_id: int, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            app = self._data["applications"].get(str(app_id))
            if not app:
                return None
            app.update(fields)
            self.save()
            return dict(app)

    def record_admin_message(self, app_id: int, admin_id: int, message_id: int) -> None:
        with self._lock:
            app = self._data["applications"].get(str(app_id))
            if not app:
                return
            app.setdefault("admin_messages", {})[str(admin_id)] = message_id
            self.save()

    def by_user(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(app)
                for app in self._data["applications"].values()
                if app.get("user_id") == user_id
            ]

    def pending_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return [app for app in self.by_user(user_id) if app.get("status") == "pending"]

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            apps = [dict(a) for a in self._data["applications"].values()]
        apps.sort(key=lambda a: a.get("id", 0), reverse=True)
        return apps[:limit]
