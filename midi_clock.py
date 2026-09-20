"""Emetteur MIDI Clock synchronise sur Ableton Link."""
from __future__ import annotations

import threading
import time

import rtmidi

CLOCK = 0xF8
START = 0xFA
CONTINUE = 0xFB
STOP = 0xFC
CLOCKS_PER_BEAT = 24
DEFAULT_PORT_NAME = "YAMAHA 01V96 Port1"


class MidiClockSender:
    """Envoie l'horloge Link vers un port MIDI de sortie dedie."""

    def __init__(self, port_name: str = DEFAULT_PORT_NAME, log=print):
        self._port_name = port_name
        self._log = log
        self._midi_out: rtmidi.MidiOut | None = None
        self._link = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()

    @staticmethod
    def list_ports() -> list[str]:
        midi_out = rtmidi.MidiOut()
        names = midi_out.get_ports()
        del midi_out
        return names

    @property
    def port_name(self) -> str:
        return self._port_name

    def set_port_name(self, port_name: str) -> None:
        if not port_name or port_name == self._port_name:
            return
        link = self._link
        self.close()
        self._port_name = port_name
        if link is not None:
            self.start(link)

    def start(self, link) -> None:
        if self._thread is not None:
            return
        midi_out = rtmidi.MidiOut()
        names = midi_out.get_ports()
        try:
            midi_out.open_port(names.index(self._port_name))
        except ValueError:
            del midi_out
            self._log(f"[MIDI Clock] port introuvable : {self._port_name}")
            return
        self._midi_out = midi_out
        self._link = link
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="midi-clock", daemon=True)
        self._thread.start()
        self._log(f"[MIDI Clock] sortie ouverte : {self._port_name}")

    def _send(self, message: int) -> None:
        midi_out = self._midi_out
        if midi_out is None:
            return
        with self._send_lock:
            midi_out.send_message([message])

    def _run(self) -> None:
        was_playing = False
        next_tick = 0.0
        while not self._stop.is_set():
            try:
                snapshot = self._link.snapshot(quantum=1.0)
                playing = bool(snapshot["is_playing"])
                bpm = max(1.0, float(snapshot["bpm"]))
                interval = 60.0 / bpm / CLOCKS_PER_BEAT
                now = time.perf_counter()
                if playing and not was_playing:
                    self._send(START)
                    phase = float(snapshot["phase"]) % 1.0
                    next_tick = now + (1.0 - phase) * 60.0 / bpm / CLOCKS_PER_BEAT
                elif not playing and was_playing:
                    self._send(STOP)
                    next_tick = 0.0
                if playing:
                    if next_tick <= 0.0:
                        next_tick = now + interval
                    while now >= next_tick:
                        self._send(CLOCK)
                        next_tick += interval
                    wait = min(0.002, max(0.0002, next_tick - now))
                else:
                    wait = 0.01
                was_playing = playing
            except (OSError, RuntimeError):
                wait = 0.01
            self._stop.wait(wait)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._midi_out is not None:
            if self._link is not None:
                try:
                    snapshot = self._link.snapshot(quantum=1.0)
                    if snapshot["is_playing"]:
                        self._send(STOP)
                except (OSError, RuntimeError):
                    pass
            self._midi_out.close_port()
            self._midi_out = None
        self._link = None
