from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from .obs_scene import OBSSceneState, OBSWebSocketSceneTracker, obs_scene_state_from_env
    from .persistent_state import RuntimeStateStore
except ImportError:
    from obs_scene import OBSSceneState, OBSWebSocketSceneTracker, obs_scene_state_from_env
    from persistent_state import RuntimeStateStore


PROJECT_DIR = Path(__file__).resolve().parent
SUPPORTED_EFFECT_TYPES = {"video", "image", "text", "html", "sprite", "lottie"}

dotenv_path = find_dotenv(usecwd=True)
if dotenv_path:
    load_dotenv(dotenv_path)


def _env_path(name: str, default: Path, fallback_name: str | None = None) -> Path:
    value = os.getenv(name) or (os.getenv(fallback_name) if fallback_name else None)
    if not value:
        return default

    path = Path(value)
    if path.is_absolute():
        return path

    for base in (Path.cwd(), PROJECT_DIR, PROJECT_DIR.parent):
        candidate = base / path
        if candidate.exists():
            return candidate
    return PROJECT_DIR / path


CONFIG_PATH = _env_path("OBS_OVERLAY_CONFIG", PROJECT_DIR / "config" / "overlays.json", "OVERLAY_CONFIG")
STATIC_DIR = PROJECT_DIR / "static"
ASSETS_DIR = PROJECT_DIR / "assets"
RUNTIME_STATE_PATH = PROJECT_DIR / "runtime" / "state.json"


class TriggerRequest(BaseModel):
    effect: str
    payload: dict[str, Any] = Field(default_factory=dict)


