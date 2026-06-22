from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests
from dotenv import find_dotenv, load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "overlays.json"

READ_BUFFER_SIZE = 4096


dotenv_path = find_dotenv(usecwd=True)
if dotenv_path:
    load_dotenv(dotenv_path)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def resolve_path(value: str | None, default: Path) -> Path:
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


TWITCH_OAUTH = (
    os.getenv("TWITCH_OAUTH")
    or os.getenv("TWITCH_TOKEN")
    or os.getenv("AUTH")
    or ""
).strip()
TWITCH_STREAMER_ENV = (os.getenv("TWITCH_STREAMER") or os.getenv("STREAMER") or "").strip().lstrip("#")
TWITCH_CHANNEL = (os.getenv("TWITCH_CHANNEL") or TWITCH_STREAMER_ENV).strip().lstrip("#")
TWITCH_STREAMER = (TWITCH_STREAMER_ENV or TWITCH_CHANNEL).strip().lstrip("#")
TWITCH_NICK = (os.getenv("TWITCH_NICK") or TWITCH_STREAMER).strip().lstrip("#")
TWITCH_SERVER = os.getenv("TWITCH_IRC_HOST", "irc.chat.twitch.tv").strip()
TWITCH_USE_TLS = env_bool("TWITCH_IRC_TLS", True)
TWITCH_PORT = env_int("TWITCH_IRC_PORT", 6697 if TWITCH_USE_TLS else 6667)

OVERLAY_URL = os.getenv("OBS_OVERLAY_URL", "http://127.0.0.1:8765").rstrip("/")
CONFIG_PATH = resolve_path(
    os.getenv("OBS_OVERLAY_CONFIG") or os.getenv("OVERLAY_CONFIG"),
    DEFAULT_CONFIG_PATH,
)

REQUIRE_ASSET_EXISTS = env_bool("TWITCH_REQUIRE_ASSET_EXISTS", True)
CATALOG_REFRESH_SEC = env_float("TWITCH_CATALOG_REFRESH_SEC", 5.0)
GLOBAL_COOLDOWN_SEC = env_float("TWITCH_READER_COOLDOWN_SEC", 0.35)


def normalize_username(name: str) -> str:
    return name.strip().lstrip("@#").lower()


