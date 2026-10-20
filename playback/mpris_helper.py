#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""FXRoute MPRIS player for hardware media keys (session D-Bus helper).

Runs on the system python3 (dbus-python + PyGObject, the runtime of
audio/bluez_agent.py), outside the venv. FXRoute writes the state of its
active source to stdin as JSON lines; MPRIS method calls leave on stdout as
JSON commands. The helper never controls playback itself.

gsd-media-keys (GNOME) sends media keys to one MPRIS player: a player that
appears while the current one is not playing, or one that starts playing
while the current one is not. A paused Spotify Desktop therefore keeps the
keys even after FXRoute switched to another source, and Play/Pause then
starts Spotify. The helper re-announces its bus name whenever another player
stops playing, appears or vanishes, and when gsd-media-keys restarts; as
soon as no other player is playing, the keys belong to FXRoute again.
"""

from __future__ import annotations

import json
import os
import sys

BUS_NAME = "org.mpris.MediaPlayer2.fxroute"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_INTERFACE = "org.mpris.MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MEDIA_KEYS_DAEMON = "org.gnome.SettingsDaemon.MediaKeys"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
PLAYBACK_STATUSES = ("Playing", "Paused", "Stopped")

# Debounce before a re-announce. A player that just stopped or appeared
# must be known as not playing to gsd-media-keys first.
ANNOUNCE_DELAY_MS = 500
# gsd-media-keys scans every existing player after a restart.
DAEMON_RESTART_DELAY_MS = 2000
STATUS_QUERY_TIMEOUT_S = 2.0

# MPRIS method -> FXRoute command.
COMMANDS = {
    "PlayPause": "toggle",
    "Play": "play",
    "Pause": "pause",
    "Stop": "stop",
    "Next": "next",
    "Previous": "previous",
}

ROOT_PROPERTIES = {
    "CanQuit": False,
    "CanRaise": False,
    "HasTrackList": False,
    "Identity": "FXRoute",
    "SupportedUriSchemes": [],
    "SupportedMimeTypes": [],
}

EXIT_UNAVAILABLE = 3


def normalize_state(raw) -> dict:
    """Coerce one state line from FXRoute into the fields the player uses."""
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("status")
    return {
        "status": status if status in PLAYBACK_STATUSES else "Stopped",
        "title": str(raw.get("title") or ""),
        "artist": str(raw.get("artist") or ""),
        "album": str(raw.get("album") or ""),
        "can_play": bool(raw.get("can_play")),
        "can_pause": bool(raw.get("can_pause")),
        "can_go_next": bool(raw.get("can_go_next")),
        "can_go_previous": bool(raw.get("can_go_previous")),
    }


class PlayerModel:
    """Plain-Python MPRIS player properties; D-Bus types are added later."""

    def __init__(self) -> None:
        self.state = normalize_state({})
        self.track_serial = 0

    def metadata(self) -> dict:
        state = self.state
        if not (state["title"] or state["artist"] or state["album"]):
            return {"mpris:trackid": NO_TRACK}
        metadata = {"mpris:trackid": f"/org/fxroute/track/{self.track_serial}"}
        if state["title"]:
            metadata["xesam:title"] = state["title"]
        if state["artist"]:
            metadata["xesam:artist"] = [state["artist"]]
        if state["album"]:
            metadata["xesam:album"] = state["album"]
        return metadata

    def properties(self) -> dict:
        state = self.state
        return {
            "PlaybackStatus": state["status"],
            "Metadata": self.metadata(),
            "Rate": 1.0,
            "MinimumRate": 1.0,
            "MaximumRate": 1.0,
            "CanPlay": state["can_play"],
            "CanPause": state["can_pause"],
            "CanGoNext": state["can_go_next"],
            "CanGoPrevious": state["can_go_previous"],
            "CanSeek": False,
            "CanControl": True,
        }

    def update(self, raw) -> dict:
        """Apply a state line; return the player properties that changed."""
        before = self.properties()
        state = normalize_state(raw)
        track = (state["title"], state["artist"], state["album"])
        if track != (self.state["title"], self.state["artist"], self.state["album"]):
            self.track_serial += 1
        self.state = state
        return {key: value for key, value in self.properties().items() if before.get(key) != value}


class AnnounceTracker:
    """Track the other MPRIS players and decide when to re-announce.

    Statuses are keyed by unique connection name, which is also the sender
    of their PropertiesChanged signals. ``None`` marks a player whose status
    query is still pending; it blocks a re-announce like a playing one.
    """

    def __init__(self, own_name: str = BUS_NAME) -> None:
        self.own_name = own_name
        self.owners: dict[str, str] = {}
        self.statuses: dict[str, str | None] = {}

    def is_other_player(self, name: str) -> bool:
        return name.startswith(MPRIS_PREFIX) and name != self.own_name

    def player_added(self, name: str, owner: str) -> None:
        self.owners[name] = owner
        self.statuses.setdefault(owner, None)

    def player_removed(self, name: str) -> None:
        owner = self.owners.pop(name, None)
        if owner is not None and owner not in self.owners.values():
            self.statuses.pop(owner, None)

    def status_known(self, owner: str, status) -> bool:
        """Record a queried status; True when a re-announce may follow."""
        if owner not in self.statuses:
            return False
        self.statuses[owner] = str(status)
        return True

    def status_changed(self, owner: str, status) -> bool:
        """Record a status signal; True when the player stopped playing."""
        if owner not in self.statuses:
            return False
        previous = self.statuses[owner]
        self.statuses[owner] = str(status)
        return previous == "Playing" and status != "Playing"

    def blocked(self) -> bool:
        """Another player is playing (or unknown): it keeps the media keys."""
        return any(self.statuses.get(owner) in ("Playing", None) for owner in self.owners.values())


def parse_lines(buffer: bytes) -> tuple[list, bytes]:
    """Split complete JSON lines off ``buffer``; malformed lines are dropped."""
    *lines, rest = buffer.split(b"\n")
    states = []
    for line in lines:
        try:
            states.append(json.loads(line))
        except ValueError:
            continue
    return states, rest


def log(message: str) -> None:
    print(f"mpris-helper: {message}", file=sys.stderr, flush=True)


def main() -> int:  # pragma: no cover - needs a session bus
    try:
        import dbus
        import dbus.mainloop.glib
        import dbus.service
        from gi.repository import GLib
    except ImportError as exc:
        log(f"python3 dbus/gi bindings unavailable: {exc}")
        return EXIT_UNAVAILABLE

    def to_dbus(value):
        if isinstance(value, bool):
            return dbus.Boolean(value)
        if isinstance(value, float):
            return dbus.Double(value)
        if isinstance(value, list):
            return dbus.Array([to_dbus(item) for item in value], signature="s")
        if isinstance(value, dict):
            return dbus.Dictionary(
                {key: (dbus.ObjectPath(item) if key == "mpris:trackid" else to_dbus(item))
                 for key, item in value.items()},
                signature="sv",
            )
        return dbus.String(value)

    def to_dbus_properties(properties: dict):
        return dbus.Dictionary({key: to_dbus(value) for key, value in properties.items()}, signature="sv")

    def emit(command: str) -> None:
        try:
            sys.stdout.write(json.dumps({"command": command}) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            mainloop.quit()

    class Player(dbus.service.Object):
        def __init__(self, connection, model: PlayerModel) -> None:
            super().__init__(connection, OBJECT_PATH)
            self.model = model

        def _properties(self, interface: str) -> dict:
            if interface == ROOT_INTERFACE:
                return ROOT_PROPERTIES
            if interface == PLAYER_INTERFACE:
                return self.model.properties()
            raise dbus.exceptions.DBusException(
                f"No such interface {interface}", name="org.freedesktop.DBus.Error.UnknownInterface")

        @dbus.service.method(PROPERTIES_INTERFACE, in_signature="ss", out_signature="v")
        def Get(self, interface, name):
            properties = self._properties(interface)
            if name not in properties:
                raise dbus.exceptions.DBusException(
                    f"No such property {name}", name="org.freedesktop.DBus.Error.UnknownProperty")
            return to_dbus_properties({name: properties[name]})[name]

        @dbus.service.method(PROPERTIES_INTERFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return to_dbus_properties(self._properties(interface))

        @dbus.service.method(PROPERTIES_INTERFACE, in_signature="ssv", out_signature="")
        def Set(self, interface, name, value):
            raise dbus.exceptions.DBusException(
                f"Property {name} is read-only", name="org.freedesktop.DBus.Error.PropertyReadOnly")

        @dbus.service.signal(PROPERTIES_INTERFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        @dbus.service.method(ROOT_INTERFACE, in_signature="", out_signature="")
        def Raise(self):
            pass

        @dbus.service.method(ROOT_INTERFACE, in_signature="", out_signature="")
        def Quit(self):
            pass

        @dbus.service.method(PLAYER_INTERFACE, in_signature="", out_signature="")
        def PlayPause(self):
            emit(COMMANDS["PlayPause"])

        @dbus.service.method(PLAYER_INTERFACE, in_signature="", out_signature="")
        def Play(self):
            emit(COMMANDS["Play"])

        @dbus.service.method(PLAYER_INTERFACE, in_signature="", out_signature="")
        def Pause(self):
            emit(COMMANDS["Pause"])

        @dbus.service.method(PLAYER_INTERFACE, in_signature="", out_signature="")
        def Stop(self):
            emit(COMMANDS["Stop"])

        @dbus.service.method(PLAYER_INTERFACE, in_signature="", out_signature="")
        def Next(self):
            emit(COMMANDS["Next"])

        @dbus.service.method(PLAYER_INTERFACE, in_signature="", out_signature="")
        def Previous(self):
            emit(COMMANDS["Previous"])

        @dbus.service.method(PLAYER_INTERFACE, in_signature="x", out_signature="")
        def Seek(self, offset):
            pass

        @dbus.service.method(PLAYER_INTERFACE, in_signature="ox", out_signature="")
        def SetPosition(self, track_id, position):
            pass

        @dbus.service.method(PLAYER_INTERFACE, in_signature="s", out_signature="")
        def OpenUri(self, uri):
            pass

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SessionBus()
    except dbus.exceptions.DBusException as exc:
        log(f"session bus unavailable: {exc}")
        return EXIT_UNAVAILABLE
    own_unique = bus.get_unique_name()
    model = PlayerModel()
    player = Player(bus, model)
    tracker = AnnounceTracker()
    mainloop = GLib.MainLoop()
    announce_timer = {"id": 0, "reason": ""}

    def request_name() -> bool:
        result = bus.request_name(BUS_NAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
        return result in (dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER, dbus.bus.REQUEST_NAME_REPLY_ALREADY_OWNER)

    def announce() -> bool:
        reason = announce_timer["reason"]
        announce_timer["id"] = 0
        if tracker.blocked():
            log(f"re-announce skipped ({reason}): another player is playing")
            return False
        try:
            bus.release_name(BUS_NAME)
            if not request_name():
                log("lost the bus name to another owner")
                mainloop.quit()
                return False
        except dbus.exceptions.DBusException as exc:
            log(f"re-announce failed: {exc}")
            return False
        log(f"re-announced ({reason})")
        return False

    def schedule_announce(reason: str, delay_ms: int = ANNOUNCE_DELAY_MS) -> None:
        if announce_timer["id"]:
            GLib.source_remove(announce_timer["id"])
        announce_timer["reason"] = reason
        announce_timer["id"] = GLib.timeout_add(delay_ms, announce)

    def query_status(name: str, owner: str, announce_after: bool = True) -> None:
        def done(status):
            known = tracker.owners.get(name) == owner and tracker.status_known(owner, status)
            if known and announce_after:
                schedule_announce(f"{name} appeared {status}")

        def failed(_error):
            done("Unknown")

        try:
            proxy = bus.get_object(name, OBJECT_PATH, introspect=False)
            proxy.Get(PLAYER_INTERFACE, "PlaybackStatus", dbus_interface=PROPERTIES_INTERFACE,
                      reply_handler=done, error_handler=failed, timeout=STATUS_QUERY_TIMEOUT_S)
        except dbus.exceptions.DBusException:
            failed(None)

    def on_name_owner_changed(name, old_owner, new_owner):
        if name == MEDIA_KEYS_DAEMON and new_owner:
            schedule_announce("gsd-media-keys started", DAEMON_RESTART_DELAY_MS)
            return
        if not tracker.is_other_player(name):
            return
        if old_owner:
            tracker.player_removed(name)
            if not new_owner:
                schedule_announce(f"{name} vanished")
        if new_owner:
            tracker.player_added(name, new_owner)
            query_status(name, new_owner)

    def on_properties_changed(interface, changed, _invalidated, sender=None):
        if interface != PLAYER_INTERFACE or sender == own_unique or "PlaybackStatus" not in changed:
            return
        if tracker.status_changed(sender, changed["PlaybackStatus"]):
            schedule_announce(f"{sender} stopped playing")

    buffer = {"data": b""}

    def on_stdin(fd, condition):
        data = b""
        if condition & GLib.IO_IN:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
        if not data:
            mainloop.quit()
            return False
        states, buffer["data"] = parse_lines(buffer["data"] + data)
        for state in states:
            changed = model.update(state)
            if changed:
                player.PropertiesChanged(PLAYER_INTERFACE, to_dbus_properties(changed), dbus.Array([], signature="s"))
        return True

    bus.add_signal_receiver(
        on_name_owner_changed,
        signal_name="NameOwnerChanged",
        dbus_interface="org.freedesktop.DBus",
        bus_name="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
    )
    bus.add_signal_receiver(
        on_properties_changed,
        signal_name="PropertiesChanged",
        dbus_interface=PROPERTIES_INTERFACE,
        path=OBJECT_PATH,
        sender_keyword="sender",
    )
    for name in bus.list_names():
        name = str(name)
        if tracker.is_other_player(name):
            try:
                owner = str(bus.get_name_owner(name))
            except dbus.exceptions.DBusException:
                continue
            tracker.player_added(name, owner)
            # The first request_name below already counts as appearing.
            query_status(name, owner, announce_after=False)
    if not request_name():
        log(f"{BUS_NAME} is owned by another process")
        return EXIT_UNAVAILABLE
    log(f"owns {BUS_NAME}")
    GLib.io_add_watch(sys.stdin.fileno(), GLib.PRIORITY_DEFAULT,
                      GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_stdin)
    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
