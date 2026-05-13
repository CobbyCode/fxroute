# Optional USB Hardware Controller

FXRoute can optionally talk to a small RP2040/ESP32-class MCU over USB CDC serial. The MCU remains the autonomous owner of the physical amplifier/input-selector logic. FXRoute only reads the current state and can send override/control commands.

Current target use case: an MCU controlling an Aiyima A70 input selector.

## Design goals

- Completely optional: FXRoute must run normally when no MCU is connected.
- No startup blocking: serial detection happens lazily through the hardware status API/UI polling.
- Safe serial behavior: explicit opt-in device path, short read/write timeouts, reconnect by retrying that device, and throttled logs.
- Small protocol surface: line-based text protocol, version 1.
- Cached state: the last valid MCU status line is kept and returned where useful.

## Files added or changed

- `hardware_controller.py`
  - Implements `HardwareController`.
  - Uses `pyserial`.
  - Opens only the explicit `HARDWARE_CONTROLLER_DEVICE` path when configured.
  - Detects a compatible MCU with `PING` → `PONG`.
  - Parses semicolon-separated status lines.
  - Serial errors close the connection and are returned as status notes instead of crashing FXRoute.
  - Reconnect retries are throttled to avoid tight loops.
  - Repeated log messages are throttled.
- `requirements.txt`
  - Adds `pyserial==3.5`.
- `config.py`
  - Adds optional `HARDWARE_CONTROLLER_DEVICE`.
- `main.py`
  - Creates a global optional `HardwareController` instance during app startup.
  - Closes it on shutdown.
  - Adds `/api/hardware/...` routes.
  - Runs serial work via `asyncio.to_thread(...)` so FastAPI is not blocked by serial I/O.
- `static/index.html` and `static/app.js`
  - Adds a compact `Amplifier Controller` card in Technical settings.
  - Polls status while Technical settings is open.
  - Disables controls when no controller is connected.

## Optional configuration

By default FXRoute does not open any serial device. This avoids touching unrelated USB serial hardware.

To enable hardware control, set an explicit serial device in `.env`:

```env
HARDWARE_CONTROLLER_DEVICE=/dev/ttyACM0
```

If unset or wrong, FXRoute continues to run. When unset, the UI shows that the controller is disabled until `HARDWARE_CONTROLLER_DEVICE` is configured.

## Serial protocol v1

All messages are line-based and newline-terminated.

### FXRoute → MCU

```text
PING
GET
SET INPUT RCA
SET INPUT XLR
PRESS INPUT
AUTO ON
AUTO OFF
```

### MCU → FXRoute

```text
PONG
OK
ERR UNKNOWN_CMD
POWER=1;TRIGGER=1;INPUT=RCA;RCA=1;XLR=0;AUTO=1
```

Expected behavior:

- `PING` should return `PONG`.
- `GET` should return a status line.
- Control commands may return `OK`; FXRoute then asks `GET` to refresh state.
- `ERR ...` is treated as command failure but must not crash FXRoute.

## Parsed status fields

FXRoute currently recognizes these keys from the status line:

- `POWER` → boolean
- `TRIGGER` → boolean
- `INPUT` → string, usually `RCA` or `XLR`
- `RCA` → boolean
- `XLR` → boolean
- `AUTO` → boolean

`0` and `1` are converted to booleans. Other values remain strings.

Example parsed payload from `/api/hardware/status`:

```json
{
  "available": true,
  "connected": true,
  "device": "/dev/ttyACM0",
  "status": {
    "POWER": true,
    "TRIGGER": true,
    "INPUT": "RCA",
    "RCA": true,
    "XLR": false,
    "AUTO": true
  },
  "raw": "POWER=1;TRIGGER=1;INPUT=RCA;RCA=1;XLR=0;AUTO=1",
  "power": true,
  "trigger": true,
  "input": "RCA",
  "rca": true,
  "xlr": false,
  "auto": true,
  "notes": []
}
```

## API routes

- `GET /api/hardware/status`
  - Scans/reconnects if needed.
  - Sends `GET` when connected.
  - Returns current/cached status and notes.
- `POST /api/hardware/input/rca`
  - Sends `SET INPUT RCA`, then `GET`.
- `POST /api/hardware/input/xlr`
  - Sends `SET INPUT XLR`, then `GET`.
- `POST /api/hardware/input/press`
  - Sends `PRESS INPUT`, then `GET`.
- `POST /api/hardware/auto/on`
  - Sends `AUTO ON`, then `GET`.
- `POST /api/hardware/auto/off`
  - Sends `AUTO OFF`, then `GET`.

## Frontend behavior

The card lives in:

```text
FXRoute logo → Technical settings → Amplifier Controller
```

It shows:

- connection state and serial device path
- current input
- trigger state
- power state
- auto mode

Buttons:

- `RCA`
- `XLR`
- `Press Input`
- `Auto On`
- `Auto Off`

Buttons are disabled when `connected=false` or while a command is pending.

## Validation already done

The implementation was checked without real MCU hardware:

```bash
python3 -m py_compile main.py config.py hardware_controller.py measurement.py easyeffects.py
node --check static/app.js
git diff --check
```

A fake-serial smoke test verified:

- `PING` → `PONG` detection
- `GET` status parsing
- `SET INPUT XLR` command path
- no crash on the simulated serial path

## Hardware still to test later

With a real RP2040/ESP32 attached:

1. Install/update dependencies so `pyserial` exists in the FXRoute venv.
2. Start/restart FXRoute.
3. Check:

   ```bash
   curl http://localhost:8000/api/hardware/status
   ```

4. Confirm that `device`, `connected`, and parsed status fields are correct.
5. Press the UI buttons and verify the MCU receives exactly:

   ```text
   SET INPUT RCA
   SET INPUT XLR
   PRESS INPUT
   AUTO ON
   AUTO OFF
   ```

6. Unplug/replug the MCU and confirm reconnect works without restarting FXRoute.

## Future polish ideas

- Add an explicit protocol/version field if the MCU firmware grows.
- Add a small MCU firmware example once pinout/logic is final.
- Consider hiding the card until a controller is detected if the settings page feels too busy.
- Add a UI timestamp for last valid status once real hardware behavior is known.
