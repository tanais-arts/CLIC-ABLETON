"""Pont HUI <-> OSC : traduit les messages MIDI HUI reçus d'une console de
mixage (ex. Yamaha 01V96V2 en mode "HUI") en commandes OSC vers Ableton Live
(volume et mute des pistes), et renvoie en retour vers la console la position
de fader / l'état mute réels d'Ableton (ex. si changés depuis la souris dans
Live). Encodage du retour confirmé par capture MIDI Monitor de Pro Tools
(implémentation HUI de référence) le 2026-08-21 : le fader utilise les mêmes
CC(zone)/CC(zone+32) que la réception, sans zone select ; le mute utilise le
même schéma zone/port que la réception mais décalé de -3 en numéro de CC
(0x0C/0x2C au lieu de 0x0F/0x2F).
Un canal HUI = une piste Live, dans l'ordre (tranche 1 -> piste 1, etc.), à
l'exception du canal 16 (dernière tranche du 2e port), réservé au contrôle du
tempo (voir `tempo_zone`/`on_tempo_fader` et beat_display.py) et donc absent
du mapping piste <-> tranche.

Protocole observé sur le 01V96V2 réel (relevé via MIDI Monitor, 2026-08-21 —
diffère de la doc HUI générique "theageman" sur plusieurs points) :
  - La console adresse un canal (0-7, une "tranche") avec CC 0x0F (zone),
    suivi d'un CC 0x2F (0x40 = enfoncé/actif, bits de poids faible = "port") :
      port 0 : fader/valeur continue en cours (précède une paire CC de valeur)
      port 2 : bouton Mute (0x40 posé = appui, retiré = relâché). Sur la
        tranche tempo (voir `tempo_zone`), ce n'est pas un mute mais un envoi
        simple (pas un bascule) qui rappelle le tempo à sa valeur de
        référence au prochain temps (voir `on_tempo_reset`/beat_display.py).
  - Fader : après le CC 0x2F "port 0", la position (14 bits) arrive en 2 CC
    sur le canal de la zone courante : CC(zone) = poids fort (0-127),
    CC(zone + 32) = poids faible (0-127) -> valeur = (fort << 7) | faible.
    Le 01V96V2 n'a que 8 tranches par port MIDI : les 16 voies sont donc
    réparties sur 2 ports (canaux 1-8 et 9-16), d'où le mapping zone <-> piste
    Live (`HuiBridge.set_mapping`), configurable par l'utilisateur (voir
    beat_display.py) plutôt qu'une simple correspondance directe 1 pour 1.
  - Ping : pour que la console continue à envoyer les faders en continu, il
    faut lui envoyer un ping (Note On canal 0, note 0, vélocité 0) toutes les
    ~1s ; sans ping elle arrête l'envoi des faders au bout de 2s (le mute
    n'est pas affecté). La console répond à chaque ping par un Note On note 0
    vélocité 127 (poignée de main) : c'est un écho normal, ignoré silencieusement.
  - Les mêmes ports MIDI (Port3/Port4) véhiculent aussi les Note On/Off des
    boutons +/-1, stop/play et navigation de scène (déjà traités ailleurs par
    le système `controller_map`, menu "MIDI IN OSC Boutons", sur son propre
    port) ainsi que d'autres touches de la console (ex. USER SEL) : ces Note
    On/Off sont ignorés ici (pas de traduction OSC) mais journalisés, sauf la
    poignée de main du ping (Note On canal 0 note 0) qui est du bruit attendu.

Tout message non reconnu est journalisé (au lieu d'être ignoré silencieusement)
pour permettre d'affiner le mapping si besoin.
"""
from __future__ import annotations

import threading
import time
import unicodedata

import rtmidi

