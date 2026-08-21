"""Pont HUI -> OSC : traduit les messages MIDI HUI reçus d'une console de
mixage (ex. Yamaha 01V96V2 en mode "HUI") en commandes OSC vers Ableton Live
(volume et mute des pistes). Un canal HUI = une piste Live, dans l'ordre
(tranche 1 -> piste 1, etc.). Sens unique pour l'instant : la console pilote
Live, Live ne renvoie rien vers la console (pas de LED/fader motorisé).

Protocole observé sur le 01V96V2 réel (relevé via MIDI Monitor, 2026-08-21 —
diffère de la doc HUI générique "theageman" sur plusieurs points) :
  - La console adresse un canal (0-7, une "tranche") avec CC 0x0F (zone),
    suivi d'un CC 0x2F (0x40 = enfoncé/actif, bits de poids faible = "port") :
      port 0 : fader/valeur continue en cours (précède une paire CC de valeur)
      port 2 : bouton Mute (0x40 posé = appui, retiré = relâché)
  - Fader : après le CC 0x2F "port 0", la position (14 bits) arrive en 2 CC
    sur le canal de la zone courante : CC(zone) = poids fort (0-127),
    CC(zone + 32) = poids faible (0-127) -> valeur = (fort << 7) | faible.
    Le 01V96V2 n'a que 8 tranches par port MIDI : les 16 voies sont donc
    réparties sur 2 ports (canaux 1-8 et 9-16), d'où `channel_offset`.
  - Ping : pour que la console continue à envoyer les faders en continu, il
    faut lui envoyer un ping (Note On canal 0, note 0, vélocité 0) toutes les
    ~1s ; sans ping elle arrête l'envoi des faders au bout de 2s (le mute
    n'est pas affecté). La console répond à chaque ping par un Note On note 0
    vélocité 127 (poignée de main) : c'est un écho normal, ignoré silencieusement.
  - Les mêmes ports MIDI (Port3/Port4) véhiculent aussi les Note On/Off des
    boutons +/-1, stop/play et navigation de scène : déjà traités par le
    système `controller_map` (menu "MIDI IN Contrôleur") sur son propre port,
    donc tout Note On/Off est ignoré ici sans journalisation.

Tout message non reconnu est journalisé (au lieu d'être ignoré silencieusement)
pour permettre d'affiner le mapping si besoin.
"""
from __future__ import annotations

import threading

import rtmidi

ZONE_CC = 0x0F
PORT_CC = 0x2F
PORT_ON_MASK = 0x40
PORT_NUMBER_MASK = 0x07
FADER_PORT = 0
MUTE_PORT = 2
PING_INTERVAL = 1.0  # secondes


class HuiBridge:
    """Ouvre un port MIDI IN (messages HUI) et, si possible, un port MIDI OUT
    du même nom (pour le ping), et traduit les événements reçus vers OSC.

    `channel_offset` décale les tranches (0-7) reçues sur ce port vers les
    pistes Live correspondantes — utile quand la console répartit ses voies
    sur plusieurs ports MIDI (ex. port A = voies 1-8, port B = voies 9-16)."""

    def __init__(self, live_osc, log=print, channel_offset: int = 0):
        self._live_osc = live_osc
        self._log = log
        self._channel_offset = channel_offset
        self._midi_in: rtmidi.MidiIn | None = None
        self._midi_out: rtmidi.MidiOut | None = None
        self._port_name: str | None = None
        self._pending_zone: int | None = None
        self._pending_coarse: int | None = None
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
        self._pending_zone = None
        self._pending_coarse = None

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
        self._pending_coarse = None

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
        status = message[0] & 0xF0
        if status in (0x80, 0x90):
            # Note On/Off : écho du ping, ou boutons +/-1, stop/play, navigation
            # de scène -- déjà traités ailleurs via controller_map. Ignoré ici.
            return
        if len(message) < 3 or status != 0xB0:
            self._log(f"HUI : message non reconnu {[hex(b) for b in message]}")
            return
        cc, value = message[1], message[2]

        if cc == ZONE_CC:
            self._pending_zone = value
            self._pending_coarse = None
            return

        if self._pending_zone is None:
            self._log(f"HUI : CC{cc}={value} reçu sans zone en attente")
            return
        zone = self._pending_zone
        track = zone + self._channel_offset

        if cc == PORT_CC:
            port = value & PORT_NUMBER_MASK
            pressed = bool(value & PORT_ON_MASK)
            if port == MUTE_PORT:
                if pressed:  # bascule à l'appui, pas au relâchement
                    muted = not self._mute_state.get(track, False)
                    self._mute_state[track] = muted
                    self._log(f"HUI : mute piste {track + 1} -> {muted}")
                    self._live_osc.set_track_mute(track, muted)
            elif port != FADER_PORT:
                self._log(f"HUI : zone={zone} port={port} pressed={pressed} (non géré pour l'instant)")
            return

        if cc == zone:  # poids fort (coarse) de la position du fader
            self._pending_coarse = value
            return

        if cc == zone + 32 and self._pending_coarse is not None:  # poids faible (fine)
            raw = (self._pending_coarse << 7) | value
            self._pending_coarse = None
            volume = raw / 16383.0
            self._log(f"HUI : volume piste {track + 1} -> {volume:.3f}")
            self._live_osc.set_track_volume(track, volume)
            return

        self._log(f"HUI : zone={zone} CC{cc}={value} (non géré pour l'instant)")