def normalize_trigger_text(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def source_asset_path(src: str) -> Path | None:
    parsed = urlsplit(src)
    path = unquote(parsed.path)
    marker = "/assets/"
    if not path.startswith(marker):
        return None
    relative = path[len(marker) :]
    return ASSETS_DIR / relative


def trigger_overlay(effect: str, payload: dict[str, Any] | None = None) -> bool:
    """Send a trigger request to the local OBS overlay bus."""
    try:
        response = requests.post(
            f"{OVERLAY_URL}/trigger",
            json={"effect": effect, "payload": payload or {}},
            timeout=1.0,
        )
    except requests.RequestException as exc:
        print(f"[overlay] trigger failed: {exc}")
        return False

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.ok and data.get("ok", False):
        clients = data.get("clients", "?")
        src = data.get("src", "?")
        if clients == 0:
            print(f"[overlay] accepted {effect} src={src} but no overlay browser clients are connected")
        else:
            print(f"[overlay] triggered {effect} src={src} clients={clients}")
        return True

    error = data.get("error", response.text)
    scene = data.get("scene")
    suffix = f" scene={scene}" if scene is not None else ""
    print(f"[overlay] trigger rejected for {effect}: {error}{suffix}")
    return False


def trigger_overlay_event(event_type: str, payload: dict[str, Any] | None = None) -> bool:
    """Send a higher-level event request to the local OBS overlay bus."""
    try:
        response = requests.post(
            f"{OVERLAY_URL}/event",
            json={"type": event_type, "payload": payload or {}},
            timeout=1.0,
        )
    except requests.RequestException as exc:
        print(f"[event] trigger failed: {exc}")
        return False

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.ok and data.get("ok", False):
        clients = data.get("clients", "?")
        effect = data.get("effect", "?")
        src = data.get("src", "?")
        if clients == 0:
            print(f"[event] accepted {event_type}->{effect} src={src} but no overlay browser clients are connected")
        else:
            print(f"[event] triggered {event_type}->{effect} src={src} clients={clients}")
        return True

    error = data.get("error", response.text)
    scene = data.get("scene")
    suffix = f" scene={scene}" if scene is not None else ""
    print(f"[event] trigger rejected for {event_type}: {error}{suffix}")
    return False


def trigger_overlay_state(
    command: str,
    value: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Apply a persistent scene-state command through the overlay bus."""
    try:
        response = requests.post(
            f"{OVERLAY_URL}/state",
            json={"command": command, "value": value, "payload": payload or {}},
            timeout=6.0,
        )
    except requests.RequestException as exc:
        print(f"[state] update failed: {exc}")
        return False

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.ok and data.get("ok", False):
        print(f"[state] {data.get('scene')}.{data.get('key')} = {data.get('value')}")
        return True

    error = data.get("error", response.text)
    detail = data.get("detail")
    allowed = data.get("allowed")
    suffix = f" detail={detail}" if detail else ""
    if allowed:
        suffix += f" allowed={','.join(allowed)}"
    print(f"[state] rejected {command} {value}: {error}{suffix}")
    return False


class EffectCatalog:
    def __init__(self, config_path: Path, require_asset_exists: bool = True) -> None:
        self.config_path = config_path
        self.require_asset_exists = require_asset_exists
        self.effects: dict[str, dict[str, Any]] = {}
        self.cheer_exact: dict[int, str] = {}
        self.cheer_minimums: list[tuple[int, str]] = []
        self.state_commands: set[str] = set()
        self._last_loaded = 0.0
        self._last_effect_names: tuple[str, ...] = ()
        self._last_skipped_names: tuple[str, ...] = ()
        self._last_cheer_rules: tuple[str, ...] = ()
        self._last_state_commands: tuple[str, ...] = ()

    def maybe_reload(self) -> None:
        if time.time() - self._last_loaded >= CATALOG_REFRESH_SEC:
            self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[catalog] failed to read {self.config_path}: {exc}")
            self.effects = {}
            self._last_loaded = time.time()
            return

        available: dict[str, dict[str, Any]] = {}
        skipped: list[str] = []

        for effect_map, scene_effects in self._iter_effect_maps(raw):
            for name, config in effect_map.items():
                if not isinstance(name, str) or not isinstance(config, dict):
                    continue

                if self._asset_available(config, scene_effects):
                    available[name] = config
                else:
                    skipped.append(name)

        self.effects = available
        self._load_cheer_effects(raw)
        self._load_state_commands(raw)
        self._last_loaded = time.time()

        effect_names = tuple(sorted(self.effects))
        skipped_names = tuple(sorted(skipped))
        cheer_rules = self._cheer_rule_labels()
        state_commands = tuple(sorted(self.state_commands))
        if (
            effect_names != self._last_effect_names
            or skipped_names != self._last_skipped_names
            or cheer_rules != self._last_cheer_rules
            or state_commands != self._last_state_commands
        ):
            names = ", ".join(effect_names) or "none"
            print(f"[catalog] triggerable effects: {names}")
            if skipped_names:
                print(f"[catalog] skipped missing assets: {', '.join(skipped_names)}")
            if cheer_rules:
                print(f"[catalog] cheer mappings: {', '.join(cheer_rules)}")
            if state_commands:
                print(f"[catalog] state commands: {', '.join('!' + command for command in state_commands)}")
            self._last_effect_names = effect_names
            self._last_skipped_names = skipped_names
            self._last_cheer_rules = cheer_rules
            self._last_state_commands = state_commands

    def _iter_effect_maps(self, raw: dict[str, Any]):
        effects = raw.get("effects", {})
        if isinstance(effects, dict):
            yield effects, None

        global_effects = raw.get("global_effects", {})
        if isinstance(global_effects, dict):
            yield global_effects, None

        scenes = raw.get("scenes", {})
        if not isinstance(scenes, dict):
            return

        for scene_config in scenes.values():
            if not isinstance(scene_config, dict):
                continue
            scene_effects = scene_config.get("effects", {})
            if isinstance(scene_effects, dict):
                yield scene_effects, scene_effects

    def _load_cheer_effects(self, raw: dict[str, Any]) -> None:
        self.cheer_exact = {}
        self.cheer_minimums = []

        cheer_effects = raw.get("cheer_effects", {})
        if isinstance(cheer_effects, dict):
            for bits_text, effect in cheer_effects.items():
                self._add_cheer_rule(str(bits_text), effect)
        elif isinstance(cheer_effects, list):
            for rule in cheer_effects:
                if not isinstance(rule, dict):
                    continue
                effect = rule.get("effect")
                if "bits" in rule:
                    self._add_cheer_rule(str(rule["bits"]), effect)
                elif "min_bits" in rule:
                    self._add_cheer_rule(f"{rule['min_bits']}+", effect)

    def _load_state_commands(self, raw: dict[str, Any]) -> None:
        commands = raw.get("state_commands", {})
        self.state_commands = {
            command.lower()
            for command in commands
            if isinstance(command, str) and command
        } if isinstance(commands, dict) else set()

    def _add_cheer_rule(self, bits_text: str, effect: Any) -> None:
        if not isinstance(effect, str) or effect not in self.effects:
            return

        key = bits_text.strip().lower()
        is_minimum = False
        if key.startswith(">="):
            key = key[2:].strip()
            is_minimum = True
        elif key.endswith("+"):
            key = key[:-1].strip()
            is_minimum = True

        try:
            bits = int(key)
        except ValueError:
            return
        if bits <= 0:
            return

        if is_minimum:
            self.cheer_minimums.append((bits, effect))
            self.cheer_minimums.sort(key=lambda item: item[0], reverse=True)
        else:
            self.cheer_exact[bits] = effect

    def _cheer_rule_labels(self) -> tuple[str, ...]:
        labels = [f"{bits}->{effect}" for bits, effect in sorted(self.cheer_exact.items())]
        labels.extend(f"{bits}+->{effect}" for bits, effect in sorted(self.cheer_minimums))
        return tuple(labels)

    def _asset_available(
        self,
        config: dict[str, Any],
        scene_effects: dict[str, dict[str, Any]] | None,
        seen: set[str] | None = None,
    ) -> bool:
        if not self.require_asset_exists:
            return True

        alias = config.get("alias")
        if alias:
            if not scene_effects or not isinstance(alias, str):
                return False
            seen = seen or set()
            if alias in seen:
                return False
            seen.add(alias)
            target = scene_effects.get(alias)
            if not isinstance(target, dict):
                return False
            return self._asset_available(target, scene_effects, seen)

        src = str(config.get("src", ""))
        asset_path = source_asset_path(src)
        return asset_path is not None and asset_path.exists()

    def match_message(self, message: str) -> list[str]:
        text = normalize_trigger_text(message)
        if not text:
            return []

        matches: list[str] = []
        for effect_name in self.effects:
            keyword = normalize_trigger_text(effect_name)
            if not keyword:
                continue

            if re.search(rf"(?:^|\s){re.escape(keyword)}(?:\s|$)", text):
                matches.append(effect_name)

        return matches

    def match_cheer(self, bits: int) -> str | None:
        exact = self.cheer_exact.get(bits)
        if exact:
            return exact

        for minimum_bits, effect in self.cheer_minimums:
            if bits >= minimum_bits:
                return effect

        fallback = f"cheer{bits}"
        if fallback in self.effects:
            return fallback
        return None

    def match_state_command(self, message: str) -> tuple[str, str] | None:
        match = re.match(r"^!([a-z0-9_]+)\s+(.+?)\s*$", message.strip(), re.IGNORECASE)
        if not match:
            return None
        command = match.group(1).lower()
        if command not in self.state_commands:
            return None
        return command, match.group(2).strip()


def unescape_irc_tag_value(value: str) -> str:
    return (
        value.replace(r"\s", " ")
        .replace(r"\:", ";")
        .replace(r"\r", "\r")
        .replace(r"\n", "\n")
        .replace(r"\\", "\\")
    )


def parse_irc_tags(raw_tags: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for raw_tag in raw_tags.split(";"):
        if not raw_tag:
            continue
        if "=" in raw_tag:
            key, value = raw_tag.split("=", 1)
        else:
            key, value = raw_tag, ""
        tags[key] = unescape_irc_tag_value(value)
    return tags


def split_irc_tags(line: str) -> tuple[dict[str, str], str]:
    if not line.startswith("@"):
        return {}, line
    try:
        raw_tags, rest = line.split(" ", 1)
    except ValueError:
        return {}, line
    return parse_irc_tags(raw_tags[1:]), rest


def parse_bits(tags: dict[str, str]) -> int | None:
    bits_text = tags.get("bits")
    if not bits_text:
        return None
    try:
        bits = int(bits_text)
    except ValueError:
        return None
    return bits if bits > 0 else None


def parse_irc_privmsg(line: str) -> tuple[str | None, str | None, dict[str, str]]:
    tags, rest = split_irc_tags(line)

    match = re.search(r":([^!]+)![^ ]+ PRIVMSG #[^ ]+ :(.+)$", rest)
    if not match:
        return None, None, tags
    return match.group(1), match.group(2).strip(), tags


def parse_irc_usernotice(line: str) -> dict[str, str] | None:
    tags, rest = split_irc_tags(line)
    if " USERNOTICE " not in rest:
        return None
    return tags


def parse_positive_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def connect_to_twitch() -> socket.socket:
    if not TWITCH_OAUTH:
        raise RuntimeError("Missing Twitch token. Set AUTH, TWITCH_OAUTH, or TWITCH_TOKEN in .env.")
    if not TWITCH_CHANNEL:
        raise RuntimeError("Missing channel. Set TWITCH_CHANNEL in .env.")
    if not TWITCH_NICK:
        raise RuntimeError("Missing bot login. Set TWITCH_NICK in .env.")
    if not TWITCH_STREAMER:
        raise RuntimeError("Missing streamer login. Set TWITCH_STREAMER or STREAMER in .env.")

    token = TWITCH_OAUTH
    if not token.startswith("oauth:"):
        token = f"oauth:{token}"

    raw_sock = socket.create_connection((TWITCH_SERVER, TWITCH_PORT), timeout=10)
    if TWITCH_USE_TLS:
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw_sock, server_hostname=TWITCH_SERVER)
    else:
        sock = raw_sock
    sock.settimeout(None)

    sock.sendall(f"PASS {token}\r\n".encode("utf-8"))
    sock.sendall(f"NICK {TWITCH_NICK}\r\n".encode("utf-8"))
    sock.sendall("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n".encode("utf-8"))
    sock.sendall(f"JOIN #{TWITCH_CHANNEL}\r\n".encode("utf-8"))
    protocol = "TLS" if TWITCH_USE_TLS else "plain IRC"
    print(f"[twitch] connected via {protocol} to {TWITCH_SERVER}:{TWITCH_PORT} as {TWITCH_NICK} to #{TWITCH_CHANNEL}")
    print("[twitch] requested IRC tags for cheer/bits detection")
    print(f"[twitch] only @{TWITCH_STREAMER} can trigger overlays")
    return sock


def run_chat_reader() -> None:
    catalog = EffectCatalog(CONFIG_PATH, require_asset_exists=REQUIRE_ASSET_EXISTS)
    catalog.reload()

    streamer = normalize_username(TWITCH_STREAMER)
    last_triggered = 0.0

    while True:
        sock: socket.socket | None = None
        buffer = ""
        try:
            sock = connect_to_twitch()

            while True:
                catalog.maybe_reload()
                data = sock.recv(READ_BUFFER_SIZE).decode("utf-8", errors="ignore")
                if not data:
                    raise ConnectionError("Twitch IRC connection closed")

                buffer += data
                lines = buffer.split("\r\n")
                buffer = lines.pop()

                for line in lines:
                    if line.startswith("PING"):
                        pong = line.replace("PING", "PONG", 1)
                        sock.sendall(f"{pong}\r\n".encode("utf-8"))
                        print("[twitch] keepalive pong")
                        continue

                    if line.endswith(" RECONNECT") or " RECONNECT " in line:
                        raise ConnectionError("Twitch requested IRC reconnect")

                    if " NOTICE " in line:
                        notice = line.split(" :", 1)[-1]
                        print(f"[twitch] notice: {notice}")
                        if "authentication failed" in notice.lower() or "improperly formatted auth" in notice.lower():
                            raise RuntimeError(notice)
                        continue

                    usernotice_tags = parse_irc_usernotice(line)
                    if usernotice_tags:
                        if usernotice_tags.get("msg-id") == "raid":
                            raider = (
                                usernotice_tags.get("msg-param-login")
                                or usernotice_tags.get("msg-param-displayName")
                                or usernotice_tags.get("login")
                                or "unknown"
                            )
                            viewers = parse_positive_int(usernotice_tags.get("msg-param-viewerCount"))
                            print(f"[raid] {raider} raided with {viewers or '?'} viewers")
                            trigger_overlay_event(
                                "raid",
                                {
                                    "username": raider,
                                    "viewer_count": viewers,
                                    "source": "twitch_usernotice",
                                },
                            )
                        continue
                    
                    user, message, tags = parse_irc_privmsg(line)
                    
                    if not user or message is None:
                        continue
                    # we only want the ban hammer to be restricted to streamer 
                    # if user != streamer and effect == 'hammer_bot':
                        # continue
                    
                    # if normalize_username(user) user!= streamer:
                        # continue
                    
                    first_msg = tags.get("first-msg") == "1"
                    if first_msg:
                        print(f"[first-chat] {user}: {message}")
                        if trigger_overlay_event(
                            "first_time_chatter",
                            {
                                "username": user,
                                "message": message,
                                "source": "twitch_irc_tags",
                            },
                        ):
                            last_triggered = time.time()

                    bits = parse_bits(tags)
                    if bits is not None:
                        effect = catalog.match_cheer(bits)
                        if not effect:
                            print(f"[cheer] {user} cheered {bits} bits but no cheer effect is configured")
                            continue

                        print(f"[cheer] {user}: {bits} bits -> {effect}")
                        now = time.time()
                        if now - last_triggered < GLOBAL_COOLDOWN_SEC:
                            print("[cheer] skipped trigger during reader cooldown")
                            continue

                        trigger_overlay(
                            effect,
                            {
                                "username": user,
                                "bits": bits,
                                "message": message,
                                "source": "twitch_cheer",
                            },
                        )
                        last_triggered = now
                        continue

                    state_command = catalog.match_state_command(message)
                    if state_command:
                        command, value = state_command
                        now = time.time()
                        if now - last_triggered < GLOBAL_COOLDOWN_SEC:
                            print("[state] skipped update during reader cooldown")
                            continue
                        if trigger_overlay_state(
                            command,
                            value,
                            {
                                "username": user,
                                "message": message,
                                "source": "twitch_chat_reader",
                            },
                        ):
                            last_triggered = now
                        continue

                    effects = catalog.match_message(message)
                    if not effects:
                        print(f'[DEBUG] Cannot find {message} in effects')
                        continue
                    print(f'{user} : {message}')
                    now = time.time()
                    if now - last_triggered < GLOBAL_COOLDOWN_SEC:
                        print("[chat] skipped trigger during reader cooldown")
                        continue
                    
                        
                    for effect in effects:
                        trigger_overlay(
                            effect,
                            {
                                "username": user,
                                "message": message,
                                "source": "twitch_chat_reader",
                            },
                        )
                        last_triggered = now

        except KeyboardInterrupt:
            print("[twitch] stopped")
            return
        except Exception as exc:
            print(f"[twitch] disconnected: {exc}; retrying in 5 seconds")
            time.sleep(5)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


if __name__ == "__main__":
    run_chat_reader()
