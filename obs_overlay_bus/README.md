# OBS Overlay Bus

A generic local visual event bus for OBS Browser Source overlays.

OBS loads one transparent browser page at `http://localhost:8765/overlay`. Local tools can `POST /trigger` with an effect name. If the name exists in `config/overlays.json`, connected browser sources receive an SSE event and render the registered media asset, then return to transparent.

The overlay renderer is intentionally not Twitch-specific. Twitch bots, Stream Deck actions, Python scripts, local games, and other tools can all trigger the same effect registry.

## Install

```bash
cd obs_overlay_bus
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The server loads a `.env` file if one exists in this folder or a parent folder. Useful optional values:

```env
OBS_OVERLAY_HOST=127.0.0.1
OBS_OVERLAY_PORT=8765
OBS_OVERLAY_CONFIG=config/overlays.json
```

## Run

```bash
cd obs_overlay_bus
uvicorn server:app --host 127.0.0.1 --port 8765
```

Or:

```bash
cd obs_overlay_bus
python server.py
```

Open `http://localhost:8765/effects` to confirm the registry loaded.

Open `http://localhost:8765/status` to confirm whether an OBS/browser overlay is connected. `overlay_clients` should be at least `1` when the Browser Source is active.

To enable scene-aware routing, add OBS WebSocket settings to `.env`:

```env
OBS_WS_HOST=localhost
OBS_WS_PORT=4455
OBS_WS_PASSWORD=
OBS_SCENE_LOCKOUT_MS=500
```

If OBS WebSocket is unavailable, the overlay bus logs a warning and keeps running.

## OBS Setup

1. Add a Browser Source.
2. Set URL to `http://localhost:8765/overlay`.
3. Set width and height to your stream canvas, such as `1920x1080`.
4. Enable transparency.
5. Keep this source above the main scene layer.

The page stays invisible until a registered effect is triggered.

## Trigger Effects

PowerShell:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8765/trigger" `
  -ContentType "application/json" `
  -Body '{"effect":"hammer_bot","payload":{"username":"test_bot"}}'
```

curl:

```bash
curl -X POST http://localhost:8765/trigger \
  -H "Content-Type: application/json" \
  -d '{"effect":"hammer_bot","payload":{"username":"test_bot"}}'
```

If the effect exists:

```json
{
  "ok": true,
  "effect": "hammer_bot",
  "clients": 1
}
```

If the effect is not registered:

```json
{
  "ok": false,
  "error": "unknown_effect",
  "effect": "missing_name"
}
```

## Add Media Assets

Put files in `assets/`, then register them in `config/overlays.json`.

```json
{
  "effects": {
    "survival_win": {
      "type": "video",
      "src": "/assets/survival_win.webm",
      "duration_ms": 3200,
      "cooldown_ms": 1000,
      "queue_policy": "queue",
      "volume": 0.7,
      "position": {
        "left": "0px",
        "top": "0px",
        "width": "100%",
        "height": "100%"
      }
    }
  }
}
```

Supported renderer types for the MVP are `video` and `image`. The registry also accepts future-friendly type names: `text`, `html`, `sprite`, and `lottie`.

For alpha WebM overlays, export the video with transparency and use `type: "video"`.

## Scene-Aware Effects

The old flat `effects` config still works. Scene-aware routing adds two optional top-level sections:

- `scenes`: effects that are valid only for a named OBS scene.
- `global_effects`: effects that can play in any scene.

Example:

```json
{
  "effects": {
    "hammer_bot": {
      "type": "video",
      "src": "/assets/hammer_test.webm",
      "duration_ms": 3000,
      "queue_policy": "drop_if_busy"
    }
  },
  "scenes": {
    "Main Room": {
      "effects": {
        "ufo": {
          "type": "video",
          "src": "/assets/main_room/ufo_window.webm",
          "duration_ms": 4500,
          "queue_policy": "drop_if_busy"
        }
      }
    },
    "Programmer View": {
      "effects": {
        "ufo": {
          "alias": "monitor_intrusion"
        },
        "monitor_intrusion": {
          "type": "video",
          "src": "/assets/programmer_view/monitor_intrusion.webm",
          "duration_ms": 3000,
          "queue_policy": "restart"
        }
      }
    }
  },
  "global_effects": {
    "lens_splatter": {
      "type": "video",
      "src": "/assets/global/lens_splatter.webm",
      "duration_ms": 2000
    }
  }
}
```

When `/trigger` receives `{"effect":"ufo"}`, the server resolves it in this order:

1. Current scene effect.
2. Scene-local alias.
3. `global_effects`.
4. Old top-level `effects`.
5. Ignore cleanly if the effect is not valid for the current scene.

Scene-bound effects are dropped briefly after a scene change based on `OBS_SCENE_LOCKOUT_MS`. Global and old flat effects can still play during that lockout. This prevents scene-specific animations, like UFOs, from flying through walls or ceilings during a transition.

