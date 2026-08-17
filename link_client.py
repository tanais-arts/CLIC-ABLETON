"""Bindings ctypes minimalistes pour la bibliothèque native abl_link (C API
officielle d'Ableton pour Link), compilée depuis les sources dans link-src/.

Ne couvre que ce qui est nécessaire à l'affichage temps réel du temps (beat)
et du tempo : activation de Link, lecture du tempo, de la phase (position
dans la mesure) et de l'état de lecture (start/stop).
"""
from __future__ import annotations

import ctypes
import os
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_DYLIB_PATH = os.path.join(_HERE, "link-lib", "libabl_link.dylib")


class _AblLink(ctypes.Structure):
    _fields_ = [("impl", ctypes.c_void_p)]


class _AblLinkSessionState(ctypes.Structure):
    _fields_ = [("impl", ctypes.c_void_p)]


def _load_library() -> ctypes.CDLL:
    if not os.path.exists(_DYLIB_PATH):
        raise LinkUnavailable(
            f"Bibliothèque Ableton Link introuvable : {_DYLIB_PATH}. "
            "Voir README.md pour la compiler depuis link-src/."
        )
    try:
        lib = ctypes.CDLL(_DYLIB_PATH)
    except OSError as exc:
        raise LinkUnavailable(f"Impossible de charger {_DYLIB_PATH} : {exc}") from exc

    lib.abl_link_create.argtypes = [ctypes.c_double]
    lib.abl_link_create.restype = _AblLink

    lib.abl_link_destroy.argtypes = [_AblLink]
    lib.abl_link_destroy.restype = None

    lib.abl_link_enable.argtypes = [_AblLink, ctypes.c_bool]
    lib.abl_link_enable.restype = None

    lib.abl_link_is_enabled.argtypes = [_AblLink]
    lib.abl_link_is_enabled.restype = ctypes.c_bool

    lib.abl_link_enable_start_stop_sync.argtypes = [_AblLink, ctypes.c_bool]
    lib.abl_link_enable_start_stop_sync.restype = None

    lib.abl_link_num_peers.argtypes = [_AblLink]
    lib.abl_link_num_peers.restype = ctypes.c_uint64

    lib.abl_link_clock_micros.argtypes = [_AblLink]
    lib.abl_link_clock_micros.restype = ctypes.c_int64

    lib.abl_link_create_session_state.argtypes = []
    lib.abl_link_create_session_state.restype = _AblLinkSessionState

    lib.abl_link_destroy_session_state.argtypes = [_AblLinkSessionState]
    lib.abl_link_destroy_session_state.restype = None

    lib.abl_link_capture_app_session_state.argtypes = [_AblLink, _AblLinkSessionState]
    lib.abl_link_capture_app_session_state.restype = None

    lib.abl_link_tempo.argtypes = [_AblLinkSessionState]
    lib.abl_link_tempo.restype = ctypes.c_double

    lib.abl_link_is_playing.argtypes = [_AblLinkSessionState]
    lib.abl_link_is_playing.restype = ctypes.c_bool

    lib.abl_link_beat_at_time.argtypes = [_AblLinkSessionState, ctypes.c_int64, ctypes.c_double]
    lib.abl_link_beat_at_time.restype = ctypes.c_double

    lib.abl_link_phase_at_time.argtypes = [_AblLinkSessionState, ctypes.c_int64, ctypes.c_double]
    lib.abl_link_phase_at_time.restype = ctypes.c_double

    return lib


class LinkUnavailable(RuntimeError):
    """La bibliothèque native Link n'a pas pu être chargée."""


class AbletonLink:
    """Petit wrapper haut niveau autour de la C API abl_link."""

    def __init__(self, initial_bpm: float = 120.0):
        self._lib = _load_library()
        self._link = self._lib.abl_link_create(initial_bpm)
        self._lock = threading.Lock()
        self._session_state = self._lib.abl_link_create_session_state()

    def enable(self, enable: bool = True) -> None:
        self._lib.abl_link_enable(self._link, enable)
        # Permet de recevoir l'état start/stop du transport de Live.
        self._lib.abl_link_enable_start_stop_sync(self._link, enable)

    @property
    def is_enabled(self) -> bool:
        return bool(self._lib.abl_link_is_enabled(self._link))

    @property
    def num_peers(self) -> int:
        return int(self._lib.abl_link_num_peers(self._link))

    def now_micros(self) -> int:
        return int(self._lib.abl_link_clock_micros(self._link))

    def snapshot(self, quantum: float, offset_micros: int = 0) -> dict:
        """Retourne {bpm, phase, beat, is_playing} au temps courant + offset.

        `offset_micros` permet de compenser la latence de transmission en
        interrogeant Link dans le futur (offset > 0) ou le passé (offset < 0).
        """
        with self._lock:
            self._lib.abl_link_capture_app_session_state(self._link, self._session_state)
            when = self.now_micros() + offset_micros
            bpm = self._lib.abl_link_tempo(self._session_state)
            phase = self._lib.abl_link_phase_at_time(self._session_state, when, quantum)
            beat = self._lib.abl_link_beat_at_time(self._session_state, when, quantum)
            is_playing = bool(self._lib.abl_link_is_playing(self._session_state))
        return {"bpm": bpm, "phase": phase, "beat": beat, "is_playing": is_playing}

    def close(self) -> None:
        self._lib.abl_link_destroy_session_state(self._session_state)
        self._lib.abl_link_destroy(self._link)
