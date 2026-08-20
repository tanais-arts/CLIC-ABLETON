"""Pont HUI -> OSC : traduit les messages MIDI HUI reçus d'une console de
mixage (ex. Yamaha 01V96V2 en mode "HUI") en commandes OSC vers Ableton Live
(volume et mute des pistes). Un canal HUI = une piste Live, dans l'ordre
(tranche 1 -> piste 1, etc.). Sens unique pour l'instant : la console pilote
Live, Live ne renvoie rien vers la console (pas de LED/fader motorisé).

Résumé du protocole HUI (source : reverse-engineering "theageman", 2010,
largement repris depuis) :
  - Fader : Pitch Bend sur le canal MIDI 0-7 (= tranche 1-8), position sur
    14 bits (LSB puis MSB) répartie sur 0-16383.
  - Switches (mute, solo, select...) : une paire de Control Change sur le
    canal 0 -> CC 0x0F (zone = tranche 0-7) puis CC 0x2F (port sur les 3 bits
    de poids faible, bit 0x40 = enfoncé/relâché). Port 5 = bouton Mute.
  - Ping : pour que la console continue à envoyer les faders en continu, il
    faut lui envoyer un ping (Note On canal 0, note 0, vélocité 0) toutes les
    ~1s ; sans ping elle arrête l'envoi des faders au bout de 2s (le mute
    n'est pas affecté).

Ce mapping n'a pas été validé sur le 01V96V2 réel : tout message non reconnu
est journalisé (au lieu d'être ignoré silencieusement) pour permettre un
ajustement si la console diverge du protocole HUI générique.
"""
from __future__ import annotations

import threading

import rtmidi

ZONE_CC = 0x0F
PORT_CC = 0x2F
PORT_ON_MASK = 0x40
PORT_NUMBER_MASK = 0x07
MUTE_PORT = 5
PING_INTERVAL = 1.0  # secondes


class HuiBridge:
    """Ouvre un port MIDI IN (messages HUI) et, si possible, un port MIDI OUT
    du même nom (pour le ping), et traduit les événements reçus vers OSC."""

    def __init__(self, live_osc, log=print):
        self._live_osc = live_osc
        self._log = log
        self._midi_in: rtmidi.MidiIn | None = None
        self._midi_out: rtmidi.MidiOut | None = None
        self._port_name: str | None = None
        self._pending_zone: int | None = None
        self._mute_state: dict[int, bool] = {}
        self._ping_stop = threading.Event()
        self._ping_thread: threading.Thread | None = None

    @staticmethod
    def list_ports() -> list[str]:
        probe = rtmidi.MidiIn()
        names = probe.get_ports()
        del probe
        return names

    @property
    def port_name(self) -> str | None:
        return self._port_name

    def connect(self, port_name: str) -> None:
        self.close()
        midi_in = rtmidi.MidiIn()
        names = midi_in.get_ports()
        midi_in.open_port(names.index(port_name))
        midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        midi_in.set_callback(self._callback)
        self._midi_in = midi_in
        self._port_name = port_name

        # Port de sortie du même nom, pour le ping qui maintient l'envoi des
        # faders côté console (le mute continue de fonctionner sans ping).
        try:
            midi_out = rtmidi.MidiOut()
            out_names = midi_out.get_ports()
            midi_out.open_port(out_names.index(port_name))
            self._midi_out = midi_out
        except ValueError:
            self._midi_out = None
            self._log(f"HUI : pas de port MIDI OUT « {port_name} » pour le ping (faders coupés après ~2s).")

        self._ping_stop.clear()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self._ping_thread.start()

    def close(self) -> None:
        self._ping_stop.set()
        self._ping_thread = None
        if self._midi_in is not None:
            self._midi_in.close_port()
            self._midi_in = None
        if self._midi_out is not None:
            self._midi_out.close_port()
            self._midi_out = None
        self._port_name = None
        self._pending_zone = None

    def _ping_loop(self) -> None:
        while not self._ping_stop.wait(PING_INTERVAL):
            if self._midi_out is not None:
                self._midi_out.send_message([0x90, 0x00, 0x00])

    def _callback(self, event, _data=None) -> None:
        message, _delta_time = event
        try:
            self._handle_message(message)
        except Exception as exc:  # noqa: BLE001 - ne doit jamais couper le thread MIDI
            self._log(f"HUI : erreur sur le message {message!r} : {exc}")

    def _handle_message(self, message: list[int]) -> None:
        if len(message) < 2:
            return
        status = message[0]
        kind = status & 0xF0
        channel = status & 0x0F

        if kind == 0xE0 and len(message) >= 3:
            # Pitch bend = position du fader de la tranche `channel` (0-7).
            value = message[1] | (message[2] << 7)
            self._live_osc.set_track_volume(channel, value / 16383.0)
            return

        if kind == 0xB0 and len(message) >= 3:
            cc, cc_value = message[1], message[2]
            if cc == ZONE_CC:
                self._pending_zone = cc_value
                return
            if cc == PORT_CC and self._pending_zone is not None:
                zone = self._pending_zone
                self._pending_zone = None
                port = cc_value & PORT_NUMBER_MASK
                pressed = bool(cc_value & PORT_ON_MASK)
                if port == MUTE_PORT:
                    if pressed:  # bascule à l'appui, pas au relâchement
                        muted = not self._mute_state.get(zone, False)
                        self._mute_state[zone] = muted
                        self._live_osc.set_track_mute(zone, muted)
                else:
                    self._log(f"HUI : zone={zone} port={port} pressed={pressed} (non géré pour l'instant)")
                return

        self._log(f"HUI : message non reconnu {[hex(b) for b in message]}")
