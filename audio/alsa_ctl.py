# SPDX-License-Identifier: AGPL-3.0-only
"""ALSA control access for digital-input lock status and clock selection.

Uses card controls straight from ``/dev/snd/controlC<card>`` via the ALSA
control ioctls, so no alsa-utils dependency is needed. Only lock style
controls of a digital receiver are read: USB Audio Class 2 clock validity
("S/PDIF Validity") and driver lock flags ("IEC958 In Lock", "SPDIF In
Status" and similar). The only control written is a USB Audio Class 2 clock
selector ("... Clock Source"), switched between Internal and a locked
digital input.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import re

logger = logging.getLogger(__name__)

_ELEM_ID_NAME_MAXLEN = 44
_ELEM_TYPE_BOOLEAN = 1
_ELEM_TYPE_ENUMERATED = 3
_ELEM_ACCESS_READ = 1 << 0
_ELEM_ACCESS_WRITE = 1 << 1
_MAX_ELEMENTS = 1024

_DIGITAL_WORDS = re.compile(r"s/?pdif|iec\s?958|optical|coax|toslink|digital", re.IGNORECASE)
_LOCK_WORDS = re.compile(r"valid|lock|sync|status", re.IGNORECASE)
# Playback-side digital controls never describe an incoming stream.
_PLAYBACK_WORDS = re.compile(r"playback|output|\bout\b", re.IGNORECASE)
# USB Audio Class 2 selectors are named "<selector> Clock Source"; each item
# is a clock source whose lock control is "<item> Validity".
_CLOCK_SELECTOR = re.compile(r"clock source$", re.IGNORECASE)
_INTERNAL_CLOCK = re.compile(r"internal", re.IGNORECASE)


class _ElemId(ctypes.Structure):
    _fields_ = [
        ("numid", ctypes.c_uint),
        ("iface", ctypes.c_int),
        ("device", ctypes.c_uint),
        ("subdevice", ctypes.c_uint),
        ("name", ctypes.c_char * _ELEM_ID_NAME_MAXLEN),
        ("index", ctypes.c_uint),
    ]


class _ElemList(ctypes.Structure):
    _fields_ = [
        ("offset", ctypes.c_uint),
        ("space", ctypes.c_uint),
        ("used", ctypes.c_uint),
        ("count", ctypes.c_uint),
        ("pids", ctypes.c_void_p),
        ("reserved", ctypes.c_ubyte * 50),
    ]


class _ElemInfoInteger(ctypes.Structure):
    _fields_ = [("min", ctypes.c_long), ("max", ctypes.c_long), ("step", ctypes.c_long)]


class _ElemInfoEnumerated(ctypes.Structure):
    _fields_ = [
        ("items", ctypes.c_uint),
        ("item", ctypes.c_uint),
        ("name", ctypes.c_char * 64),
        ("names_ptr", ctypes.c_uint64),
        ("names_length", ctypes.c_uint),
    ]


class _ElemInfoValue(ctypes.Union):
    _fields_ = [
        ("integer", _ElemInfoInteger),
        ("integer64", ctypes.c_longlong * 3),
        ("enumerated", _ElemInfoEnumerated),
        ("reserved", ctypes.c_ubyte * 128),
    ]


class _ElemInfo(ctypes.Structure):
    _fields_ = [
        ("id", _ElemId),
        ("type", ctypes.c_int),
        ("access", ctypes.c_uint),
        ("count", ctypes.c_uint),
        ("owner", ctypes.c_int),
        ("value", _ElemInfoValue),
        ("reserved", ctypes.c_ubyte * 64),
    ]


class _ElemValueUnion(ctypes.Union):
    _fields_ = [
        ("integer", ctypes.c_long * 128),
        ("integer64", ctypes.c_longlong * 64),
        ("enumerated", ctypes.c_uint * 128),
        ("bytes", ctypes.c_ubyte * 512),
    ]


class _ElemValue(ctypes.Structure):
    _fields_ = [
        ("id", _ElemId),
        ("indirect", ctypes.c_uint),
        ("value", _ElemValueUnion),
        ("reserved", ctypes.c_ubyte * 128),
    ]


def _iowr(number: int, size: int) -> int:
    # _IOWR('U', number, size) on the generic Linux ioctl layout.
    return (3 << 30) | (size << 16) | (ord("U") << 8) | number


_IOCTL_ELEM_LIST = _iowr(0x10, ctypes.sizeof(_ElemList))
_IOCTL_ELEM_INFO = _iowr(0x11, ctypes.sizeof(_ElemInfo))
_IOCTL_ELEM_READ = _iowr(0x12, ctypes.sizeof(_ElemValue))
_IOCTL_ELEM_WRITE = _iowr(0x13, ctypes.sizeof(_ElemValue))


def is_digital_lock_control(name: str) -> bool:
    """Whether a control name reports the lock of an incoming digital stream."""
    return bool(
        _DIGITAL_WORDS.search(name)
        and _LOCK_WORDS.search(name)
        and not _PLAYBACK_WORDS.search(name)
    )


def _readable_elements(fd: int) -> list[tuple[_ElemId, _ElemInfo]]:
    listing = _ElemList()
    fcntl.ioctl(fd, _IOCTL_ELEM_LIST, listing, True)
    count = min(int(listing.count), _MAX_ELEMENTS)
    ids = (_ElemId * max(count, 1))()
    listing = _ElemList(offset=0, space=count, pids=ctypes.addressof(ids))
    fcntl.ioctl(fd, _IOCTL_ELEM_LIST, listing, True)
    result = []
    for elem_id in ids[: int(listing.used)]:
        info = _ElemInfo(id=elem_id)
        fcntl.ioctl(fd, _IOCTL_ELEM_INFO, info, True)
        if info.access & _ELEM_ACCESS_READ:
            result.append((elem_id, info))
    return result


def _read_booleans(fd: int, elements: list[tuple[_ElemId, _ElemInfo]]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for elem_id, info in elements:
        if info.type != _ELEM_TYPE_BOOLEAN:
            continue
        value = _ElemValue(id=elem_id)
        fcntl.ioctl(fd, _IOCTL_ELEM_READ, value, True)
        values = [int(value.value.integer[index]) for index in range(min(int(info.count), 128))]
        name = elem_id.name.decode(errors="replace")
        result[name] = any(values) or result.get(name, False)
    return result


def read_boolean_controls(card: int) -> dict[str, bool]:
    """Return every readable boolean control of ``card`` as name -> state.

    A multi-value boolean counts as on when any of its values is on. Raises
    OSError when the control device cannot be opened or queried.
    """
    fd = os.open(f"/dev/snd/controlC{int(card)}", os.O_RDONLY | os.O_CLOEXEC)
    try:
        return _read_booleans(fd, _readable_elements(fd))
    finally:
        os.close(fd)


def read_digital_lock(card: int | None) -> bool | None:
    """Lock state of the digital receiver on ``card``, or None when unknown.

    Locked when any digital lock control is on; unknown when the card
    exposes no such control or cannot be read.
    """
    if card is None:
        return None
    try:
        controls = read_boolean_controls(card)
    except OSError as exc:
        logger.debug("ALSA control read failed for card %s: %s", card, exc)
        return None
    states = [state for name, state in controls.items() if is_digital_lock_control(name)]
    if not states:
        return None
    return any(states)


def clock_source_target(names: list[str], current: int, validity: dict[str, bool], *,
                        digital: bool) -> int | None:
    """Selector item to switch to, or None to leave the selector alone.

    With ``digital`` an Internal selector moves to a digital source whose
    "<item> Validity" control reports a lock. Without it a selector on a
    digital source returns to Internal. Other sources such as ADAT, and
    digital sources without a lock, are never selected or left.
    """
    internal = next((index for index, name in enumerate(names) if _INTERNAL_CLOCK.search(name)), None)
    if internal is None or not 0 <= current < len(names):
        return None
    if not digital:
        return internal if _DIGITAL_WORDS.search(names[current]) else None
    if current != internal:
        return None
    return next((index for index, name in enumerate(names)
                 if _DIGITAL_WORDS.search(name) and validity.get(f"{name} Validity")), None)


def select_clock_source(card: int | None, *, digital: bool) -> str | None:
    """Apply ``clock_source_target`` to every clock selector of ``card``.

    Returns the last newly selected source name, or None when nothing
    changed or the card cannot be read or written.
    """
    if card is None:
        return None
    try:
        fd = os.open(f"/dev/snd/controlC{int(card)}", os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        logger.debug("ALSA control open failed for card %s: %s", card, exc)
        return None
    try:
        elements = _readable_elements(fd)
        validity = _read_booleans(fd, elements)
        selected = None
        for elem_id, info in elements:
            name = elem_id.name.decode(errors="replace")
            if (info.type != _ELEM_TYPE_ENUMERATED or not info.access & _ELEM_ACCESS_WRITE
                    or not _CLOCK_SELECTOR.search(name)):
                continue
            names = []
            for index in range(int(info.value.enumerated.items)):
                item = _ElemInfo(id=elem_id)
                item.value.enumerated.item = index
                fcntl.ioctl(fd, _IOCTL_ELEM_INFO, item, True)
                names.append(item.value.enumerated.name.decode(errors="replace"))
            value = _ElemValue(id=elem_id)
            fcntl.ioctl(fd, _IOCTL_ELEM_READ, value, True)
            target = clock_source_target(names, int(value.value.enumerated[0]), validity, digital=digital)
            if target is None:
                continue
            value.value.enumerated[0] = target
            fcntl.ioctl(fd, _IOCTL_ELEM_WRITE, value, True)
            selected = names[target]
        return selected
    except OSError as exc:
        logger.debug("ALSA clock selection failed for card %s: %s", card, exc)
        return None
    finally:
        os.close(fd)
