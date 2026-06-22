from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class OBSSceneState:
    lockout_ms: int = 500
    current_scene: str | None = None
    last_scene_change_ms: int = 0
    obs_connected: bool = False
    obs_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_connected(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self.obs_connected = connected
            self.obs_error = error

    def set_scene(self, scene_name: str | None, mark_change: bool = True) -> None:
        with self._lock:
            if scene_name and scene_name != self.current_scene:
                self.current_scene = scene_name
                if mark_change:
                    self.last_scene_change_ms = now_ms()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current_scene": self.current_scene,
                "last_scene_change_ms": self.last_scene_change_ms,
                "obs_connected": self.obs_connected,
                "obs_error": self.obs_error,
                "lockout_ms": self.lockout_ms,
            }

    def in_lockout(self) -> bool:
        with self._lock:
            if self.lockout_ms <= 0 or self.last_scene_change_ms <= 0:
                return False
            return now_ms() - self.last_scene_change_ms < self.lockout_ms


class OBSWebSocketSceneTracker:
    def __init__(
        self,
        state: OBSSceneState,
        host: str,
        port: int,
        password: str = "",
        reconnect_delay_sec: float = 5.0,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.password = password
        self.reconnect_delay_sec = reconnect_delay_sec
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="obs-scene-tracker", daemon=True)
        self._thread.start()

    def request_once(
        self,
        request_type: str,
        request_data: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Run one authenticated OBS request on a short-lived connection."""
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is not installed") from exc

        ws = None
        try:
            ws = websocket.create_connection(
                f"ws://{self.host}:{self.port}", timeout=timeout
            )
            self._identify(ws)
            return self._request(ws, request_type, request_data)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"OBS WebSocket request failed: {exc}") from exc
        finally:
            if ws is not None:
                ws.close()

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            self.state.set_connected(False, "websocket-client is not installed")
            print("[obs] websocket-client is not installed; OBS scene tracking disabled")
            return

        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(f"ws://{self.host}:{self.port}", timeout=5)
                self._identify(ws)
                self.state.set_connected(True, None)
                print(f"[obs] connected to ws://{self.host}:{self.port}")
                self._request_current_scene(ws)
                ws.settimeout(None)
                self._read_loop(ws)
            except Exception as exc:
                message = str(exc)
                self.state.set_connected(False, message)
                print(f"[obs] warning: scene tracking unavailable: {message}")
                self._stop.wait(self.reconnect_delay_sec)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _recv(self, ws) -> dict[str, Any]:
        raw = ws.recv()
        if not raw:
            raise RuntimeError(
                "OBS WebSocket closed the connection before sending JSON; "
                "check OBS_WS_PASSWORD and WebSocket Server Settings"
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            sample = raw[:120] if isinstance(raw, str) else repr(raw[:120])
            raise RuntimeError(f"OBS WebSocket sent a non-JSON message: {sample!r}") from exc

    def _send(self, ws, op: int, data: dict[str, Any]) -> None:
        ws.send(json.dumps({"op": op, "d": data}, separators=(",", ":")))

    def _identify(self, ws) -> None:
        hello = self._recv(ws)
        if hello.get("op") != 0:
            raise RuntimeError("OBS WebSocket did not send Hello")

        hello_data = hello.get("d", {})
        identify: dict[str, Any] = {"rpcVersion": min(int(hello_data.get("rpcVersion", 1)), 1)}

        auth = hello_data.get("authentication")
        if auth:
            if not self.password:
                raise RuntimeError("OBS WebSocket requires OBS_WS_PASSWORD")
            identify["authentication"] = self._auth_response(
                self.password,
                auth["salt"],
                auth["challenge"],
            )

        self._send(ws, 1, identify)
        identified = self._recv(ws)
        if identified.get("op") != 2:
            raise RuntimeError("OBS WebSocket identification failed")

    def _auth_response(self, password: str, salt: str, challenge: str) -> str:
        secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode()
        return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode()

    def _request(self, ws, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        self._send(
            ws,
            6,
            {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": request_data or {},
            },
        )

        while True:
            message = self._recv(ws)
            op = message.get("op")
            data = message.get("d", {})
            if op == 5:
                self._handle_event(data)
                continue
            if op == 7 and data.get("requestId") == request_id:
                status = data.get("requestStatus", {})
                if not status.get("result", False):
                    raise RuntimeError(status.get("comment") or f"OBS request failed: {request_type}")
                return data.get("responseData", {})

    def _request_current_scene(self, ws) -> None:
        data = self._request(ws, "GetCurrentProgramScene")
        scene_name = data.get("currentProgramSceneName")
        if scene_name:
            mark_change = self.state.snapshot()["current_scene"] is not None
            self.state.set_scene(scene_name, mark_change=mark_change)
            print(f"[obs] current scene: {scene_name}")

    def _read_loop(self, ws) -> None:
        while not self._stop.is_set():
            message = self._recv(ws)
            if message.get("op") == 5:
                self._handle_event(message.get("d", {}))

    def _handle_event(self, data: dict[str, Any]) -> None:
        if data.get("eventType") != "CurrentProgramSceneChanged":
            return

        scene_name = data.get("eventData", {}).get("sceneName")
        if scene_name:
            self.state.set_scene(scene_name)
            print(f"[obs] current scene: {scene_name}")


def obs_scene_state_from_env() -> tuple[OBSSceneState, OBSWebSocketSceneTracker | None]:
    lockout_raw = os.getenv("OBS_SCENE_LOCKOUT_MS", "500")
    try:
        lockout_ms = int(lockout_raw)
    except ValueError:
        lockout_ms = 500

    state = OBSSceneState(lockout_ms=max(0, lockout_ms))

    has_obs_config = any(
        os.getenv(name) is not None
        for name in ("OBS_WS_HOST", "OBS_WS_PORT", "OBS_WS_PASSWORD")
    )
    if not has_obs_config:
        return state, None

    host = os.getenv("OBS_WS_HOST") or "localhost"
    port_raw = os.getenv("OBS_WS_PORT") or "4455"
    try:
        port = int(port_raw)
    except ValueError:
        state.set_connected(False, f"Invalid OBS_WS_PORT: {port_raw}")
        return state, None

    tracker = OBSWebSocketSceneTracker(
        state=state,
        host=host,
        port=port,
        password=os.getenv("OBS_WS_PASSWORD") or "",
    )
    return state, tracker