class OverlayEventRequest(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StateUpdateRequest(BaseModel):
    value: str
    command: str | None = None
    key: str | None = None
    scene: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedEffect:
    requested_effect: str
    resolved_effect: str
    config: dict[str, Any]
    scene: str | None
    scope: str
    scene_bound: bool


class EffectRegistry:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.effects: dict[str, dict[str, Any]] = {}
        self.global_effects: dict[str, dict[str, Any]] = {}
        self.scenes: dict[str, dict[str, dict[str, Any]]] = {}
        self.event_effects: dict[str, str] = {}
        self.state_commands: dict[str, str] = {}
        self.scene_state: dict[str, dict[str, dict[str, Any]]] = {}
        self.scene_components: dict[str, dict[str, dict[str, Any]]] = {}
        self.loaded_at = 0.0
        self._config_mtime = 0.0
        self.reload()

    def reload(self) -> None:
        try:
            config_mtime = self.config_path.stat().st_mtime
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Config file not found: {self.config_path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Config file is not valid JSON: {exc}") from exc

        effects = raw.get("effects", {})
        if not isinstance(effects, dict):
            raise RuntimeError("Config 'effects' must be an object")

        global_effects = raw.get("global_effects", {})
        if not isinstance(global_effects, dict):
            raise RuntimeError("Config 'global_effects' must be an object")

        scenes_raw = raw.get("scenes", {})
        if not isinstance(scenes_raw, dict):
            raise RuntimeError("Config 'scenes' must be an object")

        event_effects = raw.get("event_effects", {})
        if not isinstance(event_effects, dict):
            raise RuntimeError("Config 'event_effects' must be an object")
        for event_type, effect_name in event_effects.items():
            if not isinstance(event_type, str) or not event_type:
                raise RuntimeError("Config 'event_effects' keys must be non-empty strings")
            if not isinstance(effect_name, str) or not effect_name:
                raise RuntimeError(f"Config event_effects.{event_type} must be a non-empty effect name")

        state_commands_raw = raw.get("state_commands", {})
        if not isinstance(state_commands_raw, dict):
            raise RuntimeError("Config 'state_commands' must be an object")
        state_commands: dict[str, str] = {}
        for command, command_config in state_commands_raw.items():
            if not isinstance(command, str) or not command:
                raise RuntimeError("Config 'state_commands' keys must be non-empty strings")
            if isinstance(command_config, str):
                state_key = command_config
            elif isinstance(command_config, dict):
                state_key = command_config.get("state_key")
            else:
                state_key = None
            if not isinstance(state_key, str) or not state_key:
                raise RuntimeError(f"Config state_commands.{command} must define state_key")
            state_commands[command.lower()] = state_key

        self._validate_effect_map("effects", effects)
        self._validate_effect_map("global_effects", global_effects)

        scenes: dict[str, dict[str, dict[str, Any]]] = {}
        scene_states: dict[str, dict[str, dict[str, Any]]] = {}
        scene_components: dict[str, dict[str, dict[str, Any]]] = {}
        for scene_name, scene_config in scenes_raw.items():
            if not isinstance(scene_name, str) or not scene_name:
                raise RuntimeError("Scene names must be non-empty strings")
            if not isinstance(scene_config, dict):
                raise RuntimeError(f"Scene '{scene_name}' must be an object")
            scene_effects = scene_config.get("effects", {})
            if not isinstance(scene_effects, dict):
                raise RuntimeError(f"Scene '{scene_name}' must contain an 'effects' object")
            self._validate_effect_map(f"scenes.{scene_name}.effects", scene_effects, allow_alias=True)
            scenes[scene_name] = scene_effects

            state_config = scene_config.get("state", {})
            if not isinstance(state_config, dict):
                raise RuntimeError(f"Scene '{scene_name}' state must be an object")
            validated_state: dict[str, dict[str, Any]] = {}
            for state_key, definition in state_config.items():
                if not isinstance(state_key, str) or not state_key or not isinstance(definition, dict):
                    raise RuntimeError(f"Scene '{scene_name}' has an invalid state definition")
                default = definition.get("default")
                allowed = definition.get("allowed")
                if not isinstance(default, str) or not isinstance(allowed, list):
                    raise RuntimeError(f"Scene '{scene_name}' state '{state_key}' needs default and allowed")
                if not all(isinstance(value, str) and value for value in allowed) or default not in allowed:
                    raise RuntimeError(f"Scene '{scene_name}' state '{state_key}' has invalid allowed values")
                validated_state[state_key] = {"default": default, "allowed": list(allowed)}
            scene_states[scene_name] = validated_state

            components = scene_config.get("components", {})
            if not isinstance(components, dict):
                raise RuntimeError(f"Scene '{scene_name}' components must be an object")
            validated_components: dict[str, dict[str, Any]] = {}
            for component_name, component in components.items():
                if not isinstance(component_name, str) or not component_name or not isinstance(component, dict):
                    raise RuntimeError(f"Scene '{scene_name}' has an invalid component")
                if component.get("type") != "media_input":
                    raise RuntimeError(f"Scene '{scene_name}' component '{component_name}' has unsupported type")
                obs_input = component.get("obs_input")
                state_key = component.get("state_key")
                variants = component.get("variants")
                if not isinstance(obs_input, str) or not obs_input:
                    raise RuntimeError(f"Scene '{scene_name}' component '{component_name}' needs obs_input")
                if state_key not in validated_state:
                    raise RuntimeError(f"Scene '{scene_name}' component '{component_name}' has unknown state_key")
                if not isinstance(variants, dict) or not variants:
                    raise RuntimeError(f"Scene '{scene_name}' component '{component_name}' needs variants")
                if not all(isinstance(key, str) and isinstance(value, str) and value for key, value in variants.items()):
                    raise RuntimeError(f"Scene '{scene_name}' component '{component_name}' has invalid variants")
                validated_components[component_name] = dict(component)
            scene_components[scene_name] = validated_components

        self.effects = effects
        self.global_effects = global_effects
        self.scenes = scenes
        self.event_effects = event_effects
        self.state_commands = state_commands
        self.scene_state = scene_states
        self.scene_components = scene_components
        self.loaded_at = time.time()
        self._config_mtime = config_mtime

    def _validate_effect_map(
        self,
        label: str,
        effects: dict[str, Any],
        allow_alias: bool = False,
    ) -> None:
        for name, config in effects.items():
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"{label} effect names must be non-empty strings")
            if not isinstance(config, dict):
                raise RuntimeError(f"{label}.{name} must be an object")
            if "alias" in config:
                if not allow_alias:
                    raise RuntimeError(f"{label}.{name} aliases are only supported inside scenes")
                if not isinstance(config["alias"], str) or not config["alias"]:
                    raise RuntimeError(f"{label}.{name}.alias must be a non-empty string")
                continue

            effect_type = config.get("type")
            if effect_type not in SUPPORTED_EFFECT_TYPES:
                raise RuntimeError(f"{label}.{name} has unsupported or missing type")
            if effect_type in {"video", "image"} and not config.get("src"):
                raise RuntimeError(f"{label}.{name} must define 'src'")

    def reload_if_changed(self) -> None:
        try:
            config_mtime = self.config_path.stat().st_mtime
        except FileNotFoundError as exc:
            raise RuntimeError(f"Config file not found: {self.config_path}") from exc

        if config_mtime != self._config_mtime:
            self.reload()

    def has_effect(self, name: str) -> bool:
        return self.resolve(name, None) is not None

    def scene_effect_names(self, scene: str | None) -> list[str]:
        if not scene:
            return []
        return sorted(self.scenes.get(scene, {}).keys())

    def global_effect_names(self) -> list[str]:
        return sorted(self.global_effects.keys())

    def all_effect_names(self) -> list[str]:
        names = set(self.effects) | set(self.global_effects)
        for scene_effects in self.scenes.values():
            names.update(scene_effects)
        return sorted(names)

    def event_names(self) -> list[str]:
        return sorted(self.event_effects.keys())

    def effect_for_event(self, event_type: str) -> str:
        return self.event_effects.get(event_type, event_type)

    def state_key_for_command(self, command: str) -> str | None:
        return self.state_commands.get(command.lower())

    def scene_state_defaults(self, scene: str) -> dict[str, str]:
        return {
            key: definition["default"]
            for key, definition in self.scene_state.get(scene, {}).items()
        }

    def allowed_state_values(self, scene: str, key: str) -> list[str]:
        definition = self.scene_state.get(scene, {}).get(key, {})
        return list(definition.get("allowed", []))

    def components_for_state(self, scene: str, key: str) -> dict[str, dict[str, Any]]:
        return {
            name: component
            for name, component in self.scene_components.get(scene, {}).items()
            if component.get("state_key") == key
        }

    def effect_defined_in_any_scene(self, name: str) -> bool:
        return any(name in scene_effects for scene_effects in self.scenes.values())

    def resolve(self, name: str, scene: str | None) -> ResolvedEffect | None:
        if scene and name in self.scenes.get(scene, {}):
            resolved = self._resolve_scene_effect(scene, name)
            if resolved:
                resolved_name, config = resolved
                return ResolvedEffect(name, resolved_name, config, scene, "scene", True)

        if name in self.global_effects:
            return ResolvedEffect(name, name, self.global_effects[name], scene, "global", False)

        if name in self.effects:
            return ResolvedEffect(name, name, self.effects[name], scene, "legacy", False)

        return None

    def _resolve_scene_effect(
        self,
        scene: str,
        name: str,
        seen: set[str] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        seen = seen or set()
        if name in seen:
            raise RuntimeError(f"Alias loop while resolving '{name}' in scene '{scene}'")
        seen.add(name)

        scene_effects = self.scenes.get(scene, {})
        config = scene_effects.get(name)
        if not config:
            return None

        alias = config.get("alias")
        if alias:
            if alias not in scene_effects:
                raise RuntimeError(f"Alias '{name}' points to missing effect '{alias}' in scene '{scene}'")
            return self._resolve_scene_effect(scene, alias, seen)
        return name, config

    def response(self) -> dict[str, Any]:
        return {
            "effects": self.effects,
            "global_effects": self.global_effects,
            "scenes": {scene: {"effects": effects} for scene, effects in self.scenes.items()},
            "event_effects": self.event_effects,
            "state_commands": self.state_commands,
            "scene_state": self.scene_state,
            "scene_components": self.scene_components,
            "loaded_at": self.loaded_at,
            "config": str(self.config_path),
        }


class EventBus:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def client_count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        async with self._lock:
            self._clients.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._clients.discard(queue)

    async def broadcast(self, event: str, data: dict[str, Any]) -> int:
        message = {"event": event, "data": data}
        async with self._lock:
            clients = list(self._clients)

        delivered = 0
        for queue in clients:
            try:
                queue.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:
                # Drop the oldest message so one stalled browser source cannot
                # permanently block newer overlay triggers.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                    delivered += 1
                except asyncio.QueueEmpty:
                    pass
        return delivered


def encode_sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


registry = EffectRegistry(CONFIG_PATH)
bus = EventBus()
obs_state, obs_tracker = obs_scene_state_from_env()
runtime_state = RuntimeStateStore(RUNTIME_STATE_PATH)

app = FastAPI(title="OBS Overlay Bus")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.on_event("startup")
async def start_obs_scene_tracker() -> None:
    if obs_tracker is None:
        return
    obs_tracker.start()


@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path in {"/overlay", "/effects"} or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "overlay": "/overlay",
            "effects": "/effects",
            "trigger": "/trigger",
            "event": "/event",
            "state": "/state",
        }
    )