`GET /status` shows OBS scene state:

```json
{
  "ok": true,
  "current_scene": "Main Room",
  "obs_connected": true,
  "last_scene_change_ms": 123456789,
  "available_scene_effects": ["ufo"],
  "available_global_effects": ["lens_splatter"]
}
```

## Backend Events

Higher-level events can be routed to scene-aware effects through `POST /event`:

```json
{
  "type": "raid",
  "payload": {
    "username": "raiding_channel",
    "viewer_count": 25
  }
}
```

The backend maps event types to effect names through top-level `event_effects`:

```json
{
  "event_effects": {
    "raid": "raid",
    "first_time_chatter": "first_time_chatter",
    "follow": "follow"
  }
}
```

Those effect names resolve through the same scene-aware routing as normal triggers, so each scene can define its own `raid`, `first_time_chatter`, or `follow` effect.

The Twitch IRC reader can detect:

- `raid` from Twitch `USERNOTICE` messages.
- `first_time_chatter` from the IRC `first-msg` tag.
- Cheers/Bits from the IRC `bits` tag.

New followers are not exposed through regular IRC chat. A future EventSub helper or another local bot can POST `{"type":"follow"}` to `/event`.

Example scene-specific event effects:

```json
{
  "scenes": {
    "TDTests": {
      "effects": {
        "raid": {
          "type": "video",
          "src": "/assets/tdtests/raid.webm",
          "duration_ms": 4000
        },
        "first_time_chatter": {
          "type": "video",
          "src": "/assets/tdtests/first_chat.webm",
          "duration_ms": 2500
        }
      }
    }
  }
}
```

## Queue Policies

Each effect tracks its own playback state in the browser:

- `drop_if_busy`: ignore new triggers while that effect is already playing.
- `queue`: play triggers in order after the current playback and cooldown finish.
- `restart`: restart the active effect immediately.

`cooldown_ms` is enforced per effect after playback finishes.

## Reload Config

After editing `config/overlays.json`, reload without restarting:

```bash
curl -X POST http://localhost:8765/reload
```

Connected overlay pages receive a reload event and refresh their registry.

## Twitch Bot Integration

For a plain Twitch chat reader that triggers overlays when the streamer types an effect keyword, use:

```bash
python twitch_chat_reader.py
```

Add these values to `.env`:

```env
TWITCH_NICK=your_bot_login
AUTH=oauth_token_here
TWITCH_CHANNEL=your_channel
TWITCH_STREAMER=your_streamer_login
TWITCH_IRC_TLS=true
TWITCH_IRC_PORT=6697
OBS_OVERLAY_URL=http://127.0.0.1:8765
```

The reader only listens for messages from `TWITCH_STREAMER`. It matches configured effect names from `config/overlays.json`, and by default only enables effects whose `src` file exists in `assets/`.

The IRC reader uses Twitch's TLS IRC endpoint on port `6697` by default, replies to Twitch keepalive `PING` messages, and reconnects when Twitch sends `RECONNECT` or closes the connection. Restart the reader after rotating `AUTH`; `.env` is loaded only when the process starts.

The reader also requests Twitch IRC tags and can trigger overlays from Cheers/Bits without EventSub setup. Add a top-level `cheer_effects` object to map bit amounts to effect names:

```json
{
  "cheer_effects": {
    "1": "cheer1",
    "420": "cheer420",
    "1000+": "big_cheer"
  }
}
```

Exact matches win first. A key ending in `+` acts as a minimum threshold, so `1000+` matches any cheer of 1000 bits or more. If no explicit mapping exists, the reader falls back to an effect named `cheer{bits}`, such as `cheer420`.

Example: if `sparkle` is configured and `/assets/sparkle.gif` exists, the streamer can type:

```text
sparkle
```

or:

```text
!sparkle
```

To allow configured effects even before the asset files exist, set:

```env
TWITCH_REQUIRE_ASSET_EXISTS=false
```

The reusable helper inside `twitch_chat_reader.py` is:

```python
import requests


def trigger_overlay(effect: str, payload: dict | None = None):
    try:
        requests.post(
            "http://localhost:8765/trigger",
            json={"effect": effect, "payload": payload or {}},
            timeout=1.0,
        )
    except requests.RequestException as e:
        print(f"[overlay] trigger failed: {e}")
```

Example usage:

```python
trigger_overlay("hammer_bot", {
    "username": username,
    "reason": "ban",
})
```

## Debugging

The browser page logs useful connection, reload, queue, and playback messages to the console. In OBS, right-click the Browser Source and use the browser interaction/devtools options available in your OBS build.

The renderer appends media only after the browser reports it can load. Missing or broken asset paths are logged and kept invisible.
