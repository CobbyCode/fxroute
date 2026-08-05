"""Playback-State-Helfer (REFACTOR-006-Extrakt).

Zustandsfreie Prüffunktionen rund um Player-/Spotify-State und
Track-Matching, 1:1 aus ``main.py`` extrahiert. Keine Imports aus ``main``
oder anderen Projektmodulen — nur stdlib.
"""


def is_local_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("current_file") and not state.get("paused") and not state.get("ended"))


def is_spotify_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("available") and state.get("status") == "Playing")


def playback_state_matches_track(state: dict | None, track: dict | None) -> bool:
    state = state or {}
    track = track or {}
    source = track.get("source")
    current_file = state.get("current_file")
    track_url = track.get("url")
    if source in {"local", "radio"} and current_file and track_url and current_file != track_url:
        return False
    return True
