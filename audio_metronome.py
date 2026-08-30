"""Métronome audio local (clic .wav), indépendant du métronome interne de
Live : notre propre compteur de temps (Link/MIDI, le même que celui affiché
à l'écran) déclenche les clics via la carte son choisie. Nécessaire car la
signature rythmique de Live (une seule valeur globale, recalculée depuis le
début du morceau) ne correspond pas à notre feuille de scène à mesures
variables (voir _push_live_time_signature) — son métronome audio compte donc
parfois faux, le nôtre reste toujours juste.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"
DEFAULT_KIT = "Kit1"


def list_output_devices() -> list[str]:
    """Noms des périphériques de sortie audio disponibles (carte système,
    interfaces USB/Thunderbolt...), pour peupler le sélecteur."""
    try:
        return [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
    except Exception:
        return []


def list_kits() -> list[str]:
    """Sous-dossiers de sounds/ contenant bien click.wav + click_up.wav
    (un "kit" de sons de clic), pour peupler le sélecteur."""
    if not _SOUNDS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in _SOUNDS_DIR.iterdir()
        if p.is_dir() and (p / "click.wav").is_file() and (p / "click_up.wav").is_file()
    )


def _load_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Charge un .wav 16 bits et le ramène à un signal mono float32 (-1..1)."""
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        samplerate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        raise ValueError(f"{path.name} : format audio non pris en charge (16 bits attendu)")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    return data, samplerate


class AudioMetronome:
    """Joue click.wav (temps normal) / click_up.wav (temps 1) sur la carte
    son et le nombre de canaux choisis (paire stéréo ou mono — toujours les
    premiers canaux du périphérique).

    Garde un unique OutputStream ouvert tant que le métronome est activé
    (voir set_enabled) : sd.play() ouvre/ferme un flux CoreAudio à CHAQUE
    clic, ce qui, combiné au traitement des événements souris de Tk/Cocoa,
    ralentissait visiblement toute l'appli dès que le métronome tournait.
    """

    def __init__(self) -> None:
        self.kit_name: str = DEFAULT_KIT
        self._click, self._click_sr = _load_wav_mono(_SOUNDS_DIR / DEFAULT_KIT / "click.wav")
        self._click_up, _ = _load_wav_mono(_SOUNDS_DIR / DEFAULT_KIT / "click_up.wav")
        self.device_name: str | None = None
        self.channels: int = 2
        self._stream: sd.OutputStream | None = None
        self._play_buf: np.ndarray | None = None
        self._play_pos: int = 0
        self._pending: np.ndarray | None = None

    def configure(self, device_name: str, channels: int) -> None:
        self.device_name = device_name or None
        self.channels = 1 if channels == 1 else 2
        if self._stream is not None:
            self._open_stream()

    def set_kit(self, kit_name: str) -> None:
        """Change de dossier de sons de clic (voir list_kits) ; ignoré si le
        dossier ou ses fichiers sont introuvables (kit toujours utilisable)."""
        kit_dir = _SOUNDS_DIR / (kit_name or DEFAULT_KIT)
        try:
            click, click_sr = _load_wav_mono(kit_dir / "click.wav")
            click_up, _ = _load_wav_mono(kit_dir / "click_up.wav")
        except Exception:
            return
        self.kit_name = kit_name
        self._click, self._click_sr = click, click_sr
        self._click_up = click_up
        if self._stream is not None:
            self._open_stream()

    def set_enabled(self, enabled: bool) -> None:
        """Ouvre/ferme le flux audio persistant (appelé au toggle du bouton
        "M", pas à chaque clic)."""
        if enabled:
            self._open_stream()
        else:
            self._close_stream()

    def close(self) -> None:
        self._close_stream()

    def _open_stream(self) -> None:
        self._close_stream()
        try:
            self._stream = sd.OutputStream(
                samplerate=self._click_sr, device=self.device_name, channels=self.channels,
                dtype="float32", callback=self._audio_callback,
            )
            self._stream.start()
        except Exception:
            # Périphérique déconnecté/indisponible : le métronome reste
            # silencieux, pas d'interruption de l'appli.
            self._stream = None

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self._play_buf = None
        self._pending = None
        self._play_pos = 0

    def _audio_callback(self, outdata, frames: int, _time_info, _status) -> None:
        # Appelé sur le thread audio de PortAudio, pas le thread Tk : ne fait
        # que copier des tableaux déjà prêts, aucune allocation lourde ici.
        pending = self._pending
        if pending is not None:
            self._pending = None
            self._play_buf = pending
            self._play_pos = 0
        buf = self._play_buf
        if buf is None or self._play_pos >= len(buf):
            outdata.fill(0)
            return
        n = min(frames, len(buf) - self._play_pos)
        outdata[:n] = buf[self._play_pos : self._play_pos + n]
        if n < frames:
            outdata[n:] = 0
        self._play_pos += n

    def _prepare(self, mono: np.ndarray) -> np.ndarray:
        if self.channels == 1:
            return mono.reshape(-1, 1)
        return np.column_stack([mono, mono])

    def play(self, beat: int) -> None:
        if self._stream is None:
            return
        mono = self._click_up if beat == 1 else self._click
        self._pending = self._prepare(mono)
