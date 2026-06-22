from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class RuntimeStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._scenes: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[state] warning: could not load {self.path}: {exc}")
            return

        scenes = raw.get("scenes", {}) if isinstance(raw, dict) else {}
        if not isinstance(scenes, dict):
            return
        for scene, values in scenes.items():
            if isinstance(scene, str) and isinstance(values, dict):
                self._scenes[scene] = {
                    key: value
                    for key, value in values.items()
                    if isinstance(key, str) and isinstance(value, str)
                }

    def get_scene(self, scene: str, defaults: dict[str, str] | None = None) -> dict[str, str]:
        with self._lock:
            result = dict(defaults or {})
            result.update(self._scenes.get(scene, {}))
            return result

    def set_value(self, scene: str, key: str, value: str) -> dict[str, str]:
        with self._lock:
            self._scenes.setdefault(scene, {})[key] = value
            self._save_locked()
            return dict(self._scenes[scene])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"scenes": {scene: dict(values) for scene, values in self._scenes.items()}}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"scenes": self._scenes}
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