ZONE_CC = 0x0F
PORT_CC = 0x2F
PORT_ON_MASK = 0x40
PORT_NUMBER_MASK = 0x07
FADER_PORT = 0
MUTE_PORT = 2
PING_INTERVAL = 1.0  # secondes
# Après un mouvement de fader local, on ignore les retours OSC qui ne
# confirment pas la valeur envoyée pendant cette durée : ce sont des échos
# en retard de positions intermédiaires déjà dépassées (voir send_volume_feedback).
VOLUME_ECHO_HOLDOFF_S = 0.6
# Une poussée physique "à fond" n'atteint pas toujours exactement raw=16383/0
# (jeu mécanique) : comme la courbe de volume de Live est très resserrée près
# de son maximum (+6dB), même 0,2% d'écart peut se traduire par plusieurs dB
# de moins. On arrondit donc à l'extrême exact en dessous de cette marge.
FADER_SNAP_MARGIN = 300

# Sens host -> surface (retour vers la console), confirmé par capture MIDI
# Monitor de Pro Tools (référence HUI) : même schéma zone/port mais décalé de
# -3 par rapport au sens surface -> host ci-dessus. La valeur de fader
# (CC(zone)/CC(zone+32)) n'a elle pas besoin de zone select, elle s'auto-
# identifie par son numéro de CC.
ZONE_CC_OUT = 0x0C
PORT_CC_OUT = 0x2C