@app.get("/overlay")
async def overlay() -> FileResponse:
    return FileResponse(STATIC_DIR / "overlay.html")


@app.get("/effects")
async def effects() -> JSONResponse:
    try:
        registry.reload_if_changed()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(registry.response())


@app.get("/status")
async def status() -> JSONResponse:
    try:
        registry.reload_if_changed()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    scene_state = obs_state.snapshot()
    current_scene = scene_state["current_scene"]
    current_values = (
        runtime_state.get_scene(current_scene, registry.scene_state_defaults(current_scene))
        if current_scene
        else {}
    )
    return JSONResponse(
        {
            "ok": True,
            "current_scene": current_scene,
            "obs_tracking_configured": obs_tracker is not None,
            "obs_connected": scene_state["obs_connected"],
            "obs_error": scene_state["obs_error"],
            "last_scene_change_ms": scene_state["last_scene_change_ms"],
            "scene_lockout_ms": scene_state["lockout_ms"],
            "configured_scenes": sorted(registry.scenes.keys()),
            "available_scene_effects": registry.scene_effect_names(current_scene),
            "available_global_effects": registry.global_effect_names(),
            "available_events": registry.event_names(),
            "event_effects": registry.event_effects,
            "state_commands": registry.state_commands,
            "current_scene_state": current_values,
            "current_scene_components": sorted(registry.scene_components.get(current_scene or "", {}).keys()),
            "overlay_clients": await bus.client_count(),
            "effects": registry.all_effect_names(),
            "config": str(registry.config_path),
            "loaded_at": registry.loaded_at,
        }
    )


def resolve_media_file(src: str) -> Path:
    if src.startswith("/assets/"):
        path = ASSETS_DIR / src[len("/assets/") :]
    else:
        path = Path(src)
        if not path.is_absolute():
            path = PROJECT_DIR / path
    return path.resolve()


async def apply_media_component(
    scene: str,
    component_name: str,
    component: dict[str, Any],
    value: str,
) -> dict[str, Any]:
    variants = component["variants"]
    src = variants.get(value)
    if not src:
        raise RuntimeError(
            f"Component '{component_name}' in scene '{scene}' has no variant for '{value}'"
        )

    media_file = resolve_media_file(src)
    if not media_file.exists():
        raise FileNotFoundError(f"Media variant does not exist: {media_file}")
    if obs_tracker is None:
        raise RuntimeError("OBS WebSocket is not configured")
    if not obs_state.snapshot()["obs_connected"]:
        raise RuntimeError("OBS WebSocket is not connected")

    input_name = component["obs_input"]
    input_settings: dict[str, Any] = {"local_file": str(media_file)}
    if component.get("loop", True):
        input_settings["looping"] = True

    await asyncio.to_thread(
        obs_tracker.request_once,
        "SetInputSettings",
        {
            "inputName": input_name,
            "inputSettings": input_settings,
            "overlay": True,
        },
    )
    if component.get("restart", True):
        await asyncio.to_thread(
            obs_tracker.request_once,
            "TriggerMediaInputAction",
            {
                "inputName": input_name,
                "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
            },
        )

    return {
        "component": component_name,
        "obs_input": input_name,
        "value": value,
        "src": src,
        "file": str(media_file),
    }