def _to_hui_ascii(text: str, length: int) -> bytes:
    """Convertit vers l'ASCII 7 bits attendu par l'afficheur HUI (accents
    retirés plutôt que remplacés), tronqué/complété à `length` caractères."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_text[:length].ljust(length).encode("ascii")


class HuiBridge:
    """Ouvre un port MIDI IN (messages HUI) et, si possible, un port MIDI OUT
    du même nom (pour le ping), et traduit les événements reçus vers OSC.

    `zone_to_track` associe chaque tranche physique (0-7 sur ce port MIDI) à
    une piste Live — utile quand la console répartit ses voies sur plusieurs
    ports MIDI (ex. port A = tranches 0-7, port B = tranches 0-7 aussi mais
    pour d'autres pistes), et pour permettre un mapping arbitraire choisi par
    l'utilisateur plutôt qu'une simple correspondance 1 pour 1.

    `tempo_zone`/`on_tempo_fader` réservent une tranche (ex. le fader 16) au
    contrôle du tempo au lieu du volume d'une piste : sa position brute
    (0-16383) est transmise à `on_tempo_fader` au lieu de suivre le chemin
    volume/OSC habituel (voir beat_display.py). `on_tempo_reset` (sans
    argument) est appelé pour cette même tranche à CHAQUE message Mute reçu,
    sans condition sur la valeur (la console n'envoie pas une paire
    appui/relâchement fiable sur ce bouton précis, confirmé sur matériel
    réel : un filtre par changement de valeur en ratait un sur deux) pour
    redemander le rappel du tempo d'origine (voir beat_display.py, où la
    reprogrammation est idempotente donc sans risque en cas de déclenchement
    en trop)."""

    def __init__(
        self, live_osc, log=print, zone_to_track: dict[int, int] | None = None,
        tempo_zone: int | None = None, on_tempo_fader=None, on_tempo_reset=None,
    ):
        self._live_osc = live_osc
        self._log = log
        self._tempo_zone = tempo_zone
        self._on_tempo_fader = on_tempo_fader
        self._on_tempo_reset = on_tempo_reset
        self._midi_in: rtmidi.MidiIn | None = None
        self._midi_out: rtmidi.MidiOut | None = None
        self._port_name: str | None = None
        self._pending_zone: int | None = None
        self._pending_coarse: int | None = None
        self._mute_toggle_state: dict[int, bool] = {}
        self._mute_sent_state: dict[int, bool] = {}
        self._track_volume_local: dict[int, float] = {}
        self._track_volume_local_time: dict[int, float] = {}
        self._track_volume_sent: dict[int, float] = {}
        self._track_name: dict[int, bytes] = {}
        self._ping_stop = threading.Event()
        self._ping_thread: threading.Thread | None = None
        self._zone_to_track: dict[int, int] = {}
        self._track_to_zone: dict[int, int] = {}
        self.set_mapping(zone_to_track if zone_to_track is not None else {z: z for z in range(8)})

    @staticmethod
    def list_ports() -> list[str]:
        probe = rtmidi.MidiIn()
        names = probe.get_ports()
        del probe
        return names

    def set_mapping(self, zone_to_track: dict[int, int]) -> None:
        """Remplace la correspondance tranche HUI (0-7 sur ce port) <-> piste
        Live, ex. quand l'utilisateur change le mapping des faders dans
        l'interface. Purge les états locaux indexés par piste : sinon ils
        resteraient associés aux anciennes pistes et fausseraient la
        détection d'écho/dédoublonnage."""
        self._zone_to_track = dict(zone_to_track)
        self._track_to_zone = {track: zone for zone, track in self._zone_to_track.items()}
        self._pending_zone = None
        self._pending_coarse = None
        self._mute_toggle_state.clear()
        self._mute_sent_state.clear()
        self._track_volume_local.clear()
        self._track_volume_local_time.clear()
        self._track_volume_sent.clear()
        self._track_name.clear()

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
            channel, note, velocity = message[0] & 0x0F, message[1], message[2] if len(message) > 2 else 0
            if note == 0 and channel == 0:
                # Poignée de main du ping (Note On canal 0 note 0) : bruit attendu, pas de log.
                return
            # Boutons +/-1, stop/play, navigation de scène, USER SEL, etc. --
            # déjà traités ailleurs (controller_map) si mappés ; journalisé ici
            # aussi pour repérer les notes non encore mappées sur ce port.
            self._log(
                f"HUI : Note {'On' if status == 0x90 else 'Off'} canal={channel + 1} note={note} "
                f"vélocité={velocity} (ignoré ici, voir MIDI IN OSC Boutons)"
            )
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
        track = self._zone_to_track.get(zone)

        if cc == PORT_CC:
            port = value & PORT_NUMBER_MASK
            pressed = bool(value & PORT_ON_MASK)
            if port == MUTE_PORT:
                if zone == self._tempo_zone:
                    # Pas un vrai appui/relâchement fiable sur ce bouton précis
                    # (confirmé sur matériel réel : la valeur ne bascule pas de
                    # façon prévisible à chaque appui, un filtre par changement
                    # de valeur en ratait un sur deux). On déclenche donc sur
                    # CHAQUE message reçu pour cette tranche, sans condition :
                    # sans risque ici, un déclenchement en trop ne fait que
                    # reprogrammer le même rappel (voir _schedule_tempo_reset
                    # dans beat_display.py, idempotent).
                    self._log(f"HUI : mute fader tempo (valeur={value}) -> rappel du tempo d'origine au prochain temps")
                    if self._on_tempo_reset is not None:
                        self._on_tempo_reset()
                elif pressed and track is not None:  # bascule à l'appui, pas au relâchement
                    muted = not self._mute_toggle_state.get(track, False)
                    self._mute_toggle_state[track] = muted
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
            if raw >= 16383 - FADER_SNAP_MARGIN:
                raw = 16383
            elif raw <= FADER_SNAP_MARGIN:
                raw = 0
            if zone == self._tempo_zone:
                if self._on_tempo_fader is not None:
                    self._on_tempo_fader(raw)
                return
            volume = raw / 16383.0
            if track is None:
                self._log(f"HUI : zone {zone} sans piste Live assignée (volume {volume:.3f} ignoré)")
                return
            self._log(f"HUI : volume piste {track + 1} -> {volume:.3f}")
            self._track_volume_local[track] = volume
            self._track_volume_local_time[track] = time.monotonic()
            self._live_osc.set_track_volume(track, volume)
            return

        self._log(f"HUI : zone={zone} CC{cc}={value} (non géré pour l'instant)")

    def send_volume_feedback(self, track_index: int, volume: float) -> None:
        """Renvoie vers la console la position de fader réelle d'Ableton pour
        `track_index` (ignoré si hors de la plage de ce port). Juste après un
        mouvement local, d'anciens retours OSC intermédiaires en retard
        peuvent encore arriver en rafale : les appliquer ferait reculer le
        fader physique vers une position dépassée sans toucher au vrai volume
        dans Live (déjà bon). On les ignore pendant VOLUME_ECHO_HOLDOFF_S,
        sauf s'ils confirment justement la valeur qu'on vient d'envoyer."""
        zone = self._track_to_zone.get(track_index)
        if self._midi_out is None or zone is None:
            return
        is_echo = abs(volume - self._track_volume_local.get(track_index, -1.0)) < 1 / 16383.0
        recent_local_move = (
            time.monotonic() - self._track_volume_local_time.get(track_index, 0.0)
        ) < VOLUME_ECHO_HOLDOFF_S
        if not is_echo and recent_local_move:
            return
        if abs(volume - self._track_volume_sent.get(track_index, -1.0)) < 1 / 16383.0:
            return
        self._track_volume_sent[track_index] = volume
        raw = max(0, min(16383, round(volume * 16383)))
        coarse, fine = raw >> 7, raw & 0x7F
        self._midi_out.send_message([0xB0, zone, coarse])
        self._midi_out.send_message([0xB0, zone + 32, fine])

    def send_tempo_fader_feedback(self, raw: int) -> None:
        """Positionne la tranche tempo (`tempo_zone`) à la valeur brute donnée
        (0-16383). N'est appelée qu'une fois par lancement de scène (voir
        beat_display.py), jamais en boucle : pas de rappel/keepalive."""
        if self._midi_out is None or self._tempo_zone is None:
            return
        raw = max(0, min(16383, raw))
        coarse, fine = raw >> 7, raw & 0x7F
        self._midi_out.send_message([0xB0, self._tempo_zone, coarse])
        self._midi_out.send_message([0xB0, self._tempo_zone + 32, fine])

    def send_mute_feedback(self, track_index: int, muted: bool) -> None:
        """Renvoie vers la console l'état mute réel d'Ableton pour
        `track_index` (ignoré si hors de la plage de ce port). Utilise un
        dict distinct de celui du basculement local (`_mute_toggle_state`) :
        sinon un appui sur le bouton de la console pré-remplissait la même
        valeur avant même le retour d'Ableton, et le message MIDI réel
        (celui qui allume/éteint la LED) n'était jamais envoyé."""
        zone = self._track_to_zone.get(track_index)
        if self._midi_out is None or zone is None:
            return
        self._mute_toggle_state[track_index] = muted
        if self._mute_sent_state.get(track_index) == muted:
            return
        self._mute_sent_state[track_index] = muted
        value = MUTE_PORT | (PORT_ON_MASK if muted else 0)
        self._midi_out.send_message([0xB0, ZONE_CC_OUT, zone])
        self._midi_out.send_message([0xB0, PORT_CC_OUT, value])

    def send_name_feedback(self, track_index: int, name: str) -> None:
        """Envoie le nom (abrégé à 4 caractères) de la piste vers l'afficheur
        de la tranche correspondante (SysEx confirmé par capture Pro Tools :
        F0 00 00 66 05 00 10 <zone> <4 car. ASCII> F7)."""
        zone = self._track_to_zone.get(track_index)
        if self._midi_out is None or zone is None:
            return
        text = _to_hui_ascii(name, 4)
        if self._track_name.get(track_index) == text:
            return
        self._track_name[track_index] = text
        self._midi_out.send_message([0xF0, 0x00, 0x00, 0x66, 0x05, 0x00, 0x10, zone, *text, 0xF7])