@app.get("/state")
async def get_state(scene: str | None = None) -> JSONResponse:
    try:
        registry.reload_if_changed()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    selected_scene = scene or obs_state.snapshot()["current_scene"]
    if not selected_scene:
        return JSONResponse({"ok": False, "error": "current_scene_unknown"}, status_code=409)
    if selected_scene not in registry.scene_state:
        return JSONResponse(
            {"ok": False, "error": "scene_has_no_state", "scene": selected_scene},
            status_code=404,
        )

    return JSONResponse(
        {
            "ok": True,
            "scene": selected_scene,
            "state": runtime_state.get_scene(
                selected_scene,
                registry.scene_state_defaults(selected_scene),
            ),
            "schema": registry.scene_state[selected_scene],
            "components": registry.scene_components.get(selected_scene, {}),
        }
    )


@app.post("/state")
async def set_state(request: StateUpdateRequest) -> JSONResponse:
    try:
        registry.reload_if_changed()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    scene = request.scene or obs_state.snapshot()["current_scene"]
    if not scene:
        return JSONResponse({"ok": False, "error": "current_scene_unknown"}, status_code=409)

    state_key = request.key
    if request.command:
        state_key = registry.state_key_for_command(request.command.strip().lower())
        if not state_key:
            return JSONResponse(
                {"ok": False, "error": "unknown_state_command", "command": request.command},
                status_code=404,
            )
    if not state_key:
        return JSONResponse({"ok": False, "error": "missing_state_key"}, status_code=400)

    allowed = registry.allowed_state_values(scene, state_key)
    requested_value = request.value.strip()
    value = next((item for item in allowed if item.lower() == requested_value.lower()), None)
    if value is None:
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_state_value",
                "scene": scene,
                "key": state_key,
                "value": requested_value,
                "allowed": allowed,
            },
            status_code=400,
        )

    applied: list[dict[str, Any]] = []
    try:
        for component_name, component in registry.components_for_state(scene, state_key).items():
            applied.append(await apply_media_component(scene, component_name, component, value))
    except FileNotFoundError as exc:
        return JSONResponse(
            {"ok": False, "error": "media_file_not_found", "detail": str(exc)},
            status_code=404,
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"ok": False, "error": "obs_media_update_failed", "detail": str(exc)},
            status_code=503,
        )

    values = runtime_state.set_value(scene, state_key, value)
    await bus.broadcast(
        "state_changed",
        {
            "scene": scene,
            "key": state_key,
            "value": value,
            "state": values,
            "components": applied,
            "payload": request.payload,
            "sent_at": time.time(),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "scene": scene,
            "key": state_key,
            "value": value,
            "state": values,
            "components": applied,
        }
    )


async def dispatch_effect(
    effect_name: str,
    payload: dict[str, Any],
    event_type: str | None = None,
) -> JSONResponse:
    try:
        registry.reload_if_changed()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    scene_state = obs_state.snapshot()
    current_scene = scene_state["current_scene"]

    try:
        resolved = registry.resolve(effect_name, current_scene)
    except RuntimeError as exc:
        data = {
            "ok": False,
            "error": "effect_resolution_failed",
            "effect": effect_name,
            "scene": current_scene,
            "detail": str(exc),
        }
        if event_type:
            data["event_type"] = event_type
        return JSONResponse(
            data,
            status_code=400,
        )

    if not resolved:
        if registry.effect_defined_in_any_scene(effect_name):
            data = {
                "ok": False,
                "error": "effect_not_valid_for_scene",
                "effect": effect_name,
                "scene": current_scene,
            }
            if event_type:
                data["event_type"] = event_type
            return JSONResponse(
                data
            )
        data = {"ok": False, "error": "unknown_effect", "effect": effect_name}
        if event_type:
            data["event_type"] = event_type
        return JSONResponse(
            data,
            status_code=404,
        )

    if resolved.scene_bound and obs_state.in_lockout():
        data = {
            "ok": False,
            "error": "scene_transition_lockout",
            "effect": effect_name,
            "resolved_effect": resolved.resolved_effect,
            "scene": current_scene,
        }
        if event_type:
            data["event_type"] = event_type
        return JSONResponse(data)

    # Compatibility nudge for already-open OBS browser pages that may still
    # hold an older in-memory copy of overlays.json.
    await bus.broadcast(
        "config_reload",
        {
            "loaded_at": registry.loaded_at,
            "effects": registry.all_effect_names(),
        },
    )
    await asyncio.sleep(0.15)

    delivered = await bus.broadcast(
        "trigger",
        {
            "effect": effect_name,
            "resolved_effect": resolved.resolved_effect,
            "scene": current_scene,
            "scope": resolved.scope,
            "config": resolved.config,
            "payload": payload,
            "event_type": event_type,
            "registry_loaded_at": registry.loaded_at,
            "sent_at": time.time(),
        },
    )
    response = {
        "ok": True,
        "effect": effect_name,
        "resolved_effect": resolved.resolved_effect,
        "scene": current_scene,
        "scope": resolved.scope,
        "src": resolved.config.get("src"),
        "clients": delivered,
    }
    if event_type:
        response["event_type"] = event_type
    if delivered == 0:
        response["warning"] = "no_overlay_clients"
    return JSONResponse(response)


@app.post("/trigger")
async def trigger(request: TriggerRequest) -> JSONResponse:
    return await dispatch_effect(request.effect, request.payload)


@app.post("/event")
async def trigger_event(request: OverlayEventRequest) -> JSONResponse:
    event_type = request.type.strip()
    if not event_type:
        return JSONResponse({"ok": False, "error": "missing_event_type"}, status_code=400)
    effect_name = registry.effect_for_event(event_type)
    return await dispatch_effect(effect_name, request.payload, event_type=event_type)


@app.post("/reload")
async def reload_config() -> JSONResponse:
    try:
        registry.reload()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    delivered = await bus.broadcast(
        "config_reload",
        {
            "loaded_at": registry.loaded_at,
            "effects": registry.all_effect_names(),
        },
    )
    return JSONResponse({"ok": True, "effects": registry.all_effect_names(), "clients": delivered})


@app.get("/events")
async def events() -> StreamingResponse:
    async def stream():
        queue = await bus.subscribe()
        try:
            yield encode_sse("ready", {"ok": True, "effects": registry.all_effect_names()})
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    yield encode_sse(message["event"], message["data"])
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("OBS_OVERLAY_HOST") or os.getenv("HOST") or "127.0.0.1"
    port = int(os.getenv("OBS_OVERLAY_PORT") or os.getenv("PORT") or "8765")
    uvicorn.run("server:app", host=host, port=port, reload=False)
