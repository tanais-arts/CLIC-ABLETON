#!/usr/bin/env python3
"""Affichage du temps (1-4) d'Ableton Live pour un batteur.

Deux sources possibles, sélectionnables dans l'appli :
  - Ableton Link (recommandé) : toujours aligné sur le vrai temps 1 de Live,
    même si on se connecte en cours de lecture.
  - MIDI Clock : pour les logiciels qui n'ont pas Link (Start/Stop/Continue/
    Song Position Pointer/Clock 24 ppqn) via un port MIDI virtuel.

Une page web locale (voir web_server.py) permet aussi d'afficher le même
compteur sur un smartphone connecté au même réseau. Voir README.md.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont, ttk

import rtmidi

from audio_metronome import AudioMetronome, list_kits, list_output_devices
from config import load_config, save_config
from hui_bridge import HuiBridge
from link_client import AbletonLink, LinkUnavailable
from live_osc import LiveOSC
from lyrics import LyricsSheet, load_lyrics
from scene_sheet import SceneSheet, SceneSheetRow, load_scene_sheet
from web_server import BeatWebServer, SharedBeatState, project_phase

CLOCK = 0xF8
START = 0xFA
CONTINUE = 0xFB
STOP = 0xFC
SONG_POSITION = 0xF2
TICKS_PER_QUARTER = 24

BG_IDLE = "#1e1e1e"
FLASH_YELLOW = "#f5c518"
FLASH_BLUE = "#2b4bff"  # bleu outremer
FLASH_HIGHLIGHT = "#ff2b2b"  # fond ET chiffres/dots : mesure HIGHLIGHT (scene_sheet.py)
HIGHLIGHT_SIZE_SCALE = 1.5  # chiffres/dots 50% plus grands sur une mesure HIGHLIGHT
LYRICS_SCROLL_BEATS = 48  # nombre de temps pour traverser toute la zone de paroles
LYRICS_BEATS_PER_LINE = 8  # chaque ligne du CSV mesure exactement 8 temps (positionnement du défilement)
LYRICS_FONT = ("Helvetica", 25, "bold")  # titre du morceau (20) + 25%
# Position de lecture (calage du défilement), fraction (0..1) depuis le haut
# de la zone de paroles : valeur calibrée manuellement, figée.
LYRICS_READING_POSITION_RATIO = 0.28
SCENE_NOT_LAUNCHED = "#ff4d4d"  # rouge : scène sélectionnée, pas encore lancée (identique au web)
SCENE_LAUNCHED = "#3ddc57"  # vert : scène lancée
SCENE_FLASH_WHITE = "#ffffff"
SCENE_FLASH_PULSE = 0.15  # secondes par flash
SCENE_FLASH_GAP = 0.1  # secondes entre les deux flashs
FG_TEXT = "#f5f5f5"


def _lerp_color(start: str, end: str, t: float) -> str:
    """Interpole entre deux couleurs hexa (#rrggbb) ; t clampé à [0, 1]."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(start[1:3], 16), int(start[3:5], 16), int(start[5:7], 16)
    r2, g2, b2 = int(end[1:3], 16), int(end[3:5], 16), int(end[5:7], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


class ClockListener:
    """Lit les messages MIDI Clock dans un thread rtmidi et les pousse dans une queue."""

    def __init__(self, event_queue: "queue.Queue[tuple]"):
        self._queue = event_queue
        self._midi_in: rtmidi.MidiIn | None = None
        self._port_name: str | None = None

    def list_ports(self) -> list[str]:
        probe = rtmidi.MidiIn()
        names = probe.get_ports()
        del probe
        return names

    def connect(self, port_name: str) -> None:
        self.close()
        midi_in = rtmidi.MidiIn()
        names = midi_in.get_ports()
        index = names.index(port_name)
        midi_in.open_port(index)
        # Ne pas ignorer les messages temps réel (Clock/Start/Stop/SPP).
        midi_in.ignore_types(sysex=True, timing=False, active_sense=True)
        midi_in.set_callback(self._callback)
        self._midi_in = midi_in
        self._port_name = port_name

    def close(self) -> None:
        if self._midi_in is not None:
            self._midi_in.close_port()
            self._midi_in = None
            self._port_name = None

    @property
    def port_name(self) -> str | None:
        return self._port_name

    def _callback(self, event, _data=None) -> None:
        message, delta_time = event
        self._queue.put((message, delta_time, time.perf_counter()))


def _describe_controller_message(message: list[int], key: tuple[str, int, int] | None) -> str:
    """Description lisible d'un message reçu du contrôleur MIDI (log terminal,
    pour identifier quel bouton/encodeur physique correspond à quel CC/note)."""
    if key is None:
        return f"message brut non reconnu : {message}"
    kind, channel, number = key
    kind_label = "Note" if kind == "note" else "CC"
    value = message[2] if len(message) > 2 else "?"
    return f"{kind_label} {number} (canal {channel + 1}), valeur={value}"


def _controller_key(message: list[int]) -> tuple[str, int, int] | None:
    """Identifiant (type, canal, numéro) d'un message Note On / Control Change,
    utilisé pour l'apprentissage MIDI d'un bouton de contrôleur."""
    if len(message) < 2:
        return None
    status = message[0]
    kind = status & 0xF0
    channel = status & 0x0F
    if kind == 0x90:
        return ("note", channel, message[1])
    if kind == 0xB0:
        return ("cc", channel, message[1])
    return None


def _controller_label(key: tuple[str, int, int] | None) -> str:
    if key is None:
        return "non assigné"
    kind, channel, number = key
    kind_label = "Note" if kind == "note" else "CC"
    return f"{kind_label} {number} (canal {channel + 1})"


def _as_key(value) -> tuple[str, int, int] | None:
    """Reconstruit un tuple (type, canal, numéro) depuis la config JSON (liste)."""
    if not value:
        return None
    kind, channel, number = value
    return (kind, int(channel), int(number))


class ControllerListener:
    """Lit les messages Note On / Control Change d'un contrôleur MIDI (ex.
    Behringer BCF2000), pour déclencher des actions (boutons -1/+1)."""

    def __init__(self, event_queue: "queue.Queue[list[int]]"):
        self._queue = event_queue
        self._midi_in: rtmidi.MidiIn | None = None
        self._midi_out: rtmidi.MidiOut | None = None
        self._port_name: str | None = None

    def list_ports(self) -> list[str]:
        probe = rtmidi.MidiIn()
        names = probe.get_ports()
        del probe
        return names

    def connect(self, port_name: str) -> None:
        self.close()
        midi_in = rtmidi.MidiIn()
        names = midi_in.get_ports()
        index = names.index(port_name)
        midi_in.open_port(index)
        midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        midi_in.set_callback(self._callback)
        self._midi_in = midi_in
        self._port_name = port_name

        # Port de sortie du même nom, pour renvoyer l'état (LED) des touches
        # USER DEFINED apprises (voir send_note_feedback) — absent si la
        # console n'expose pas ce port en sortie (pas bloquant, juste pas de LED).
        try:
            midi_out = rtmidi.MidiOut()
            out_names = midi_out.get_ports()
            midi_out.open_port(out_names.index(port_name))
            self._midi_out = midi_out
            print(f"[Contrôleur MIDI] port MIDI OUT « {port_name} » ouvert (LED USER DEFINED possibles).")
        except ValueError:
            self._midi_out = None
            print(
                f"[Contrôleur MIDI] pas de port MIDI OUT « {port_name} » : "
                "pas de retour LED possible sur les touches USER DEFINED."
            )

    def close(self) -> None:
        if self._midi_in is not None:
            self._midi_in.close_port()
            self._midi_in = None
            self._port_name = None
        if self._midi_out is not None:
            self._midi_out.close_port()
            self._midi_out = None

    @property
    def port_name(self) -> str | None:
        return self._port_name

    def send_note_feedback(self, channel: int, note: int, on: bool) -> None:
        """Allume/éteint la LED d'une touche USER DEFINED en renvoyant un Note
        On/Note Off sur la même note/canal, sur ce même port MIDI — la
        console boucle l'état de la LED sur ce qu'elle reçoit."""
        if self._midi_out is None:
            return
        print(f"[Contrôleur MIDI] LED -> canal={channel + 1} note={note} on={on}")
        # Note Off explicite (statut 0x80), pas juste un Note On vélocité 0 :
        # certains appareils ne traitent pas les deux de la même façon.
        status = 0x90 if on else 0x80
        velocity = 127 if on else 0
        self._midi_out.send_message([status | (channel & 0x0F), note & 0x7F, velocity])

    def _callback(self, event, _data=None) -> None:
        message, _delta_time = event
        self._queue.put(message)


class BeatState:
    """Calcule le temps courant (1..N) et le BPM à partir des messages MIDI reçus."""

    def __init__(self, beats_per_bar: int = 4):
        self.beats_per_bar = beats_per_bar
        self.running = False
        self.beat_in_bar = 1
        self.bpm: float | None = None
        self._ticks_since_quarter = 0
        self._quarter_count = 0
        self._quarter_time_accum = 0.0

    def reset_position(self) -> None:
        self._ticks_since_quarter = 0
        self._quarter_count = 0
        self._quarter_time_accum = 0.0
        self.beat_in_bar = 1

    def phase(self) -> float:
        """Position continue dans la mesure (0..beats_per_bar), pour la page web."""
        fractional = self._ticks_since_quarter / TICKS_PER_QUARTER
        return (self._quarter_count % self.beats_per_bar) + fractional

    def handle_message(self, message: list[int], delta_time: float) -> bool:
        """Traite un message MIDI. Retourne True si l'affichage doit être rafraîchi."""
        status = message[0]

        if status == START:
            self.reset_position()
            self.running = True
            return True

        if status == CONTINUE:
            self.running = True
            return True

        if status == STOP:
            self.running = False
            return True

        if status == SONG_POSITION and len(message) >= 3:
            lsb, msb = message[1], message[2]
            sixteenths = (msb << 7) | lsb
            self._quarter_count = sixteenths // 4
            self._ticks_since_quarter = 0
            self._quarter_time_accum = 0.0
            self.beat_in_bar = (self._quarter_count % self.beats_per_bar) + 1
            return True

        if status == CLOCK:
            # Live n'envoie le clock que pendant la lecture : son arrivée
            # suffit à prouver que le transport tourne, même si aucun
            # message Start n'a été reçu (ex. Live jouait déjà avant la
            # connexion du port MIDI).
            self.running = True
            self._quarter_time_accum += delta_time
            self._ticks_since_quarter += 1
            if self._ticks_since_quarter >= TICKS_PER_QUARTER:
                self._ticks_since_quarter = 0
                if self._quarter_time_accum > 0:
                    self.bpm = 60.0 / self._quarter_time_accum
                self._quarter_time_accum = 0.0
                self.beat_in_bar = (self._quarter_count % self.beats_per_bar) + 1
                self._quarter_count += 1
                return True
            return False

        return False


class App:
    # Dossier où chercher <nom de scène>.xlsx (voir scene_sheet.py) : à côté du
    # script, comme le fichier d'exemple Viser.xlsx.
    SCENE_SHEET_DIR = Path(__file__).resolve().parent
    # Tranche HUI (0-7) réservée au tempo sur le 2e port MIDI (channel_offset=8),
    # donc canal physique 16 = 8 + 7 + 1. Voir _apply_tempo_fader.
    TEMPO_FADER_ZONE = 7
    # Position brute (0-16383) du fader 16 correspondant à 0% de modification
    # du tempo (centre de course) : 0 = -X%, 16383 = +X%, X selon la plage
    # choisie (Plage fader 16). Envoyée une fois au lancement de chaque scène.
    TEMPO_FADER_CENTER_RAW = 8192
    # Faders HUI motorisés : sans confirmation périodique de sa position, la
    # console y ramène le fader tout seul (moteur), sans changer le tempo
    # (voir _poll_tempo_fader_keepalive). On laisse la main de l'utilisateur
    # gagner pendant HOLDOFF_S après son dernier geste, puis on renvoie la
    # position au tempo affiché toutes les INTERVAL_S.
    TEMPO_FADER_KEEPALIVE_HOLDOFF_S = 1.5
    TEMPO_FADER_KEEPALIVE_INTERVAL_S = 1.0
    # Métronome audio local : libellé affiché pour "périphérique par défaut
    # du système" (config.json stocke "" dans ce cas, voir audio_metronome.py).
    METRONOME_DEFAULT_DEVICE_LABEL = "Par défaut (système)"
    # Options de plage du fader 16 : (libellé affiché, clé persistée en config).
    TEMPO_RANGE_OPTIONS = [
        ("± 3 %", "3"), ("± 6 %", "6"), ("± 10 %", "10"), ("± 20 %", "20"),
        ("± 100 %", "100"),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Compteur de temps - Ableton Live")
        self.root.configure(bg=BG_IDLE)
        # Taille/position fixées au lancement pour se caler à gauche de
        # l'écran, à côté de la fenêtre Live (voir capture de référence).
        # Les réglages AUDIO/MIDI vivent dans des fenêtres à part (voir
        # _make_settings_window) : la fenêtre principale n'a donc plus de
        # contenu qui grandit/rétrécit après coup, hors cette taille de départ.
        self.root.geometry("710x1050+0+0")
        self.root.minsize(520, 420)

        self.config = load_config()
        # Dernières valeurs valides de beats_var/latency_var (Spinbox) : leur
        # IntVar.get() lève TclError le temps où le champ est vidé pendant la
        # frappe, ce qui plantait _poll() et gelait l'affichage/le métronome
        # jusqu'à l'appui suivant (voir _safe_int_var).
        self._beats_per_bar_cache: int = self.config["beats_per_bar"]
        self._latency_ms_cache: int = self.config["latency_ms"]

        # -- Source MIDI (repli pour les logiciels sans Link) --
        self._event_queue: "queue.Queue[tuple]" = queue.Queue()
        self.listener = ClockListener(self._event_queue)
        self.midi_state = BeatState(beats_per_bar=self.config["beats_per_bar"])

        # -- Source Ableton Link (recommandée) --
        self.link: AbletonLink | None = None
        self.link_error: str | None = None

        # -- Page web locale pour smartphone --
        self.shared_state = SharedBeatState()
        self.web_server = BeatWebServer(self.shared_state, port=self.config["web_port"])
        self.web_server.start()

        # -- Décalage à la volée du playhead de Live (rattrapage faux départ) --
        self.live_osc = LiveOSC()

        # -- Navigation dans la colonne des scènes de Live (nom + précédente/suivante) --
        self._scene_index: int | None = None
        self._scene_count: int | None = None
        self._scene_name: str = ""
        # Tempo d'origine du morceau en cours, lu dans le nom de la scène qui
        # précède la scène du morceau (convention du set : ex. morceau "OVLM"
        # précédé d'une scène nommée "100") — voir _poll_scene_replies (peek
        # arrière) et le rappel du fader 16 (bouton Mute, _reset_tempo_fader).
        self._scene_origin_tempo: float | None = None

        # -- Métronome audio local (clic .wav, voir audio_metronome.py) : ne
        # commande plus le métronome interne de Live (sa signature globale
        # ne correspond pas à notre feuille de scène à mesures variables). --
        self._metronome_on: bool = False
        self._last_played_beat: int | None = None
        self._metronome_on_2: bool = False
        # Coupe silencieusement le clic quand le LABEL "END" (feuille de
        # scène) apparaît, jusqu'au prochain Stop (_stop_return_to_start) :
        # ne touche ni _metronome_on/_metronome_on_2 (état des boutons M1/M2)
        # ni leurs LED, seulement la décision de jouer le clic.
        self._metronome_end_muted: bool = False
        self._audio_metronome = AudioMetronome()
        self._audio_metronome.set_kit(self.config["metronome_kit"])
        self._audio_metronome.configure(
            self.config["metronome_audio_device"], self.config["metronome_audio_channels"],
        )
        # Deuxième sortie (voir _build_ui metronome_frame_2) : jouée en
        # parallèle de la première quand activée, propre carte son/kit/latence.
        self._audio_metronome_2 = AudioMetronome()
        self._audio_metronome_2.set_kit(self.config["metronome_kit_2"])
        self._audio_metronome_2.configure(
            self.config["metronome_audio_device_2"], self.config["metronome_audio_channels_2"],
        )
        # Cache non-Tkinter du mode courant, lu par _metronome_loop (thread
        # séparé : lire un tk.StringVar hors du thread Tk n'est pas sûr).
        self._mode_cache: str = self.config["mode"]
        self._metronome_latency_ms_cache: int = self.config["metronome_audio_latency_ms"]
        self._metronome_latency_ms_cache_2: int = self.config["metronome_audio_latency_ms_2"]
        self._last_played_beat_2: int | None = None
        # Déclenche les clics indépendamment de _poll()/after() : sur macOS,
        # bouger la souris sur la fenêtre peut geler les timers Tcl "after()"
        # (limitation connue de Tk/Cocoa), ce qui rendait aussi bien le clic
        # que l'affichage irréguliers puisque tout passait par _poll(). Ce
        # thread ne touche à aucun widget Tk (voir _metronome_loop).
        self._metronome_thread_stop = threading.Event()
        self._metronome_thread = threading.Thread(target=self._metronome_loop, daemon=True)
        # Comptage continu du temps DANS la mesure pour chaque sortie
        # métronome (même logique que self._link_beat_in_bar pour
        # l'affichage, voir _poll) : indispensable dès qu'une feuille de
        # scène change le COUNT d'une mesure à l'autre, sinon phase % quantum
        # (mesuré depuis le tout début de la session Link) décale
        # durablement le clic accentué (click_up) après la mesure irrégulière.
        self._metronome_beat_in_bar = 1
        self._metronome_prev_fractional: float | None = None
        self._metronome_last_update_time: float | None = None
        # Comme _awaiting_downbeat pour l'affichage : True tant que le vrai
        # temps 1 (phase % quantum) n'a pas encore été observé depuis la
        # (re)connexion, pour ne jamais forcer le clic sur un temps 1 qui ne
        # correspond pas à la position réelle de Link (voir _metronome_next_beat).
        self._metronome_awaiting_downbeat = True
        self._metronome_beat_in_bar_2 = 1
        self._metronome_prev_fractional_2: float | None = None
        self._metronome_last_update_time_2: float | None = None
        self._metronome_awaiting_downbeat_2 = True
        # -- Mode Offline (test sans Live/MIDI) --
        self._offline_playing: bool = False
        self._offline_beat_in_bar: int = 1
        self._offline_last_beat_time: float = 0.0
        # Confirmation réelle (pas juste l'instant d'envoi) de la prise en
        # compte de la signature par Live, voir _poll_scene_replies.
        self.live_osc.start_listen_signature_numerator()
        self.live_osc.start_listen_signature_denominator()

        # -- Contrôleur MIDI (ex. Behringer BCF2000) pour les 6 boutons (nudge,
        # navigation scènes, stop, lancer la scène) --
        self._controller_queue: "queue.Queue[list[int]]" = queue.Queue()
        self.controller = ControllerListener(self._controller_queue)
        self._action_order = ["minus", "plus", "scene_prev", "scene_next", "stop", "play", "metronome", "metronome_2"]
        self._action_labels = {
            "minus": "−1", "plus": "+1", "scene_prev": "▲", "scene_next": "▼", "stop": "■", "play": "▶",
            "metronome": "M1", "metronome_2": "M2",
        }
        self._action_commands = {
            "minus": lambda: self._jump_beats(-1),
            "plus": lambda: self._jump_beats(1),
            "scene_prev": lambda: self._scene_step(-1),
            "scene_next": lambda: self._scene_step(1),
            "stop": self._stop_return_to_start,
            "play": self._scene_launch,
            "metronome": self._toggle_metronome,
            "metronome_2": self._toggle_metronome_2,
        }
        self.controller_map: dict[str, tuple[str, int, int] | None] = {
            action: _as_key(self.config.get(f"controller_map_{action}"))
            for action in self._action_order
        }
        self._learning: str | None = None
        # Anti-rebond : la console peut renvoyer une même touche (Note On/Off)
        # plusieurs fois d'affilée sans appui réel (observé juste après un
        # changement de signature rythmique), ce qui déclenchait l'action
        # correspondante (ex. stop) plusieurs fois de suite.
        self._controller_last_fire: dict[str, float] = {}
        self.CONTROLLER_DEBOUNCE_S = 0.25

        # -- Pont HUI -> OSC (ex. Yamaha 01V96V2) pour faders/mutes des pistes --
        # La console répartit ses 16 voies sur 2 ports MIDI (8 tranches chacun).
        # Mapping piste Live <-> tranche HUI choisi par l'utilisateur (persisté,
        # diagonale 1<->1 par défaut) : voir _open_hui_mapping_dialog. Le
        # fader 16 (tranche 7 du 2e port) est réservé au tempo (voir
        # TEMPO_FADER_ZONE/_apply_tempo_fader), donc exclu du mapping/clampé.
        self._track_mapping: list[int] = list(self.config.get("hui_track_mapping", list(range(16))))
        if len(self._track_mapping) != 16:
            self._track_mapping = list(range(16))
        self._track_mapping = [min(v, 14) for v in self._track_mapping]
        zone_map_1, zone_map_2 = self._hui_zone_maps(self._track_mapping)
        self.hui_bridge = HuiBridge(self.live_osc, log=lambda msg: print(f"[HUI] {msg}"), zone_to_track=zone_map_1)
        # File des positions brutes (0-16383) du fader 16, remplie depuis le
        # thread MIDI de hui_bridge_2, consommée dans _poll (thread Tk) via
        # _poll_tempo_fader — jamais de widget Tk touché hors du thread principal.
        self._tempo_fader_queue: "queue.Queue[int]" = queue.Queue()
        # Envoi simple (pas un bascule) du bouton Mute de la tranche tempo :
        # demande de rappel du tempo d'origine du morceau, appliquée au
        # prochain temps (voir _poll_tempo_reset/_schedule_tempo_reset).
        self._tempo_reset_queue: "queue.Queue[None]" = queue.Queue()
        self._tempo_reset_after_id: str | None = None
        self.hui_bridge_2 = HuiBridge(
            self.live_osc, log=lambda msg: print(f"[HUI] {msg}"), zone_to_track=zone_map_2,
            tempo_zone=self.TEMPO_FADER_ZONE, on_tempo_fader=self._tempo_fader_queue.put,
            on_tempo_reset=lambda: self._tempo_reset_queue.put(None),
        )
        # Tempo de référence ("morceau chargé sans modification", position
        # centrale du fader 16) : voir _on_link_tempo_observed/_apply_tempo_fader.
        self._tempo_reference_bpm: float | None = None
        # Dernier BPM que NOUS avons envoyé à Link (spinbox ou fader 16) :
        # permet de distinguer un écho de notre propre envoi d'un vrai
        # changement externe (autre pair Link, ex. Live), voir
        # _on_link_tempo_observed/_update_tempo_display.
        self._tempo_last_sent_bpm: float | None = None
        # Horodatages pour _poll_tempo_fader_keepalive : dernier geste de la
        # main sur le fader 16 (holdoff) et dernier renvoi de sa position.
        self._tempo_fader_local_time = 0.0
        self._tempo_fader_refresh_time = 0.0
        # Dernière position brute envoyée au fader 16 (None = jamais encore) :
        # permet de renvoyer tout de suite un vrai changement de position,
        # sans attendre TEMPO_FADER_KEEPALIVE_INTERVAL_S (voir même fonction).
        self._tempo_fader_last_raw_sent: int | None = None

        # À la reprise (connecté/en lecture après ne pas l'avoir été), on
        # n'affiche les temps qu'à partir du prochain temps 1 réel, pour ne
        # pas commencer au milieu d'une mesure.
        self._was_connected = False
        self._awaiting_downbeat = False
        # Comptage du temps DANS la mesure, en mode Link, une fois le premier
        # vrai temps 1 détecté (voir _poll) : incrémenté/rebouclé nous-mêmes
        # sur self.beats_var (COUNT courant), PAS par un modulo de la phase
        # Link brute (qui suppose une mesure de longueur constante depuis le
        # début de la session Link — faux dès qu'une feuille de scène change
        # le COUNT d'une mesure à l'autre, ex. 2 temps puis 4 temps).
        self._link_beat_in_bar = 1
        self._link_prev_fractional: float | None = None
        # Horodatage (monotonic) associé à _link_prev_fractional : sert à
        # calculer le nombre RÉEL de temps écoulés depuis la dernière mise à
        # jour (voir _poll, branche "comptage en continu") plutôt que de se
        # contenter de détecter un seul rebouclage par appel, ce qui faisait
        # durablement prendre du retard au compteur après un gel de _poll()
        # (déplacement de souris sur macOS/Tk, voir _metronome_loop).
        self._link_last_update_time: float | None = None
        # Avertissement "0 pair Link" (voir _update_link_peers_label) : une
        # boîte de dialogue s'ouvre après 5s sans pair, et se referme toute
        # seule dès qu'un pair est détecté (ou reste fermée si l'utilisateur
        # l'a fermée manuellement entretemps, jusqu'à la prochaine coupure).
        self._link_zero_peers_since: float | None = None
        self._link_dialog: tk.Toplevel | None = None
        self._link_dialog_shown = False
        # Dernier tempo connu, pour animer la ligne de défilement à l'arrêt
        # (même quand la source ne fournit plus de temps courant fiable).
        self._last_bpm: float | None = None
        # Flash blanc ponctuel du canvas au lancement d'une scène.
        self._scene_flash_start: float = 0.0
        # Index de la scène pour laquelle le flash a déjà été montré : évite
        # de reflasher si on relance la même scène après un Stop (le flash ne
        # doit marquer que la révélation d'une scène pas encore vue).
        self._scene_flash_shown_for: int | None = None
        # Compteur de mesures depuis le lancement du morceau en cours (voir
        # _scene_launch/_update_bar_count) : None = pas de comptage affiché.
        # Les scènes "tempo seul" (chiffres) ne déclenchent jamais ce compte.
        self._bar_count: int | None = None
        self._awaiting_bar_start = False
        self._bar_count_prev_beat: int | None = None
        self._bar_count_signature_pushed_for: int | None = None
        # Numéro de mesure de départ ("GOTO Label" + "PREROLL", voir
        # goto_label_var/preroll_var) pour le prochain lancement de scène —
        # 1 par défaut (début du morceau).
        self._bar_count_start = 1
        # Saut GOTO en attente (nombre de temps), exécuté au premier vrai
        # temps 1 détecté (voir _update_bar_count) plutôt qu'après un délai
        # fixe : la quantification de lancement de Live (aucune, 1 mesure...)
        # peut retarder le vrai démarrage du clip de façon imprévisible.
        self._pending_goto_jump_beats: int | None = None
        # Feuille de scène chargée en prévisualisation dès la SÉLECTION d'une
        # scène (pas son lancement) pour peupler le sélecteur GOTO, voir
        # _refresh_goto_labels — distincte de self._scene_sheet (celle-ci
        # active/utilisée pendant la lecture), pour ne jamais perturber une
        # scène en cours de lecture par simple navigation.
        self._scene_sheet_preview: SceneSheet | None = None
        # Dernier GOTO choisi par scène (nom -> label), pour le retrouver en
        # y revenant après avoir navigué ailleurs (voir _refresh_goto_labels).
        # Vidé au redémarrage du logiciel (repart sur INTRO par défaut).
        self._goto_label_by_scene: dict[str, str] = {}
        # Feuille de scène XLSX (scene_sheet.py) du morceau en cours, si le
        # fichier <nom de scène>.xlsx existe à côté du script ; None = aucune
        # feuille, comportement inchangé (voir _apply_scene_sheet_row).
        self._scene_sheet: SceneSheet | None = None
        self._scene_sheet_row: SceneSheetRow | None = None
        # Paroles (lyrics.py) du morceau en cours, si <nom de scène>.csv
        # existe dans Lyrics/ ; None = aucune parole (voir _draw_lyrics_scroll).
        self._lyrics_sheet: LyricsSheet | None = None
        # Temps écoulés (depuis la mesure 1) au début de chaque mesure déjà
        # rencontrée, tient compte du COUNT (voir _cumulative_beats_at_bar) :
        # complété au fil de l'eau, jamais recalculé depuis le début.
        self._lyrics_bar_beats: list[float] = []
        # Index des lignes de paroles actuellement affichées (voir
        # _draw_lyrics_scroll), pour cacher celles qui sortent du cadre.
        self._lyrics_visible_indices: set[int] = set()
        # Demi-hauteur de ligne (métrique réelle de LYRICS_FONT), calculée une
        # seule fois au premier affichage (voir _draw_lyrics_scroll).
        self._lyrics_line_half_height: float | None = None
        # Dernière signature rythmique (numérateur COUNT) poussée à Live, pour
        # ne la renvoyer que si elle change réellement (voir _apply_scene_sheet_row).
        self._live_time_signature_sent: int | None = None
        # Dernier LABEL non vide rencontré : reste affiché tant qu'aucune
        # nouvelle valeur non vide n'arrive ("collant", voir _apply_scene_sheet_row).
        self._scene_label_sticky: str = ""
        # Détection de la présence de Live/AbletonOSC (voir _ping_live) : si
        # CLIC démarre avant Live, les abonnements OSC envoyés au tout début
        # (start_listen_track_*, métronome) se perdent (personne n'écoute
        # encore côté Live) — on les renvoie donc dès que Live répond.
        self._live_available = False
        self._live_last_seen = 0.0

        self._build_ui()
        self._refresh_ports()
        if self.config.get("midi_port"):
            self.port_var.set(self.config["midi_port"])
        self._refresh_controller_ports()
        self._refresh_controller_ports_2()
        self._refresh_controller_ports_3()
        configured_controller_port = self.config.get("controller_port")
        connected_at_startup = False
        if configured_controller_port:
            self.controller_port_var.set(configured_controller_port)
            if configured_controller_port in self.controller_port_combo["values"]:
                self._toggle_controller_connect()
                connected_at_startup = True
        configured_hui_port = self.config.get("hui_port")
        if configured_hui_port:
            self.controller_port_var_2.set(configured_hui_port)
            if configured_hui_port in self.controller_port_combo_2["values"]:
                self._toggle_hui_connect()
        configured_hui_port_2 = self.config.get("hui_port_2")
        if configured_hui_port_2:
            self.controller_port_var_3.set(configured_hui_port_2)
            if configured_hui_port_2 in self.controller_port_combo_3["values"]:
                self._toggle_hui_connect_2()
        if connected_at_startup:
            self._refresh_controller_map_table()
        else:
            self._update_controller_status_label()
        self._refresh_scene_state()
        self._apply_mode()
        # Réappliquée en tout dernier : _apply_mode() ci-dessus pack()
        # link_frame/midi_frame, ce qui refait recalculer par Tk une taille
        # "naturelle" (plus petite, sections repliées) et écraserait la taille
        # fixée plus haut si on la posait avant ces pack() tardifs.
        self.root.update_idletasks()
        self.root.geometry("710x1050+0+0")
        # Sur macOS, la fenêtre n'est réellement "mappée" par Aqua qu'au tout
        # début de mainloop() (update_idletasks() ne suffit pas) : Aqua peut
        # donc encore écraser la taille ci-dessus à ce moment-là. On la
        # reprogramme une fois mainloop lancé pour avoir le dernier mot.
        self.root.after(50, lambda: self.root.geometry("710x1050+0+0"))
        self._poll()
        self._ping_live()
        self._metronome_thread.start()

    # ---------------------------------------------------------------- UI --
    def _build_ui(self) -> None:
        top = tk.Frame(self.root, bg=BG_IDLE)
        top.pack(fill="x", padx=10, pady=8)

        mode_row = tk.Frame(top, bg=BG_IDLE)
        mode_row.pack(fill="x")
        tk.Label(mode_row, text="Source :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.mode_var = tk.StringVar(value=self.config["mode"])
        tk.Radiobutton(
            mode_row, text="Ableton Link", value="link", variable=self.mode_var,
            command=self._apply_mode, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(8, 4))
        tk.Radiobutton(
            mode_row, text="MIDI Clock", value="midi", variable=self.mode_var,
            command=self._apply_mode, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=4)
        tk.Radiobutton(
            mode_row, text="Offline", value="offline", variable=self.mode_var,
            command=self._apply_mode, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=4)

        # -- Bloc Link --
        self.link_frame = tk.Frame(top, bg=BG_IDLE)
        self.link_peers_label = tk.Label(self.link_frame, text="Pairs Link connectés : —", bg=BG_IDLE, fg=FG_TEXT)
        self.link_peers_label.pack(side="left", pady=(6, 0))

        tk.Label(self.link_frame, text="  Régler le tempo :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", pady=(6, 0))
        self.set_tempo_var = tk.DoubleVar(value=120.0)
        # Envoi seulement à la validation (flèches, Entrée ou perte du focus),
        # jamais à chaque touche tapée : sinon chaque état intermédiaire de la
        # saisie (ex. curseur pas en fin de champ) part vers Link/Live, qui
        # peut le rejeter/clamper (tempo max 999) et écraser ce qu'on tape.
        self.tempo_spinbox = tk.Spinbox(
            self.link_frame, from_=0.0, to=500.0, increment=0.1, width=6,
            textvariable=self.set_tempo_var, command=self._set_tempo,
        )
        self.tempo_spinbox.pack(side="left", padx=(4, 4), pady=(6, 0))
        self.tempo_spinbox.bind("<Return>", lambda _event: self._set_tempo())
        self.tempo_spinbox.bind("<KP_Enter>", lambda _event: self._set_tempo())
        self.tempo_spinbox.bind("<FocusOut>", lambda _event: self._set_tempo())

        tk.Label(self.link_frame, text="  Plage fader 16 :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", pady=(6, 0))
        range_labels = [label for label, _key in self.TEMPO_RANGE_OPTIONS]
        range_by_label = dict(self.TEMPO_RANGE_OPTIONS)
        label_by_range = {key: label for label, key in self.TEMPO_RANGE_OPTIONS}
        self.tempo_fader_range_var = tk.StringVar(
            value=label_by_range.get(self.config.get("tempo_fader_range", "6"), range_labels[1])
        )
        tempo_range_combo = ttk.Combobox(
            self.link_frame, textvariable=self.tempo_fader_range_var, state="readonly",
            values=range_labels, width=16,
        )
        tempo_range_combo.pack(side="left", padx=(4, 4), pady=(6, 0))

        def _on_tempo_range_change(_event=None) -> None:
            self.config["tempo_fader_range"] = range_by_label[self.tempo_fader_range_var.get()]
            save_config(self.config)

        tempo_range_combo.bind("<<ComboboxSelected>>", _on_tempo_range_change)

        # -- Bloc MIDI --
        self.midi_frame = tk.Frame(top, bg=BG_IDLE)
        port_row = tk.Frame(self.midi_frame, bg=BG_IDLE)
        port_row.pack(fill="x", pady=(6, 0))
        tk.Label(port_row, text="Port MIDI :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(port_row, textvariable=self.port_var, state="readonly")
        self.port_combo.pack(side="left", padx=6, fill="x", expand=True)

        button_row = tk.Frame(self.midi_frame, bg=BG_IDLE)
        button_row.pack(fill="x", pady=(6, 0))
        tk.Button(button_row, text="Rafraîchir", command=self._refresh_ports).pack(side="left", padx=4)
        self.connect_btn = tk.Button(button_row, text="Connecter", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=4)

        # -- Bloc Offline --
        self.offline_frame = tk.Frame(top, bg=BG_IDLE)
        tk.Label(self.offline_frame, text="Morceau :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", pady=(6, 0))
        self.offline_song_var = tk.StringVar(value="")
        self.offline_song_combo = ttk.Combobox(
            self.offline_frame, textvariable=self.offline_song_var, state="readonly", width=24,
        )
        self.offline_song_combo.pack(side="left", padx=(6, 12), pady=(6, 0))
        self.offline_song_combo.bind("<<ComboboxSelected>>", self._on_offline_song_selected)
        tk.Label(self.offline_frame, text="BPM :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", pady=(6, 0))
        self.offline_tempo_var = tk.DoubleVar(value=self.config.get("offline_bpm", 120.0))
        tk.Spinbox(
            self.offline_frame, from_=20.0, to=300.0, increment=1.0, width=6,
            textvariable=self.offline_tempo_var,
        ).pack(side="left", padx=(4, 0), pady=(6, 0))

        # -- Réglages communs --
        settings_frame = tk.Frame(self.root, bg=BG_IDLE)
        settings_frame.pack(fill="x", padx=10, pady=(0, 8))

        tk.Label(settings_frame, text="Temps par mesure :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.beats_var = tk.IntVar(value=self.config["beats_per_bar"])
        tk.Spinbox(
            settings_frame, from_=1, to=12, width=4, textvariable=self.beats_var,
            command=self._on_settings_change,
        ).pack(side="left", padx=(6, 16))

        tk.Label(settings_frame, text="Latence (ms) :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.latency_var = tk.IntVar(value=self.config["latency_ms"])
        tk.Spinbox(
            settings_frame, from_=-60, to=60, increment=1, width=6,
            textvariable=self.latency_var, command=self._on_settings_change,
        ).pack(side="left", padx=6)

        self.dots_var = tk.BooleanVar(value=self.config["dots_only"])
        tk.Checkbutton(
            settings_frame, text="Points", variable=self.dots_var,
            command=self._on_settings_change, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(16, 0))

        # -- GOTO par LABEL (feuille de scène) + PREROLL, sous "Temps par
        # mesure" : voir _refresh_goto_labels (peuplé à la sélection d'une
        # scène) et _scene_launch (calcule le vrai saut Live). --
        goto_frame = tk.Frame(self.root, bg=BG_IDLE)
        goto_frame.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(goto_frame, text="GOTO :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.goto_label_var = tk.StringVar(value="")
        self.goto_label_combo = ttk.Combobox(
            goto_frame, textvariable=self.goto_label_var, state="readonly", width=16,
        )
        self.goto_label_combo.pack(side="left", padx=(6, 16))
        tk.Label(goto_frame, text="PREROLL (mesures) :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.preroll_var = tk.IntVar(value=0)
        tk.Spinbox(
            goto_frame, from_=0, to=999, width=5, textvariable=self.preroll_var,
        ).pack(side="left", padx=(6, 0))

        # -- Affichage principal du temps : un carré, gros pour 1/3, petit pour 2/4 --
        self.display = tk.Canvas(self.root, bg=BG_IDLE, highlightthickness=0)
        self.display.pack(expand=True, fill="both", padx=10, pady=4)
        # Items persistants (créés une seule fois, puis déplacés/recolorés via
        # coords()/itemconfig()) : recréer tous les items à chaque frame
        # (delete("all") + create_*) au rythme du _poll (30 ms) est ce qui
        # rend l'appli à la traîne dès qu'on bouge la souris au-dessus du
        # canvas sous macOS/Tk (Cocoa recalcule les zones de suivi de la
        # souris à chaque création/suppression d'item).
        self._canvas_items: dict[str, int] = {}

        # -- Label de section (INTRO/COUPLET/REFRAIN..., voir scene_sheet.py) --
        self.scene_label_label = tk.Label(
            self.root, text="", bg=BG_IDLE, fg="#7fb2ff", font=("Helvetica", 16, "bold"),
        )
        self.scene_label_label.pack(fill="x", padx=10, pady=(0, 2))

        # -- Compteur de mesures depuis le lancement du morceau en cours --
        self.bar_count_label = tk.Label(
            self.root, text="", bg=BG_IDLE, fg=FG_TEXT, font=("Helvetica", 16, "bold"),
        )
        self.bar_count_label.pack(fill="x", padx=10, pady=(0, 2))

        # -- Nom de la scène en cours, détaché des boutons de navigation --
        # Jaune = scène sélectionnée mais pas encore lancée, vert = lancée.
        self.scene_name_label = tk.Label(
            self.root, text="…", bg=BG_IDLE, fg=SCENE_NOT_LAUNCHED, font=("Helvetica", 20, "bold"),
            justify="center",
        )
        self.scene_name_label.pack(fill="x", padx=10, pady=(0, 4))

        bottom = tk.Frame(self.root, bg=BG_IDLE)
        bottom.pack(fill="x", padx=10, pady=4)
        self.status_label = tk.Label(bottom, text="Déconnecté", bg=BG_IDLE, fg="#bbbbbb")
        self.status_label.pack(side="left")
        self.bpm_label = tk.Label(bottom, text="", bg=BG_IDLE, fg="#bbbbbb")
        self.bpm_label.pack(side="right")

        # -- Lecture/Stop/Métronomes puis Nudge (-1/+1)/navigation scènes
        # (▲▼) : 2 rangées de 4 boutons carrés de même taille (modèle : le
        # bouton +1). À gauche de chaque bouton : "A" (apprendre le code
        # MIDI) au-dessus de "E" (effacer l'apprentissage), eux aussi carrés
        # (taille fixe en pixels). --
        controls_row_1 = tk.Frame(self.root, bg=BG_IDLE)
        controls_row_1.pack(fill="x", padx=10, pady=(0, 2))
        controls_row_2 = tk.Frame(self.root, bg=BG_IDLE)
        controls_row_2.pack(fill="x", padx=10, pady=(0, 8))
        MINI_SIZE = 22  # pixels : taille fixe pour que A/E soient réellement carrés
        self.learn_buttons: dict[str, tk.Button] = {}
        self.clear_buttons: dict[str, tk.Button] = {}
        self.action_buttons: dict[str, tk.Frame] = {}
        self._control_buttons: dict[str, tk.Button] = {}
        self._action_flash_after_id: dict[str, str] = {}
        # Fond persistant (jaune si actif) des boutons Lecture/M1/M2, distinct
        # du flash bref MIDI ci-dessous (_flash_action_button le restaure ici
        # plutôt que vers BG_IDLE une fois le flash terminé).
        self._action_active_bg: dict[str, str] = {}

        def add_mini_button(parent: tk.Frame, text: str, command) -> tk.Button:
            holder = tk.Frame(parent, width=MINI_SIZE, height=MINI_SIZE, bg=BG_IDLE)
            holder.pack_propagate(False)
            holder.pack(side="top", pady=(0, 2) if text == "A" else (2, 0))
            btn = tk.Button(holder, text=text, command=command, font=("Helvetica", 9), padx=0, pady=0)
            btn.pack(fill="both", expand=True)
            return btn

        def add_control(parent_row: tk.Frame, action: str, text: str, font_size: int = 14) -> None:
            group = tk.Frame(parent_row, bg=BG_IDLE)
            group.pack(side="left", padx=4)
            mini = tk.Frame(group, bg=BG_IDLE)
            mini.pack(side="left", padx=(0, 2))
            self.learn_buttons[action] = add_mini_button(mini, "A", lambda: self._start_learn(action))
            self.clear_buttons[action] = add_mini_button(mini, "E", lambda: self._clear_assignment(action))
            # macOS Aqua ignore le bg d'un tk.Button natif : on flashe ce cadre autour, pas le bouton.
            # Taille de la boîte fixée en pixels (pack_propagate(False)) plutôt qu'en
            # largeur/hauteur "caractères" du Button : cette dernière dépend de la
            # taille de police, ce qui rendait la boîte de STOP plus grande que celle
            # de PLAY dès que leurs polices de glyphe différaient (14 vs 16 pt).
            flash_holder = tk.Frame(group, width=80, height=56, bg=BG_IDLE)
            flash_holder.pack_propagate(False)
            flash_holder.pack(side="left")
            btn = tk.Button(
                flash_holder, text=text, command=self._action_commands[action], font=("Helvetica", font_size, "bold"),
            )
            btn.pack(fill="both", expand=True, padx=3, pady=3)
            self.action_buttons[action] = flash_holder
            self._control_buttons[action] = btn

        add_control(controls_row_1, "play", "▶")
        # Glyphe ■ plus petit que ▶ à taille de police égale : agrandi.
        add_control(controls_row_1, "stop", "■", font_size=32)
        add_control(controls_row_1, "metronome", "M1")
        add_control(controls_row_1, "metronome_2", "M2")
        add_control(controls_row_2, "minus", "−1")
        add_control(controls_row_2, "plus", "+1")
        add_control(controls_row_2, "scene_prev", "▲")
        add_control(controls_row_2, "scene_next", "▼")

        # -- Réglages AUDIO/MIDI dans des fenêtres à part (plutôt qu'un
        # panneau repliable dans la fenêtre principale) : sur macOS Aqua, un
        # tk.Button natif ignore son bg dès que la fenêtre a le focus (il
        # repasse en blanc natif), ce qui rendait le texte blanc des entêtes
        # illisible une fois l'appli au premier plan. Une fenêtre à part
        # évite complètement ce souci d'entête à fond personnalisé. --
        def _make_settings_window(title: str) -> tuple[tk.Toplevel, tk.Frame]:
            win = tk.Toplevel(self.root)
            win.title(title)
            win.configure(bg=BG_IDLE)
            # On masque plutôt que détruire à la fermeture : les widgets/valeurs
            # (Combobox, StringVar…) restent intacts pour la prochaine ouverture.
            win.protocol("WM_DELETE_WINDOW", win.withdraw)
            win.withdraw()
            content = tk.Frame(win, bg=BG_IDLE)
            content.pack(fill="both", expand=True, padx=10, pady=10)
            return win, content

        def _show_settings_window(win: tk.Toplevel) -> None:
            win.deiconify()
            win.lift()

        settings_row = tk.Frame(self.root, bg=BG_IDLE)
        settings_row.pack(fill="x", padx=10, pady=(0, 8))
        self.audio_settings_win, audio_content = _make_settings_window("Réglages AUDIO")
        self.midi_settings_win, midi_content = _make_settings_window("Réglages MIDI")
        tk.Button(
            settings_row, text="Réglages AUDIO", command=lambda: _show_settings_window(self.audio_settings_win),
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            settings_row, text="Réglages MIDI", command=lambda: _show_settings_window(self.midi_settings_win),
        ).pack(side="left")
        # Paroles défilantes (module à venir) : réduit de moitié la taille des
        # chiffres/points et les remonte en haut du canvas, pour laisser 50%
        # de l'espace libre en bas (voir _update_display/_draw_digit).
        self.lyrics_var = tk.BooleanVar(value=self.config["lyrics_enabled"])
        tk.Checkbutton(
            settings_row, text="Afficher les paroles", variable=self.lyrics_var,
            command=self._on_settings_change, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(16, 0))
        self.lyrics_reading_ratio_var = tk.DoubleVar(
            value=self.config.get("lyrics_reading_position_ratio", LYRICS_READING_POSITION_RATIO)
        )
        tk.Scale(
            settings_row, from_=0.0, to=1.0, resolution=0.01, orient="horizontal", length=100,
            variable=self.lyrics_reading_ratio_var, showvalue=False, command=lambda _v: self._on_settings_change(),
            bg=BG_IDLE, fg=FG_TEXT, troughcolor="#333333", highlightthickness=0,
        ).pack(side="left", padx=(4, 0))

        # -- Métronome audio local (clic.wav, voir audio_metronome.py) : carte
        # son + sortie (paire stéréo ou mono), activé/désactivé par le bouton
        # "M" (_toggle_metronome), plus le métronome interne de Live. --
        metronome_frame = tk.Frame(audio_content, bg=BG_IDLE)
        metronome_frame.pack(fill="x")
        tk.Label(metronome_frame, text="Métronome — Carte son :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.metronome_device_var = tk.StringVar(
            value=self.config["metronome_audio_device"] or self.METRONOME_DEFAULT_DEVICE_LABEL,
        )
        self.metronome_device_combo = ttk.Combobox(
            metronome_frame, textvariable=self.metronome_device_var, state="readonly", width=26,
        )
        self.metronome_device_combo.pack(side="left", padx=(6, 4))
        self.metronome_device_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_metronome_audio_change())
        tk.Button(
            metronome_frame, text="Rafraîchir", command=self._refresh_metronome_devices,
        ).pack(side="left", padx=(0, 16))
        tk.Label(metronome_frame, text="Kit :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.metronome_kit_var = tk.StringVar(value=self.config["metronome_kit"])
        self.metronome_kit_combo = ttk.Combobox(
            metronome_frame, textvariable=self.metronome_kit_var, state="readonly", width=10,
            values=list_kits(),
        )
        self.metronome_kit_combo.pack(side="left", padx=(6, 16))
        self.metronome_kit_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_metronome_audio_change())
        tk.Label(metronome_frame, text="Sortie :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.metronome_channels_var = tk.IntVar(value=self.config["metronome_audio_channels"])
        tk.Radiobutton(
            metronome_frame, text="Stéréo (2 canaux)", variable=self.metronome_channels_var, value=2,
            command=self._on_metronome_audio_change, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(6, 0))
        tk.Radiobutton(
            metronome_frame, text="Mono (1 canal)", variable=self.metronome_channels_var, value=1,
            command=self._on_metronome_audio_change, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(6, 0))
        tk.Label(metronome_frame, text="Latence clic (ms) :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", padx=(16, 0))
        self.metronome_latency_var = tk.IntVar(value=self.config["metronome_audio_latency_ms"])
        tk.Spinbox(
            metronome_frame, from_=-500, to=500, increment=5, width=6,
            textvariable=self.metronome_latency_var, command=self._on_metronome_audio_change,
        ).pack(side="left", padx=(6, 0))

        # -- Deuxième sortie métronome (2e musicien, propre carte son/kit/
        # latence), activée/désactivée par le bouton "M2" (_toggle_metronome_2). --
        metronome_frame_2 = tk.Frame(audio_content, bg=BG_IDLE)
        metronome_frame_2.pack(fill="x", pady=(6, 0))
        tk.Label(metronome_frame_2, text="Métronome 2 — Carte son :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.metronome_device_var_2 = tk.StringVar(
            value=self.config["metronome_audio_device_2"] or self.METRONOME_DEFAULT_DEVICE_LABEL,
        )
        self.metronome_device_combo_2 = ttk.Combobox(
            metronome_frame_2, textvariable=self.metronome_device_var_2, state="readonly", width=26,
        )
        self.metronome_device_combo_2.pack(side="left", padx=(6, 4))
        self.metronome_device_combo_2.bind("<<ComboboxSelected>>", lambda _e: self._on_metronome_audio_change_2())
        tk.Button(
            metronome_frame_2, text="Rafraîchir", command=self._refresh_metronome_devices,
        ).pack(side="left", padx=(0, 16))
        tk.Label(metronome_frame_2, text="Kit :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.metronome_kit_var_2 = tk.StringVar(value=self.config["metronome_kit_2"])
        self.metronome_kit_combo_2 = ttk.Combobox(
            metronome_frame_2, textvariable=self.metronome_kit_var_2, state="readonly", width=10,
            values=list_kits(),
        )
        self.metronome_kit_combo_2.pack(side="left", padx=(6, 16))
        self.metronome_kit_combo_2.bind("<<ComboboxSelected>>", lambda _e: self._on_metronome_audio_change_2())
        tk.Label(metronome_frame_2, text="Sortie :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.metronome_channels_var_2 = tk.IntVar(value=self.config["metronome_audio_channels_2"])
        tk.Radiobutton(
            metronome_frame_2, text="Stéréo (2 canaux)", variable=self.metronome_channels_var_2, value=2,
            command=self._on_metronome_audio_change_2, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(6, 0))
        tk.Radiobutton(
            metronome_frame_2, text="Mono (1 canal)", variable=self.metronome_channels_var_2, value=1,
            command=self._on_metronome_audio_change_2, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(6, 0))
        tk.Label(metronome_frame_2, text="Latence clic (ms) :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", padx=(16, 0))
        self.metronome_latency_var_2 = tk.IntVar(value=self.config["metronome_audio_latency_ms_2"])
        tk.Spinbox(
            metronome_frame_2, from_=-500, to=500, increment=5, width=6,
            textvariable=self.metronome_latency_var_2, command=self._on_metronome_audio_change_2,
        ).pack(side="left", padx=(6, 0))
        self._refresh_metronome_devices()

        # -- Contrôleur MIDI (ex. Behringer BCF2000) pour piloter les mêmes boutons --
        controller_row = tk.Frame(midi_content, bg=BG_IDLE)
        controller_row.pack(fill="x", pady=(0, 4))
        tk.Label(controller_row, text="MIDI IN OSC Boutons :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.controller_port_var = tk.StringVar()
        self.controller_port_combo = ttk.Combobox(
            controller_row, textvariable=self.controller_port_var, state="readonly", width=22,
        )
        self.controller_port_combo.pack(side="left", padx=6)
        tk.Button(controller_row, text="Rafraîchir", command=self._refresh_controller_ports).pack(side="left", padx=2)
        self.controller_connect_btn = tk.Button(
            controller_row, text="Connecter", command=self._toggle_controller_connect,
        )
        self.controller_connect_btn.pack(side="left", padx=2)

        # -- Pont HUI -> OSC (ex. Yamaha 01V96V2), 16 voies réparties sur 2 ports --
        controller_row_2 = tk.Frame(midi_content, bg=BG_IDLE)
        controller_row_2.pack(fill="x", pady=(0, 4))
        tk.Label(controller_row_2, text="MIDI IN Faders and Mutes (voies 1-8) :", bg=BG_IDLE, fg=FG_TEXT).pack(
            side="left"
        )
        self.controller_port_var_2 = tk.StringVar()
        self.controller_port_combo_2 = ttk.Combobox(
            controller_row_2, textvariable=self.controller_port_var_2, state="readonly", width=22,
        )
        self.controller_port_combo_2.pack(side="left", padx=6)
        tk.Button(controller_row_2, text="Rafraîchir", command=self._refresh_controller_ports_2).pack(
            side="left", padx=2
        )
        self.hui_connect_btn = tk.Button(
            controller_row_2, text="Connecter", command=self._toggle_hui_connect,
        )
        self.hui_connect_btn.pack(side="left", padx=2)

        controller_row_3 = tk.Frame(midi_content, bg=BG_IDLE)
        controller_row_3.pack(fill="x", pady=(0, 4))
        tk.Label(controller_row_3, text="MIDI IN Faders and Mutes (voies 9-16) :", bg=BG_IDLE, fg=FG_TEXT).pack(
            side="left"
        )
        self.controller_port_var_3 = tk.StringVar()
        self.controller_port_combo_3 = ttk.Combobox(
            controller_row_3, textvariable=self.controller_port_var_3, state="readonly", width=22,
        )
        self.controller_port_combo_3.pack(side="left", padx=6)
        tk.Button(controller_row_3, text="Rafraîchir", command=self._refresh_controller_ports_3).pack(
            side="left", padx=2
        )
        self.hui_connect_btn_2 = tk.Button(
            controller_row_3, text="Connecter", command=self._toggle_hui_connect_2,
        )
        self.hui_connect_btn_2.pack(side="left", padx=2)

        hui_mapping_row = tk.Frame(midi_content, bg=BG_IDLE)
        hui_mapping_row.pack(fill="x", pady=(0, 4))
        tk.Button(
            hui_mapping_row, text="Configurer le mapping des faders…", command=self._open_hui_mapping_dialog,
        ).pack(side="left")
        tk.Label(
            hui_mapping_row, text="  (fader 16 dédié au tempo, voir plus haut)", bg=BG_IDLE, fg="#888888",
        ).pack(side="left")

        status_row = tk.Frame(midi_content, bg=BG_IDLE)
        status_row.pack(fill="x", pady=(0, 4))
        self.controller_status_label = tk.Label(status_row, text="", bg=BG_IDLE, fg="#bbbbbb")
        self.controller_status_label.pack(side="left")

        table_row = tk.Frame(midi_content, bg=BG_IDLE)
        table_row.pack(fill="x", pady=(0, 4))
        header_font = ("Helvetica", 9, "bold")
        tk.Label(table_row, text="Bouton", bg=BG_IDLE, fg="#888888", font=header_font).grid(
            row=0, column=0, sticky="w", padx=(0, 16),
        )
        tk.Label(table_row, text="Commande MIDI", bg=BG_IDLE, fg="#888888", font=header_font).grid(
            row=0, column=1, sticky="w",
        )
        self.controller_map_labels: dict[str, tk.Label] = {}
        for row, action in enumerate(self._action_order, start=1):
            tk.Label(table_row, text=self._action_labels[action], bg=BG_IDLE, fg=FG_TEXT).grid(
                row=row, column=0, sticky="w", padx=(0, 16),
            )
            value_label = tk.Label(table_row, text="", bg=BG_IDLE, fg="#bbbbbb")
            value_label.grid(row=row, column=1, sticky="w")
            self.controller_map_labels[action] = value_label

        web_row = tk.Frame(self.root, bg=BG_IDLE)
        web_row.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(web_row, text="Affichage smartphone :", bg=BG_IDLE, fg="#bbbbbb").pack(side="left")
        url_var = tk.StringVar(value=self.web_server.url())
        # Entry en lecture seule plutôt qu'un Label : le texte reste
        # sélectionnable/copiable (Ctrl/Cmd+C) même si non modifiable.
        url_entry = tk.Entry(
            web_row, textvariable=url_var, fg="#7fb2ff", bg=BG_IDLE, disabledforeground="#7fb2ff",
            font=("Helvetica", 12, "bold"), bd=0, relief="flat", width=len(url_var.get()) + 1,
            state="readonly", readonlybackground=BG_IDLE,
        )
        url_entry.pack(side="left", padx=6)

        def _copy_url() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(url_var.get())

        tk.Button(web_row, text="Copier", command=_copy_url).pack(side="left", padx=(4, 0))

    def _apply_mode(self) -> None:
        mode = self.mode_var.get()
        if mode != "offline" and self._offline_playing:
            self._offline_playing = False
            self._offline_last_beat_time = 0.0
        self.link_frame.pack_forget()
        self.midi_frame.pack_forget()
        self.offline_frame.pack_forget()
        if mode == "link":
            self.link_frame.pack(fill="x")
        elif mode == "midi":
            self.midi_frame.pack(fill="x")
        else:
            self._refresh_offline_songs()
            self.offline_frame.pack(fill="x")
        self._on_settings_change()

    def _refresh_offline_songs(self) -> None:
        songs = sorted(path.stem for path in self.SCENE_SHEET_DIR.glob("*.xlsx"))
        self.offline_song_combo["values"] = songs
        if self.offline_song_var.get() not in songs:
            self.offline_song_var.set(songs[0] if songs else "")
        self._on_offline_song_selected()

    def _on_offline_song_selected(self, _event=None) -> None:
        if self._offline_playing:
            self._stop_return_to_start()
        self._scene_name = self.offline_song_var.get()
        self._scene_sheet_preview = load_scene_sheet(
            self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: print(f"[Feuille de scène] {msg}"),
        )
        labels = self._scene_sheet_preview.labels() if self._scene_sheet_preview is not None else []
        self.goto_label_combo["values"] = labels
        remembered = self._goto_label_by_scene.get(self._scene_name)
        if remembered in labels:
            self.goto_label_var.set(remembered)
        elif "INTRO" in labels:
            self.goto_label_var.set("INTRO")
        elif labels:
            self.goto_label_var.set(labels[0])
        else:
            self.goto_label_var.set("")
        self.scene_name_label.config(text=self._scene_name or "Aucun morceau", fg=SCENE_NOT_LAUNCHED)

    def _launch_offline_song(self) -> None:
        if not self._scene_name:
            self.status_label.config(text="Aucun fichier XLSX disponible")
            return
        self._scene_sheet = load_scene_sheet(
            self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: print(f"[Feuille de scène] {msg}"),
        )
        if self._scene_sheet is None:
            self.status_label.config(text=f"Feuille {self._scene_name}.xlsx illisible")
            return
        self._lyrics_sheet = load_lyrics(
            self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: print(f"[Paroles] {msg}"),
        )
        self.shared_state.set_lyrics_lines(self._lyrics_sheet.lines if self._lyrics_sheet is not None else [])
        self._lyrics_bar_beats = []
        label_bar = self._scene_sheet.bar_for_label(self.goto_label_var.get())
        try:
            preroll = max(0, int(self.preroll_var.get()))
            bpm = max(20.0, min(300.0, float(self.offline_tempo_var.get())))
        except (tk.TclError, ValueError):
            preroll = 0
            bpm = 120.0
        self.config["offline_bpm"] = bpm
        save_config(self.config)
        self._bar_count_start = max(1, label_bar - preroll) if label_bar is not None else 1
        self._scene_label_sticky = self._scene_sheet.label_at_or_before(self._bar_count_start)
        self._bar_count = None
        self._bar_count_prev_beat = None
        self._bar_count_signature_pushed_for = None
        self._awaiting_bar_start = True
        self._pending_goto_jump_beats = None
        self._metronome_end_muted = False
        self._offline_beat_in_bar = 1
        self._offline_last_beat_time = 0.0
        self._offline_playing = True
        self._apply_scene_sheet_row(self._bar_count_start)
        self.bar_count_label.config(text="")
        self.scene_name_label.config(text=self._scene_name, fg=SCENE_LAUNCHED)
        self.shared_state.set_scene_name(self._scene_name)
        self.shared_state.set_scene_launched(True)
        self.status_label.config(text="Lecture Offline")

    # --------------------------------------------------------- MIDI ports --
    def _refresh_ports(self) -> None:
        ports = self.listener.list_ports()
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connect(self) -> None:
        if self.listener.port_name:
            self.listener.close()
            self.connect_btn.config(text="Connecter")
            self.status_label.config(text="Déconnecté")
            return

        port_name = self.port_var.get()
        if not port_name:
            self.status_label.config(text="Aucun port MIDI sélectionné")
            return
        try:
            self.listener.connect(port_name)
        except Exception as exc:  # noqa: BLE001 - affichage utilisateur simple
            self.status_label.config(text=f"Erreur de connexion : {exc}")
            return
        self.connect_btn.config(text="Déconnecter")
        self.status_label.config(text=f"Connecté à {port_name} — en attente du MIDI Clock…")
        self.config["midi_port"] = port_name
        save_config(self.config)

    # ------------------------------------------------ Contrôleur MIDI (BCF2000) --
    def _refresh_controller_ports(self) -> None:
        ports = self.controller.list_ports()
        self.controller_port_combo["values"] = ports
        if ports and not self.controller_port_var.get():
            self.controller_port_var.set(ports[0])

    def _refresh_controller_ports_2(self) -> None:
        ports = self.controller.list_ports()
        self.controller_port_combo_2["values"] = ports
        if ports and not self.controller_port_var_2.get():
            self.controller_port_var_2.set(ports[0])

    def _toggle_hui_connect(self) -> None:
        if self.hui_bridge.port_name:
            self.hui_bridge.close()
            self._set_hui_listen(0, listen=False)
            self.hui_connect_btn.config(text="Connecter")
            return
        port_name = self.controller_port_var_2.get()
        if not port_name:
            return
        try:
            self.hui_bridge.connect(port_name)
        except Exception as exc:  # noqa: BLE001 - affichage utilisateur simple
            print(f"[HUI] erreur de connexion : {exc}")
            return
        self._set_hui_listen(0, listen=True)
        self.hui_connect_btn.config(text="Déconnecter")
        self.config["hui_port"] = port_name
        save_config(self.config)

    def _refresh_controller_ports_3(self) -> None:
        ports = self.controller.list_ports()
        self.controller_port_combo_3["values"] = ports
        if ports and not self.controller_port_var_3.get():
            self.controller_port_var_3.set(ports[0])

    def _toggle_hui_connect_2(self) -> None:
        if self.hui_bridge_2.port_name:
            self.hui_bridge_2.close()
            self._set_hui_listen(8, listen=False)
            self.hui_connect_btn_2.config(text="Connecter")
            return
        port_name = self.controller_port_var_3.get()
        if not port_name:
            return
        try:
            self.hui_bridge_2.connect(port_name)
        except Exception as exc:  # noqa: BLE001 - affichage utilisateur simple
            print(f"[HUI] erreur de connexion : {exc}")
            return
        self._set_hui_listen(8, listen=True)
        self.hui_connect_btn_2.config(text="Déconnecter")
        self.config["hui_port_2"] = port_name
        save_config(self.config)

    @staticmethod
    def _hui_zone_maps(mapping: list[int]) -> tuple[dict[int, int], dict[int, int]]:
        """Convertit le mapping piste Live (index) -> tranche HUI (valeur,
        0-15) en deux dicts zone (0-7) -> piste, un par port MIDI (tranches
        0-7 -> bridge principal, 8-15 -> bridge_2)."""
        zone_map_1: dict[int, int] = {}
        zone_map_2: dict[int, int] = {}
        for track, channel in enumerate(mapping):
            if 0 <= channel < 8:
                zone_map_1[channel] = track
            elif 8 <= channel < 16:
                zone_map_2[channel - 8] = track
        return zone_map_1, zone_map_2

    def _apply_track_mapping(self, mapping: list[int]) -> None:
        """Applique le mapping choisi dans la fenêtre de configuration (pas
        automatique : rien ne change avant l'appui sur "Appliquer")."""
        self._track_mapping = list(mapping)
        zone_map_1, zone_map_2 = self._hui_zone_maps(self._track_mapping)
        self.hui_bridge.set_mapping(zone_map_1)
        self.hui_bridge_2.set_mapping(zone_map_2)
        self.config["hui_track_mapping"] = self._track_mapping
        save_config(self.config)
        self._refresh_hui_feedback()

    def _open_hui_mapping_dialog(self) -> None:
        """Fenêtre de mapping piste Live (ligne) <-> tranche HUI/Yamaha
        (colonne) : une seule tranche par piste (boutons radio par ligne,
        donc pas de doublon possible sur une même ligne), diagonale 1<->1 par
        défaut. Colonne "—" en plus des 15 tranches : laisse la piste sans
        tranche assignée (pas commandée par la console). Tranche 16 absente
        (réservée au tempo, voir TEMPO_FADER_ZONE). Rien n'est appliqué avant
        l'appui sur "Appliquer"."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Mapping faders Live ↔ Yamaha")
        dialog.configure(bg=BG_IDLE)

        tk.Label(dialog, text="Piste \\ Tranche", bg=BG_IDLE, fg=FG_TEXT).grid(row=0, column=0, padx=(4, 6))
        tk.Label(dialog, text="—", bg=BG_IDLE, fg=FG_TEXT, width=2).grid(row=0, column=1, padx=1, pady=(4, 2))
        for col in range(15):
            tk.Label(dialog, text=str(col + 1), bg=BG_IDLE, fg=FG_TEXT, width=2).grid(
                row=0, column=col + 2, padx=1, pady=(4, 2)
            )

        row_vars: list[tk.IntVar] = []
        for track in range(16):
            var = tk.IntVar(value=self._track_mapping[track])
            row_vars.append(var)
            tk.Label(dialog, text=f"Piste {track + 1}", bg=BG_IDLE, fg=FG_TEXT).grid(
                row=track + 1, column=0, sticky="w", padx=(4, 6)
            )
            tk.Radiobutton(
                dialog, variable=var, value=-1, bg=BG_IDLE, activebackground=BG_IDLE, selectcolor="#333333",
            ).grid(row=track + 1, column=1)
            for col in range(15):
                tk.Radiobutton(
                    dialog, variable=var, value=col, bg=BG_IDLE, activebackground=BG_IDLE, selectcolor="#333333",
                ).grid(row=track + 1, column=col + 2)

        button_row = tk.Frame(dialog, bg=BG_IDLE)
        button_row.grid(row=17, column=0, columnspan=17, pady=8)
        tk.Button(
            button_row, text="Appliquer", command=lambda: self._apply_track_mapping([v.get() for v in row_vars]),
        ).pack(side="left", padx=6)
        tk.Button(button_row, text="Fermer", command=dialog.destroy).pack(side="left", padx=6)

    def _refresh_hui_feedback(self) -> None:
        """Redemande le volume/mute/nom actuel des 16 pistes pour repositionner
        les faders et LED de la console sur leur véritable état (ex. après
        _apply_track_mapping, ou pour se resynchroniser en cas de doute)."""
        try:
            for track in range(16):
                self.live_osc.get_track_volume(track)
                self.live_osc.get_track_mute(track)
                self.live_osc.get_track_name(track)
        except OSError as exc:
            self.status_label.config(text=f"Erreur OSC : {exc}")

    def _set_hui_listen(self, channel_offset: int, listen: bool) -> None:
        """Abonne/désabonne aux changements de volume, mute et nom d'Ableton
        pour les 8 pistes couvertes par un pont HUI, pour le retour vers la
        console (fader/LED mute/nom qui reflètent l'état réel de Live).
        Pas besoin d'un get explicite en plus du start_listen : AbletonOSC
        renvoie déjà la valeur actuelle immédiatement à l'abonnement (sinon,
        deux requêtes "état actuel" en vol pouvaient se répondre dans le
        désordre et renvoyer une valeur périmée juste après un changement
        réel, provoquant un faux retour en arrière du fader sur la console)."""
        for track in range(channel_offset, channel_offset + 8):
            if listen:
                self.live_osc.start_listen_track_volume(track)
                self.live_osc.start_listen_track_mute(track)
                self.live_osc.start_listen_track_name(track)
            else:
                self.live_osc.stop_listen_track_volume(track)
                self.live_osc.stop_listen_track_mute(track)
                self.live_osc.stop_listen_track_name(track)

    def _toggle_controller_connect(self) -> None:
        if self.controller.port_name:
            self.controller.close()
            self.controller_connect_btn.config(text="Connecter")
            return
        port_name = self.controller_port_var.get()
        if not port_name:
            return
        try:
            self.controller.connect(port_name)
        except Exception as exc:  # noqa: BLE001 - affichage utilisateur simple
            self.controller_status_label.config(text=f"Erreur de connexion : {exc}")
            return
        self.controller_connect_btn.config(text="Déconnecter")
        self.config["controller_port"] = port_name
        save_config(self.config)
        self._update_controller_status_label()
        # Repositionne les LED métronome sur leur véritable état dès la
        # (re)connexion, sinon la console repart LED éteinte quel que soit l'état réel.
        self._send_controller_led("metronome", self._metronome_on)
        self._send_controller_led("metronome_2", self._metronome_on_2)

    def _start_learn(self, action: str) -> None:
        self._learning = action
        self._update_controller_status_label()

    def _clear_assignment(self, action: str) -> None:
        self.controller_map[action] = None
        self.config[f"controller_map_{action}"] = None
        save_config(self.config)
        self._update_controller_status_label()

    def _update_controller_status_label(self) -> None:
        if self._learning is not None:
            self.controller_status_label.config(
                text=f"Appuyez sur le bouton du contrôleur pour {self._action_labels[self._learning]}…"
            )
        else:
            self.controller_status_label.config(text="")
        self._refresh_controller_map_table()

    def _refresh_controller_map_table(self) -> None:
        for action in self._action_order:
            self.controller_map_labels[action].config(text=_controller_label(self.controller_map[action]))
        for action, btn in self.clear_buttons.items():
            btn.config(state="normal" if self.controller_map[action] else "disabled")

    def _flash_action_button(self, action: str) -> None:
        """Flash bref en bleu pour visualiser la réception d'une donnée MIDI mappée."""
        holder = self.action_buttons.get(action)
        if holder is None:
            return
        pending = self._action_flash_after_id.pop(action, None)
        if pending is not None:
            self.root.after_cancel(pending)
        holder.config(bg="#2f6fed")

        def _restore() -> None:
            self._action_flash_after_id.pop(action, None)
            holder.config(bg=self._action_active_bg.get(action, BG_IDLE))

        self._action_flash_after_id[action] = self.root.after(200, _restore)

    def _set_action_active(self, action: str, active: bool) -> None:
        """Fond jaune persistant tant que l'action reste "active" (lecture en
        cours, métronome allumé) — au lieu du point/texte utilisé avant."""
        holder = self.action_buttons.get(action)
        if holder is None:
            return
        bg = FLASH_YELLOW if active else BG_IDLE
        if self._action_active_bg.get(action) == bg:
            return
        self._action_active_bg[action] = bg
        if action not in self._action_flash_after_id:
            holder.config(bg=bg)

    def _poll_controller(self) -> None:
        try:
            while True:
                message = self._controller_queue.get_nowait()
                key = _controller_key(message)
                # Log actif : identifier quel bouton/encodeur physique a été touché.
                print(f"[Contrôleur MIDI] {_describe_controller_message(message, key)}")
                # Ne déclenche que sur l'appui (vélocité/valeur > 0), pas le relâchement.
                if key is None or len(message) < 3 or message[2] <= 0:
                    continue
                if self._learning is not None:
                    action = self._learning
                    self.controller_map[action] = key
                    self.config[f"controller_map_{action}"] = list(key)
                    save_config(self.config)
                    self._learning = None
                    self._update_controller_status_label()
                    # Touche fraîchement apprise : reflète tout de suite l'état
                    # courant sur sa LED (utile si le métronome était déjà actif).
                    if action == "metronome":
                        self._send_controller_led("metronome", self._metronome_on)
                    elif action == "metronome_2":
                        self._send_controller_led("metronome_2", self._metronome_on_2)
                    continue
                for action, mapped_key in self.controller_map.items():
                    if key == mapped_key:
                        now = time.monotonic()
                        if now - self._controller_last_fire.get(action, 0.0) < self.CONTROLLER_DEBOUNCE_S:
                            break
                        self._controller_last_fire[action] = now
                        self._flash_action_button(action)
                        self._action_commands[action]()
                        break
        except queue.Empty:
            pass

    def _on_settings_change(self) -> None:
        try:
            beats = max(1, int(self.beats_var.get()))
            latency = int(self.latency_var.get())
        except (tk.TclError, ValueError):
            return
        self.midi_state.beats_per_bar = beats
        self.config["beats_per_bar"] = beats
        self._beats_per_bar_cache = beats
        self.config["latency_ms"] = latency
        self.config["mode"] = self.mode_var.get()
        self._mode_cache = self.config["mode"]
        self.config["dots_only"] = self.dots_var.get()
        self.config["lyrics_enabled"] = self.lyrics_var.get()
        self.config["lyrics_reading_position_ratio"] = self.lyrics_reading_ratio_var.get()
        save_config(self.config)

    def _safe_int_var(self, var: tk.IntVar, cache_attr: str) -> int:
        """Lit un IntVar de Spinbox sans planter _poll() quand le champ est
        momentanément vide (pendant la frappe) : renvoie la dernière valeur
        valide connue dans ce cas, au lieu de laisser remonter TclError."""
        try:
            value = int(var.get())
        except (tk.TclError, ValueError):
            return getattr(self, cache_attr)
        setattr(self, cache_attr, value)
        return value

    def _refresh_metronome_devices(self) -> None:
        devices = list_output_devices()
        values = [self.METRONOME_DEFAULT_DEVICE_LABEL] + devices
        self.metronome_device_combo["values"] = values
        if self.metronome_device_var.get() not in devices:
            self.metronome_device_var.set(self.METRONOME_DEFAULT_DEVICE_LABEL)
        self.metronome_device_combo_2["values"] = values
        if self.metronome_device_var_2.get() not in devices:
            self.metronome_device_var_2.set(self.METRONOME_DEFAULT_DEVICE_LABEL)

    def _on_metronome_audio_change(self) -> None:
        device = self.metronome_device_var.get()
        if device == self.METRONOME_DEFAULT_DEVICE_LABEL:
            device = ""
        try:
            channels = 1 if int(self.metronome_channels_var.get()) == 1 else 2
        except (tk.TclError, ValueError):
            channels = 2
        self._audio_metronome.set_kit(self.metronome_kit_var.get())
        self._audio_metronome.configure(device, channels)
        self.config["metronome_audio_device"] = device
        self.config["metronome_audio_channels"] = channels
        self.config["metronome_kit"] = self.metronome_kit_var.get()
        self._metronome_latency_ms_cache = self._safe_int_var(
            self.metronome_latency_var, "_metronome_latency_ms_cache",
        )
        self.config["metronome_audio_latency_ms"] = self._metronome_latency_ms_cache
        save_config(self.config)

    def _on_metronome_audio_change_2(self) -> None:
        device = self.metronome_device_var_2.get()
        if device == self.METRONOME_DEFAULT_DEVICE_LABEL:
            device = ""
        try:
            channels = 1 if int(self.metronome_channels_var_2.get()) == 1 else 2
        except (tk.TclError, ValueError):
            channels = 2
        self._audio_metronome_2.set_kit(self.metronome_kit_var_2.get())
        self._audio_metronome_2.configure(device, channels)
        self.config["metronome_audio_device_2"] = device
        self.config["metronome_audio_channels_2"] = channels
        self.config["metronome_kit_2"] = self.metronome_kit_var_2.get()
        self._metronome_latency_ms_cache_2 = self._safe_int_var(
            self.metronome_latency_var_2, "_metronome_latency_ms_cache_2",
        )
        self.config["metronome_audio_latency_ms_2"] = self._metronome_latency_ms_cache_2
        save_config(self.config)

    # --------------------------------------------------------- Ableton Link --
    def _ensure_link(self) -> AbletonLink | None:
        if self.link is not None:
            return self.link
        if self.link_error is not None:
            return None
        try:
            link = AbletonLink(120.0)
            link.enable(True)
        except LinkUnavailable as exc:
            self.link_error = str(exc)
            self.status_label.config(text=f"Ableton Link indisponible : {exc}")
            return None
        self.link = link
        return self.link

    def _set_tempo(self) -> None:
        """Impose le tempo choisi à tous les pairs Link (dont Ableton Live) en
        temps réel, à chaque changement du champ (voir trace_add dans _build_ui) —
        le champ n'est visible qu'en mode Link. Déclenché uniquement à la
        validation (flèches, Entrée, perte du focus) ou par un mouvement du
        fader 16 (_apply_tempo_fader/_reset_tempo_fader) : jamais à chaque
        touche tapée (voir _build_ui)."""
        try:
            bpm = float(self.set_tempo_var.get())
        except (tk.TclError, ValueError):
            return
        link = self._ensure_link()
        if link is None:
            return
        link.set_tempo(bpm)
        self._tempo_last_sent_bpm = bpm

    def _update_tempo_display(self, bpm: float) -> None:
        """Reflète dans le champ de tempo un changement observé côté Link (ex.
        tempo changé à la souris dans Live, ou par un autre pair) : ignore les
        échos de notre propre dernier envoi (_tempo_last_sent_bpm) pour éviter
        une boucle, et n'écrit dans le champ que si la valeur diffère
        réellement de ce qui y est déjà affiché. Ne touche jamais au champ
        pendant que l'utilisateur y tape (sinon le poll, ~30ms, écraserait sa
        saisie en cours avant même qu'il ait pu valider)."""
        if self.tempo_spinbox.focus_get() is self.tempo_spinbox:
            return
        if self._tempo_last_sent_bpm is not None and abs(bpm - self._tempo_last_sent_bpm) < 0.05:
            return
        try:
            current = float(self.set_tempo_var.get())
        except (tk.TclError, ValueError):
            current = None
        if current is not None and abs(bpm - current) < 0.05:
            return
        self.set_tempo_var.set(round(bpm, 1))

    def _update_tempo_reference(self, bpm: float) -> None:
        """Tient à jour le tempo de référence ("morceau chargé sans
        modification", position centrale du fader 16) à partir du tempo Link
        observé : tout changement qui ne vient pas de notre propre dernier
        envoi (_tempo_last_sent_bpm) est considéré comme un nouveau
        morceau/tempo de base (ex. lancement de scène, réglage manuel dans
        Live) et devient la nouvelle référence."""
        if self._tempo_reference_bpm is None:
            self._tempo_reference_bpm = bpm
            return
        if self._tempo_last_sent_bpm is not None and abs(bpm - self._tempo_last_sent_bpm) < 0.05:
            return
        if abs(bpm - self._tempo_reference_bpm) > 0.05:
            self._tempo_reference_bpm = bpm

    def _on_link_tempo_observed(self, bpm: float) -> None:
        """Point d'entrée unique appelé à chaque poll avec le BPM Link actuel
        (indépendamment du mode d'affichage midi/link) : met à jour à la fois
        le tempo de référence du fader 16 et l'affichage du champ de tempo."""
        self._update_tempo_reference(bpm)
        self._update_tempo_display(bpm)

    def _poll_tempo_fader(self) -> None:
        """Consomme les positions brutes du fader 16 (voir _tempo_fader_queue) :
        seule la dernière valeur reçue depuis le dernier passage est appliquée
        (évite de spammer Link pendant un mouvement rapide du fader)."""
        raw = None
        try:
            while True:
                raw = self._tempo_fader_queue.get_nowait()
        except queue.Empty:
            pass
        if raw is not None:
            self._apply_tempo_fader(raw)

    def _apply_tempo_fader(self, raw: int) -> None:
        """Traduit la position brute (0-16383) du fader 16, façon pitch de
        platine Pioneer DJ MK2 : au centre (~8192) le tempo de référence est
        inchangé, monter/descendre l'augmente/diminue sur la plage choisie
        (± 3/6/10/20/100 % autour du tempo de référence)."""
        mode = self.config.get("tempo_fader_range", "6")
        try:
            pct = float(mode) / 100.0
        except ValueError:
            pct = 0.06
        frac = max(-1.0, min(1.0, (raw - 8191.5) / 8191.5))
        reference = self._tempo_reference_bpm if self._tempo_reference_bpm else 120.0
        new_bpm = reference * (1.0 + frac * pct)
        self.set_tempo_var.set(round(new_bpm, 1))
        self._set_tempo()
        self._tempo_fader_local_time = time.monotonic()
        # Réaffirme tout de suite la position que la main vient de donner :
        # sans cet écho, la console reprend le fader vers la dernière cible
        # qu'on lui avait commandée dès qu'on relâche le toucher (le moteur a
        # besoin d'une confirmation MIDI de la nouvelle position, pas
        # seulement de recevoir la position brute côté entrée).
        self.hui_bridge_2.send_tempo_fader_feedback(raw)
        self._tempo_fader_refresh_time = self._tempo_fader_local_time
        self._tempo_fader_last_raw_sent = raw

    def _tempo_fader_raw_for_bpm(self, bpm: float) -> int:
        """Inverse de _apply_tempo_fader : position brute (0-16383) du fader
        16 correspondant au tempo donné, sur la plage actuellement choisie."""
        mode = self.config.get("tempo_fader_range", "6")
        try:
            pct = float(mode) / 100.0
        except ValueError:
            pct = 0.06
        reference = self._tempo_reference_bpm if self._tempo_reference_bpm else 120.0
        frac = (bpm / reference - 1.0) / pct if pct and reference else 0.0
        frac = max(-1.0, min(1.0, frac))
        return max(0, min(16383, round(8191.5 + frac * 8191.5)))

    def _poll_tempo_fader_keepalive(self) -> None:
        """Réaffirme au fader 16 la position correspondant au tempo
        actuellement affiché (Live, logiciel ou fader lui-même), pour
        contrer le retour automatique du moteur de la console en l'absence de
        confirmation MIDI — sans jamais lutter contre un geste récent de la
        main (voir TEMPO_FADER_KEEPALIVE_HOLDOFF_S). Un vrai changement de
        position est renvoyé tout de suite (pas de latence à attendre la
        prochaine réaffirmation) ; seule la réaffirmation périodique d'une
        position inchangée est limitée à INTERVAL_S."""
        now = time.monotonic()
        if now - self._tempo_fader_local_time < self.TEMPO_FADER_KEEPALIVE_HOLDOFF_S:
            return
        try:
            bpm = float(self.set_tempo_var.get())
        except (tk.TclError, ValueError):
            return
        raw = self._tempo_fader_raw_for_bpm(bpm)
        if raw == self._tempo_fader_last_raw_sent and now - self._tempo_fader_refresh_time < self.TEMPO_FADER_KEEPALIVE_INTERVAL_S:
            return
        self.hui_bridge_2.send_tempo_fader_feedback(raw)
        self._tempo_fader_refresh_time = now
        self._tempo_fader_last_raw_sent = raw

    def _poll_tempo_reset(self) -> None:
        """Consomme les demandes de rappel du tempo d'origine (bouton Mute de
        la tranche tempo, envoi simple voir hui_bridge.py) : (re)programme le
        rappel au prochain temps, sans jamais l'appliquer immédiatement."""
        triggered = False
        try:
            while True:
                self._tempo_reset_queue.get_nowait()
                triggered = True
        except queue.Empty:
            pass
        if triggered:
            self._schedule_tempo_reset()

    def _schedule_tempo_reset(self) -> None:
        """Calcule le délai jusqu'au prochain temps directement à partir de la
        phase/tempo Link courants (déterministe, indépendant du rythme des
        polls) et y programme l'application du rappel via root.after. Annule
        d'abord tout rappel déjà programmé : un second appui (ou le 2e envoi
        du même appui physique) reprogramme au lieu de s'empiler."""
        if self._tempo_reset_after_id is not None:
            self.root.after_cancel(self._tempo_reset_after_id)
            self._tempo_reset_after_id = None
        delay_ms = 0.0
        link = self._ensure_link()
        if link is not None:
            snapshot = link.snapshot(quantum=1.0)
            bpm = snapshot["bpm"] if snapshot["bpm"] > 0 else 120.0
            frac = snapshot["phase"] % 1.0
            delay_ms = max(0.0, (1.0 - frac) * (60.0 / bpm) * 1000.0)
        self._tempo_reset_after_id = self.root.after(int(delay_ms), self._apply_scheduled_tempo_reset)

    def _apply_scheduled_tempo_reset(self) -> None:
        self._tempo_reset_after_id = None
        self._reset_tempo_fader()

    def _reset_tempo_fader(self) -> None:
        origin = self._scene_origin_tempo if self._scene_origin_tempo is not None else self._tempo_reference_bpm
        if origin is None:
            return
        self.set_tempo_var.set(round(origin, 1))
        self._set_tempo()

    def _jump_beats(self, beats: int) -> None:
        """Décale de `beats` temps le clip en cours de lecture de chaque
        piste, sans déplacer le compteur général de Live (voir README.md) —
        indépendant du mode Link/MIDI choisi pour l'affichage."""
        try:
            self.live_osc.jump_tracks_by(beats)
        except OSError as exc:
            self.status_label.config(text=f"Erreur OSC (AbletonOSC lancé côté Live ?) : {exc}")

    # --------------------------------------------------- Scènes (Live) --
    def _refresh_scene_state(self) -> None:
        try:
            self.live_osc.get_num_scenes()
            self.live_osc.get_selected_scene()
        except OSError as exc:
            self.scene_name_label.config(text=f"Erreur OSC : {exc}")

    def _ping_live(self) -> None:
        """Sonde Live toutes les 2s (voir LiveOSC.ping) : si aucune réponse
        n'arrive pendant ~6s, on considère Live absent, pour détecter sa
        (re)connexion (ex. lancé après CLIC) via _poll_scene_replies et
        renvoyer les abonnements perdus."""
        try:
            self.live_osc.ping()
        except OSError:
            pass
        if self._live_available and time.monotonic() - self._live_last_seen > 6.0:
            self._live_available = False
        self.root.after(2000, self._ping_live)

    def _on_live_available(self) -> None:
        """Appelé quand Live répond pour la première fois (ou de nouveau après
        une coupure/un lancement tardif) : renvoie les abonnements HUI et
        métronome perdus au démarrage si Live n'était pas encore là, et
        rafraîchit l'état des scènes."""
        if self.hui_bridge.port_name:
            self._set_hui_listen(0, listen=True)
        if self.hui_bridge_2.port_name:
            self._set_hui_listen(8, listen=True)
        try:
            self.live_osc.start_listen_signature_numerator()
            self.live_osc.start_listen_signature_denominator()
        except OSError:
            pass
        self._refresh_scene_state()
        try:
            self.live_osc.get_num_tracks()
        except OSError:
            pass

    def _reset_faders_beyond(self, num_tracks: int) -> None:
        """Remet à zéro (fader en bas, mute éteint, nom vide) les tranches HUI
        au-delà du nombre réel de pistes du projet courant : sans piste, Live
        ne renvoie jamais de retour pour ces index (erreur "Index out of
        range", déjà filtrée plus bas), donc le fader physique resterait
        bloqué sur sa dernière position connue (ex. venant d'un projet
        précédent avec plus de pistes)."""
        for track in range(max(0, num_tracks), 16):
            for bridge in (self.hui_bridge, self.hui_bridge_2):
                bridge.send_volume_feedback(track, 0.0)
                bridge.send_mute_feedback(track, False)
                bridge.send_name_feedback(track, "")

    def _scene_step(self, delta: int) -> None:
        if self._scene_index is None or self._scene_count is None:
            self._refresh_scene_state()
            return
        new_index = max(0, min(self._scene_count - 1, self._scene_index + delta))
        if new_index == self._scene_index:
            return
        self._scene_index = new_index
        # Réinitialise tout de suite le vert/rouge du lancement précédent : le
        # nom/statut exacts de la nouvelle scène n'arriveront qu'après l'aller-
        # retour OSC, sinon on voit brièvement l'ancienne scène encore verte.
        self.scene_name_label.config(fg=SCENE_NOT_LAUNCHED)
        self.shared_state.set_scene_name("À SUIVRE")
        self.shared_state.set_scene_launched(False)
        # Changer de sélection annule le comptage de mesures en cours : il ne
        # doit reprendre qu'au lancement réel de la scène qui sera affichée.
        self._bar_count = None
        self._bar_count_prev_beat = None
        self._bar_count_signature_pushed_for = None
        self._awaiting_bar_start = False
        self.bar_count_label.config(text="")
        self.shared_state.set_bar_count(None)
        # Idem pour la feuille de scène (COUNT/HIGHLIGHT/LABEL) : elle ne
        # s'applique qu'à la scène effectivement lancée, pas à une simple
        # navigation.
        self._scene_sheet = None
        self._scene_sheet_row = None
        self._scene_label_sticky = ""
        self.scene_label_label.config(text="")
        self.shared_state.set_scene_label("")
        self._lyrics_sheet = None
        self._lyrics_bar_beats = []
        self._hide_lyrics_scroll_items()
        try:
            self.live_osc.set_selected_scene(new_index)
            self.live_osc.get_scene_name(new_index)
        except OSError as exc:
            self.scene_name_label.config(text=f"Erreur OSC : {exc}")

    def _scene_launch(self) -> None:
        if self.mode_var.get() == "offline":
            self._launch_offline_song()
            return
        # fire_selected (Scene.fire_as_selected) avance aussi la sélection vers
        # la scène suivante côté Live : on utilise fire(index) pour ne lancer
        # que la scène affichée, sans bouger la sélection.
        if self._scene_index is None:
            return
        try:
            self.live_osc.fire_scene(self._scene_index)
            # Fader 16 remis à 0% de modification (position centrale) à
            # chaque lancement de scène, feuille de morceau ou tempo seul.
            self.hui_bridge_2.send_tempo_fader_feedback(self.TEMPO_FADER_CENTER_RAW)
            self._tempo_fader_refresh_time = time.monotonic()
            self._tempo_fader_last_raw_sent = self.TEMPO_FADER_CENTER_RAW
            # Convention "tempo seul" : une scène nommée juste avec des
            # chiffres ne contient pas de clip, donc fire() ne démarre pas le
            # transport tout seul — on le déclenche explicitement. Ce n'est
            # qu'un réglage de tempo, pas un vrai lancement : pas de vert, pas
            # de flash, pas d'agrandissement (réservés aux scènes nommées).
            if self._scene_name.strip().isdigit():
                self.live_osc.start_playing()
                # Scène "tempo seul" : aucune feuille de scène ne s'applique.
                self._scene_sheet = None
                self._scene_sheet_row = None
                self._scene_label_sticky = ""
                self.scene_label_label.config(text="")
                self.shared_state.set_scene_label("")
                self._lyrics_sheet = None
                self._lyrics_bar_beats = []
                self._hide_lyrics_scroll_items()
                # 2s après le lancement d'une scène "tempo seul", on
                # sélectionne automatiquement la scène suivante (comme un
                # appui sur ▼), prête à être lancée avec le bouton ▶.
                launched_index = self._scene_index
                self.root.after(2000, lambda: self._auto_advance_scene(launched_index))
            else:
                self.scene_name_label.config(fg=SCENE_LAUNCHED)
                # Le smartphone n'affiche le vrai titre qu'à ce moment (pas de
                # spoiler avant l'appui) : À SUIVRE jusqu'ici, nom révélé ici.
                self.shared_state.set_scene_name(self._scene_name)
                self.shared_state.set_scene_launched(True)
                if self._scene_flash_shown_for != self._scene_index:
                    self._scene_flash_shown_for = self._scene_index
                    self._scene_flash_start = time.monotonic()
                # Le compteur de mesures démarre au premier vrai temps 1 qui
                # suit ce lancement (voir _update_bar_count), pas à l'appui.
                self._bar_count = None
                self._bar_count_prev_beat = None
                self._bar_count_signature_pushed_for = None
                self._awaiting_bar_start = True
                self.bar_count_label.config(text="")
                self.shared_state.set_bar_count(None)
                # Feuille de scène XLSX (<nom de scène>.xlsx, voir
                # scene_sheet.py) : None si le fichier n'existe pas, aucun
                # changement de comportement dans ce cas. "GOTO" (label
                # choisi, moins PREROLL mesures) fixe le point de départ du
                # comptage ; on applique tout de suite la ligne correspondante
                # (COUNT/HIGHLIGHT/LABEL) pour que le tout premier temps
                # affiché soit déjà correct, sans attendre une mesure de retard.
                self._scene_sheet = load_scene_sheet(
                    self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: print(f"[Feuille de scène] {msg}"),
                )
                self._lyrics_sheet = load_lyrics(
                    self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: print(f"[Paroles] {msg}"),
                )
                self.shared_state.set_lyrics_lines(self._lyrics_sheet.lines if self._lyrics_sheet is not None else [])
                self._lyrics_bar_beats = []
                label_bar = (
                    self._scene_sheet.bar_for_label(self.goto_label_var.get())
                    if self._scene_sheet is not None else None
                )
                try:
                    preroll = max(0, int(self.preroll_var.get()))
                except (tk.TclError, ValueError):
                    preroll = 0
                self._bar_count_start = max(1, label_bar - preroll) if label_bar is not None else 1
                self._scene_label_sticky = (
                    self._scene_sheet.label_at_or_before(self._bar_count_start)
                    if self._scene_sheet is not None else ""
                )
                self._apply_scene_sheet_row(self._bar_count_start)
                # Live ne connaît pas la notion de "mesure" : on saute réellement
                # le clip qu'on vient de lancer du nombre de temps équivalent
                # (somme des COUNT des mesures précédentes), pour qu'il démarre
                # bien à self._bar_count_start plutôt qu'à son tout début.
                # jump_in_running_session_clip n'agit que sur un clip déjà
                # RUNNING : on attend donc le premier vrai temps 1 (voir
                # _update_bar_count) avant d'envoyer le saut, la quantification
                # de lancement de Live pouvant retarder le vrai démarrage du
                # clip d'une durée variable (pas un délai fixe fiable).
                jump_beats = sum(self._count_for_mes(m) for m in range(1, self._bar_count_start))
                self._pending_goto_jump_beats = jump_beats or None
        except OSError as exc:
            self.scene_name_label.config(text=f"Erreur OSC : {exc}")

    def _auto_advance_scene(self, expected_index: int) -> None:
        """Callback différée de _scene_launch : n'avance que si on est
        toujours sur la scène lancée il y a 2s (pas de navigation manuelle
        entretemps)."""
        if self._scene_index == expected_index:
            self._scene_step(1)

    def _stop_return_to_start(self) -> None:
        """Simule un double appui sur Stop dans Live : arrête la lecture (et les
        clips en cours, comportement natif normal), puis (comme au 2e Stop) ramène
        le curseur à 1:1:1, en attente d'un start de scène."""
        self._metronome_end_muted = False
        if self.mode_var.get() == "offline":
            self._offline_playing = False
            self._offline_last_beat_time = 0.0
            self._bar_count = None
            self._bar_count_prev_beat = None
            self._awaiting_bar_start = False
            self.bar_count_label.config(text="")
            self.scene_name_label.config(text=self._scene_name or "Aucun morceau", fg=SCENE_NOT_LAUNCHED)
            self.shared_state.set_bar_count(None)
            self.shared_state.set_scene_launched(False)
            self.status_label.config(text="Arrêté (Offline)")
            return
        try:
            self.live_osc.stop_playing()
            self.root.after(120, self.live_osc.stop_playing)
        except OSError as exc:
            self.status_label.config(text=f"Erreur OSC : {exc}")

    def _send_controller_led(self, action: str, on: bool) -> None:
        """Renvoie l'état on/off d'une action au contrôleur MIDI (ex. LED des
        touches USER DEFINED de la console), si elle est mappée sur une note."""
        mapping = self.controller_map.get(action)
        if not mapping or mapping[0] != "note":
            return
        _kind, channel, note = mapping
        self.controller.send_note_feedback(channel, note, on)

    def _toggle_metronome(self) -> None:
        """Bascule notre métronome audio local (clic.wav via la carte son
        choisie, voir audio_metronome.py) — ne commande plus le métronome
        interne de Live."""
        self._metronome_on = not self._metronome_on
        self._last_played_beat = None
        self._audio_metronome.set_enabled(self._metronome_on)
        self._set_action_active("metronome", self._metronome_on)
        self._send_controller_led("metronome", self._metronome_on)

    def _toggle_metronome_2(self) -> None:
        """Bascule la deuxième sortie métronome (voir _build_ui
        metronome_frame_2), indépendante de la première (2e musicien)."""
        self._metronome_on_2 = not self._metronome_on_2
        self._last_played_beat_2 = None
        self._audio_metronome_2.set_enabled(self._metronome_on_2)
        self._set_action_active("metronome_2", self._metronome_on_2)
        self._send_controller_led("metronome_2", self._metronome_on_2)

    def _poll_scene_replies(self) -> None:
        for address, args in self.live_osc.poll_replies():
            if address == "/live/startup":
                # Renvoyé par Live à chaque (re)chargement de projet, même si
                # Live tournait déjà et répondait au ping /live/test : c'est
                # le seul signal fiable pour détecter un changement de projet.
                self._live_last_seen = time.monotonic()
                self._live_available = True
                self._on_live_available()
            elif address == "/live/test":
                self._live_last_seen = time.monotonic()
                if not self._live_available:
                    self._live_available = True
                    self._on_live_available()
            elif address == "/live/error":
                # "Index out of range" est attendu en permanence si le pont
                # HUI écoute des pistes au-delà du nombre réel de pistes du
                # projet (ex. 16 canaux HUI pour un projet à moins de 16
                # pistes) : ce n'est pas une vraie erreur, on ne l'affiche pas.
                if not any("Index out of range" in str(arg) for arg in args):
                    print(f"[OSC] erreur renvoyée par AbletonOSC : {args}")
            elif address == "/live/song/get/num_tracks":
                self._reset_faders_beyond(int(args[0]))
            elif address == "/live/track/get/volume":
                track_index, volume = int(args[0]), float(args[1])
                self.hui_bridge.send_volume_feedback(track_index, volume)
                self.hui_bridge_2.send_volume_feedback(track_index, volume)
            elif address == "/live/track/get/mute":
                track_index, muted = int(args[0]), bool(args[1])
                self.hui_bridge.send_mute_feedback(track_index, muted)
                self.hui_bridge_2.send_mute_feedback(track_index, muted)
            elif address == "/live/track/get/name":
                track_index, name = int(args[0]), (args[1] or "")
                self.hui_bridge.send_name_feedback(track_index, name)
                self.hui_bridge_2.send_name_feedback(track_index, name)
            elif address == "/live/song/get/num_scenes":
                self._scene_count = int(args[0])
            elif address == "/live/view/get/selected_scene":
                self._scene_index = int(args[0])
                self.live_osc.get_scene_name(self._scene_index)
            elif address == "/live/scene/get/name":
                index, name = int(args[0]), (args[1] or "")
                if index == self._scene_index:
                    # Mémorise le GOTO choisi pour la scène qu'on quitte, avant
                    # qu'il ne soit écrasé par _refresh_goto_labels ci-dessous.
                    if self._scene_name and not self._scene_name.strip().isdigit():
                        self._goto_label_by_scene[self._scene_name] = self.goto_label_var.get()
                    self._scene_name = name
                    self._refresh_goto_labels()
                    # Convention du set : une scène nommée juste "84" ne fait
                    # que régler le tempo, la scène suivante contient le
                    # morceau prêt à être lancé — on affiche donc son nom
                    # entre parenthèses (ex. "84 (Briser)").
                    if name.strip().isdigit() and self._scene_count and index + 1 < self._scene_count:
                        self.live_osc.get_scene_name(index + 1)
                    else:
                        self._update_scene_label()
                    # Tempo d'origine du morceau : lu dans le nom de la scène
                    # PRÉCÉDENTE (convention du set, ex. morceau "OVLM" précédé
                    # d'une scène "100") — seulement pertinent si la scène
                    # sélectionnée est bien un morceau, pas une scène "tempo seul".
                    self._scene_origin_tempo = None
                    if not name.strip().isdigit() and index > 0:
                        self.live_osc.get_scene_name(index - 1)
                elif (
                    self._scene_index is not None
                    and index == self._scene_index + 1
                    and self._scene_name.strip().isdigit()
                ):
                    self._update_scene_label(next_name=name)
                elif (
                    self._scene_index is not None
                    and index == self._scene_index - 1
                    and not self._scene_name.strip().isdigit()
                    and name.strip().isdigit()
                ):
                    self._scene_origin_tempo = float(name.strip())


    def _update_scene_label(self, next_name: str | None = None) -> None:
        name = self._scene_name or "(sans nom)"
        if next_name:
            name = f"{name} ({next_name})"
        text = f"{self._scene_index + 1}/{self._scene_count} : {name}"
        # Nouvelle sélection de scène : rouge (pas encore lancée), même règle
        # de couleur que sur le web (les scènes numériques restent rouge, cf.
        # _scene_launch qui ne passe jamais au vert pour elles).
        self.scene_name_label.config(text=text, fg=SCENE_NOT_LAUNCHED)
        # Page web : "À SUIVRE (Titre)" dans tous les cas — le titre entre
        # parenthèses est celui du morceau à jouer (le suivant si scène
        # "tempo seul", ou la scène elle-même si elle porte déjà le vrai nom).
        display_name = next_name if self._scene_name.strip().isdigit() else self._scene_name
        web_name = f"À SUIVRE ({display_name})" if display_name else "À SUIVRE"
        self.shared_state.set_scene_name(web_name)

    def _refresh_goto_labels(self) -> None:
        """Recharge en prévisualisation la feuille de la scène SÉLECTIONNÉE
        (pas forcément lancée) pour peupler le sélecteur GOTO, INTRO choisi
        par défaut si présent. N'écrase jamais self._scene_sheet (celle de la
        scène réellement en cours de lecture)."""
        sheet = (
            load_scene_sheet(self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: None)
            if not self._scene_name.strip().isdigit() else None
        )
        self._scene_sheet_preview = sheet
        labels = sheet.labels() if sheet is not None else []
        self.goto_label_combo["values"] = labels
        remembered = self._goto_label_by_scene.get(self._scene_name)
        if remembered in labels:
            self.goto_label_var.set(remembered)
        elif "INTRO" in labels:
            self.goto_label_var.set("INTRO")
        elif labels:
            self.goto_label_var.set(labels[0])
        else:
            self.goto_label_var.set("")

    def _count_for_mes(self, mes: int) -> int:
        """COUNT (temps par mesure) prévu pour la mesure `mes` d'après la
        feuille de scène, ou la valeur configurée par défaut si absente/hors
        feuille."""
        row = self._scene_sheet.get(mes) if self._scene_sheet is not None else None
        return row.count if row is not None and row.count else self.config["beats_per_bar"]

    def _cumulative_beats_at_bar(self, mes: int) -> float:
        """Temps écoulés (depuis la mesure 1) au tout début de la mesure
        `mes` (mes=1 -> 0.0), en tenant compte du COUNT de chaque mesure
        (feuille de scène) : complété au fil de l'eau dans
        self._lyrics_bar_beats (jamais recalculé depuis le début à chaque
        frame), voir _draw_lyrics_scroll."""
        cache = self._lyrics_bar_beats
        if not cache:
            cache.append(0.0)
        while len(cache) < mes:
            bar_just_started = len(cache)  # mesure dont le début vient d'être ajouté
            cache.append(cache[-1] + self._count_for_mes(bar_just_started))
        return cache[mes - 1]

    def _push_lyrics_state(self, beat: int, fractional: float) -> None:
        """Pousse vers la page web la position continue dans le morceau (même
        calcul que _draw_lyrics_scroll) : elle fait ainsi défiler les paroles
        à la même vitesse que le grand écran (le texte lui-même est poussé
        une seule fois par morceau, voir set_lyrics_lines)."""
        if self._lyrics_sheet is None or self._bar_count is None:
            self.shared_state.set_lyrics_position(None)
            return
        song_beat = self._cumulative_beats_at_bar(self._bar_count) + (beat - 1) + fractional
        self.shared_state.set_lyrics_position(song_beat)

    def _push_live_time_signature(self, count: int) -> None:
        """Pousse `count` comme signature rythmique (numérateur) à Live, mais
        seulement s'il diffère du dernier envoyé (Live n'a qu'une seule
        signature globale, pas "par mesure" comme le tableur) : jamais
        d'envoi OSC quand la valeur ne change pas."""
        if self.mode_var.get() != "offline" and count != self._live_time_signature_sent:
            self.live_osc.set_time_signature(count)
            self._live_time_signature_sent = count

    def _apply_scene_sheet_row(self, mes: int) -> None:
        """Applique la ligne de la feuille de scène (scene_sheet.py) pour la
        mesure `mes` : COUNT (temps par mesure, avec retour à la valeur
        configurée si absent), HIGHLIGHT (consommé par _update_display) et
        LABEL ("collant" : ne s'efface que quand une nouvelle valeur non vide
        arrive). Sans feuille (ou mesure hors feuille), comportement normal."""
        row = self._scene_sheet.get(mes) if self._scene_sheet is not None else None
        self._scene_sheet_row = row
        count = self._count_for_mes(mes)
        self.beats_var.set(count)
        self.midi_state.beats_per_bar = count
        self._push_live_time_signature(count)
        if row is not None and row.label:
            self._scene_label_sticky = row.label
        self.scene_label_label.config(text=self._scene_label_sticky)
        self.shared_state.set_scene_label(self._scene_label_sticky)
        if self._scene_label_sticky.strip().upper() == "END":
            self._metronome_end_muted = True

    # -------------------------------------------------------- Boucle poll --
    def _update_link_peers_label(self, num_peers: int) -> None:
        """CLIC ne peut pas activer Link à la place de l'utilisateur dans
        Live (réglage interne à Live, non exposé par l'API Link ni par
        AbletonOSC) : on se contente d'avertir immédiatement si 0 pair."""
        if num_peers >= 1:
            self.link_peers_label.config(text=f"Pairs Link connectés : {num_peers}", fg=FG_TEXT)
            self._link_zero_peers_since = None
            self._link_dialog_shown = False
            self._close_link_dialog()
            return
        self.link_peers_label.config(
            text="Pairs Link connectés : 0 — active Link dans Ableton Live "
            "(Préférences > Link/Tempo/MIDI)",
            fg=SCENE_NOT_LAUNCHED,
        )
        if self._link_zero_peers_since is None:
            self._link_zero_peers_since = time.monotonic()
        if not self._link_dialog_shown and time.monotonic() - self._link_zero_peers_since > 5.0:
            self._link_dialog_shown = True
            self._show_link_dialog()

    def _show_link_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Link non détecté")
        dialog.configure(bg=BG_IDLE)
        tk.Label(
            dialog, bg=BG_IDLE, fg=FG_TEXT, justify="left", wraplength=360, font=("Helvetica", 13),
            text="Aucun pair Link détecté depuis 5 secondes.\n\n"
            "Active Link dans Ableton Live (Préférences > Link/Tempo/MIDI).\n\n"
            "Cette fenêtre se referme automatiquement dès qu'un pair est détecté.",
        ).pack(padx=20, pady=(20, 10))
        tk.Button(dialog, text="Fermer", command=self._close_link_dialog).pack(pady=(0, 16))
        dialog.protocol("WM_DELETE_WINDOW", self._close_link_dialog)
        self._link_dialog = dialog

    def _close_link_dialog(self) -> None:
        if self._link_dialog is not None:
            self._link_dialog.destroy()
            self._link_dialog = None

    def _update_bar_count(self, connected: bool, beat: int, fractional: float = 0.0) -> None:
        """Compte les mesures depuis le premier vrai temps 1 qui suit le
        lancement du morceau en cours (armé par _scene_launch pour les vraies
        scènes uniquement, jamais pour les scènes "tempo seul"/préparation)."""
        if not connected:
            self._bar_count_prev_beat = None
            self._bar_count_signature_pushed_for = None
            return
        if self._awaiting_bar_start:
            if beat == 1:
                self._awaiting_bar_start = False
                self._bar_count = self._bar_count_start
                self._bar_count_prev_beat = 1
                self.bar_count_label.config(text=f"Mesure {self._bar_count}")
                self.shared_state.set_bar_count(self._bar_count)
                if self._pending_goto_jump_beats:
                    # Léger délai : une piste dont le clip démarre tout juste
                    # à cet instant précis (pas déjà en lecture depuis une
                    # scène précédente) peut ne pas encore être "is_playing"
                    # côté Live au moment même du vrai temps 1, et
                    # jump_in_running_session_clip échoue alors silencieusement
                    # (pas d'erreur OSC) pour cette piste seulement, qui
                    # redémarre donc du tout début à chaque lancement.
                    jump_beats = self._pending_goto_jump_beats
                    self.root.after(50, lambda b=jump_beats: self._jump_beats(b))
                self._pending_goto_jump_beats = None
            return
        if self._bar_count is not None and beat == 1 and self._bar_count_prev_beat != 1:
            self._bar_count += 1
            self._apply_scene_sheet_row(self._bar_count)
            self.bar_count_label.config(text=f"Mesure {self._bar_count}")
            self.shared_state.set_bar_count(self._bar_count)
        elif (
            self._bar_count is not None
            and beat == self.midi_state.beats_per_bar
            and fractional >= 0.5
            and self._bar_count_signature_pushed_for != self._bar_count
        ):
            # Anticipe le changement de signature côté Live : on le pousse à
            # la moitié du dernier temps de la mesure en cours (pas à son
            # tout début), pour qu'il ait le temps d'être appliqué avant le
            # changement réel tout en restant le plus proche possible de la
            # bascule. Ne touche ni beats_var ni la feuille/highlight
            # locaux, qui basculent toujours exactement au temps 1 suivant.
            self._push_live_time_signature(self._count_for_mes(self._bar_count + 1))
            self._bar_count_signature_pushed_for = self._bar_count
        self._bar_count_prev_beat = beat

    def _poll(self) -> None:
        self._poll_controller()
        self._poll_scene_replies()
        self._poll_tempo_fader()
        self._poll_tempo_fader_keepalive()
        self._poll_tempo_reset()
        if self.mode_var.get() == "midi":
            try:
                while True:
                    message, delta_time, _ts = self._event_queue.get_nowait()
                    self.midi_state.handle_message(message, delta_time)
            except queue.Empty:
                pass
            # Pour le MIDI Clock, "running" (présence du clock) est fiable :
            # sert à la fois de signal "connecté" et d'état lecture/arrêt.
            connected = self.midi_state.running
            if connected and not self._was_connected:
                self._awaiting_downbeat = True
            self._was_connected = connected
            phase = project_phase(
                self.midi_state.phase(), self.midi_state.bpm, connected,
                self._safe_int_var(self.latency_var, "_latency_ms_cache"),
            )
            beat = int(phase % self.midi_state.beats_per_bar) + 1
            if connected and self._awaiting_downbeat and beat == 1:
                self._awaiting_downbeat = False
            running = connected  # présence réelle du clock, avant masquage
            connected = connected and not self._awaiting_downbeat
            self._update_bar_count(connected, beat, phase % 1.0)
            self._update_display(beat, self.midi_state.beats_per_bar, phase % 1.0, self.midi_state.bpm, connected, running)
            self.shared_state.update(
                self.midi_state.phase(), self.midi_state.beats_per_bar,
                self.midi_state.bpm, connected, running, "midi",
            )
            self._push_lyrics_state(beat, phase % 1.0)
            # Le fader 16 pilote le tempo via Link indépendamment du mode
            # d'affichage choisi (comme les boutons -1/+1) : on garde le tempo
            # de référence et le champ de tempo à jour même si l'affichage
            # courant est en MIDI Clock.
            tempo_link = self._ensure_link()
            if tempo_link is not None:
                self._on_link_tempo_observed(tempo_link.snapshot(quantum=1.0)["bpm"])
        elif self.mode_var.get() == "offline":
            try:
                bpm = max(20.0, min(300.0, float(self.offline_tempo_var.get())))
            except (tk.TclError, ValueError):
                bpm = 120.0
            quantum = float(max(1, self._safe_int_var(self.beats_var, "_beats_per_bar_cache")))
            if self._offline_playing:
                now = time.monotonic()
                if self._offline_last_beat_time == 0.0:
                    self._offline_last_beat_time = now
                    self._offline_beat_in_bar = 1
                    fractional = 0.0
                else:
                    beat_duration = 60.0 / bpm
                    elapsed = now - self._offline_last_beat_time
                    fractional = (elapsed % beat_duration) / beat_duration
                    full_beats = int(elapsed / beat_duration)
                    if full_beats > 0:
                        self._offline_beat_in_bar = (
                            (self._offline_beat_in_bar - 1 + full_beats) % int(quantum)
                        ) + 1
                        self._offline_last_beat_time += full_beats * beat_duration
                self._update_bar_count(True, self._offline_beat_in_bar, fractional)
                self._update_display(
                    self._offline_beat_in_bar, int(quantum), fractional, bpm, True, True,
                )
                self.shared_state.update(fractional + (self._offline_beat_in_bar - 1), quantum, bpm, True, True, "offline")
                self._push_lyrics_state(self._offline_beat_in_bar, fractional)
            else:
                self._update_display(1, int(quantum), 0.0, None, False, False)
                self.shared_state.update(0.0, quantum, None, False, False, "offline")
                self._push_lyrics_state(1, 0.0)
        else:
            link = self._ensure_link()
            if link is not None:
                quantum = float(max(1, self._safe_int_var(self.beats_var, "_beats_per_bar_cache")))
                snapshot = link.snapshot(quantum=quantum)
                # On n'affiche le comptage qu'une fois le signal START de Link
                # reçu (is_playing) : la seule présence de pairs ne suffit pas,
                # sinon le compteur démarre au lancement même sans lecture.
                connected = link.num_peers >= 1 and snapshot["is_playing"]
                if connected and not self._was_connected:
                    self._awaiting_downbeat = True
                    self._link_prev_fractional = None
                    self._link_last_update_time = None
                self._was_connected = connected
                phase = project_phase(
                    snapshot["phase"], snapshot["bpm"], connected,
                    self._safe_int_var(self.latency_var, "_latency_ms_cache"),
                )
                fractional = phase % 1.0
                if self._awaiting_downbeat or self._awaiting_bar_start:
                    # Détection du premier vrai temps 1 (reconnexion Link ou
                    # lancement de scène) : Live aligne réellement les clips
                    # lancés sur ce quantum (quantification globale, cf. Link),
                    # donc ce modulo est fiable ICI, à un instant donné.
                    # Contrairement au comptage en continu ci-dessous, il ne
                    # doit PAS servir une fois la mesure en cours (voir
                    # _link_beat_in_bar plus bas).
                    beat = int(phase % quantum) + 1
                    if connected and self._awaiting_downbeat and beat == 1:
                        self._awaiting_downbeat = False
                    if beat == 1:
                        self._link_beat_in_bar = 1
                        self._link_prev_fractional = fractional
                        self._link_last_update_time = time.monotonic()
                else:
                    # Comptage en continu, indépendant du modulo Link : le
                    # nombre de temps réellement écoulés depuis la dernière
                    # mise à jour est calculé à partir du tempo et du temps
                    # réel écoulé (pas seulement "un rebouclage détecté = +1"),
                    # pour rattraper d'un coup plusieurs temps sautés si
                    # _poll() a été gelé un moment (déplacement de souris sur
                    # macOS/Tk, voir _metronome_loop) — sinon le compteur
                    # prenait durablement du retard sur l'audio après un gel.
                    # Reboucle sur le COUNT courant (self.beats_var), donc
                    # correct même quand ce COUNT change d'une mesure à
                    # l'autre (feuille de scène, ex. 2 temps puis 4 temps), ce
                    # que ne permet pas phase % quantum (aligné sur la session
                    # Link depuis son tout début, pas sur le début de la
                    # mesure en cours).
                    now = time.monotonic()
                    if self._link_prev_fractional is not None and self._link_last_update_time is not None:
                        dt = now - self._link_last_update_time
                        elapsed_beats = snapshot["bpm"] / 60.0 * dt
                        wraps = round(elapsed_beats - (fractional - self._link_prev_fractional))
                        if wraps > 0:
                            self._link_beat_in_bar = ((self._link_beat_in_bar - 1 + wraps) % int(quantum)) + 1
                    self._link_prev_fractional = fractional
                    self._link_last_update_time = now
                    beat = self._link_beat_in_bar
                connected = connected and not self._awaiting_downbeat
                self._update_bar_count(connected, beat, fractional)
                self._update_display(beat, int(quantum), fractional, snapshot["bpm"], connected, snapshot["is_playing"])
                self._update_link_peers_label(link.num_peers)
                self._on_link_tempo_observed(snapshot["bpm"])
                # Phase relative à la mesure en cours (0..quantum), déjà bornée
                # par notre propre comptage : contrairement à la phase Link
                # brute, un modulo par quantum côté page web (voir
                # web_server.project_beat/compute) reste donc correct même
                # après un changement de COUNT.
                bar_relative_phase = (beat - 1) + fractional
                self.shared_state.update(
                    bar_relative_phase, quantum, snapshot["bpm"], connected, snapshot["is_playing"], "link",
                )
                self._push_lyrics_state(beat, fractional)

        self.root.after(30, self._poll)

    def _metronome_loop(self) -> None:
        """Déclenche les clics (mode Link uniquement) depuis un thread séparé
        de _poll()/after() : sur macOS, bouger la souris sur la fenêtre peut
        geler les timers Tcl "after()" (limitation connue de Tk/Cocoa — mode
        de suivi de la souris qui suspend les timers jusqu'à l'arrêt du
        mouvement), ce qui rendait le clic aussi irrégulier que l'affichage
        puisque tout passait par le même _poll(). Ne touche à aucun widget ni
        variable Tkinter (lecture non thread-safe) : seulement à Link (déjà
        protégé par son propre verrou, link_client.py) et à AudioMetronome
        (play() ne fait que déposer un buffer, lu par le thread de
        PortAudio) — le clic reste donc juste même si l'affichage se fige.
        En mode MIDI Clock, le clic reste déclenché depuis _update_display
        (l'état MIDI lui-même n'est mis à jour que par _poll).

        Le temps DANS la mesure (utilisé pour choisir click.wav/click_up.wav)
        est compté en continu (mêmes principes que self._link_beat_in_bar
        pour l'affichage), PAS via phase % quantum : cette dernière suppose
        une mesure de longueur constante depuis le tout début de la session
        Link, ce qui est faux dès qu'une feuille de scène change le COUNT
        d'une mesure à l'autre (le clic accentué décale alors durablement,
        ex. sur le temps 3 au lieu du temps 1 après une mesure à 2 temps).

        Comme pour l'affichage (_awaiting_downbeat), on attend le vrai temps 1
        (phase % quantum) avant de démarrer le comptage/le clic après chaque
        (re)connexion : is_playing peut passer à True un instant avant que la
        phase Link n'atteigne réellement le début de mesure, et forcer le
        clic sur le temps 1 à cet instant-là le faisait partir en avance sur
        l'affichage (qui, lui, attend le vrai temps 1)."""
        last_beat: int | None = None
        last_beat_2: int | None = None
        while not self._metronome_thread_stop.is_set():
            if self._mode_cache == "link" and self.link is not None:
                if self._metronome_on and not self._metronome_end_muted:
                    try:
                        quantum = float(max(1, self._beats_per_bar_cache))
                        # Latence positive = interroge Link dans le futur, donc
                        # déclenche le clic plus tôt (compense la latence de
                        # sortie audio, voir "Latence clic (ms)" dans l'UI).
                        offset_micros = int(self._metronome_latency_ms_cache * 1000)
                        snapshot = self.link.snapshot(quantum=quantum, offset_micros=offset_micros)
                        connected = self.link.num_peers >= 1 and snapshot["is_playing"]
                        if connected:
                            beat = self._metronome_next_beat(
                                quantum, snapshot, "_metronome_beat_in_bar",
                                "_metronome_prev_fractional", "_metronome_last_update_time",
                                "_metronome_awaiting_downbeat",
                            )
                            if beat is not None and beat != last_beat:
                                last_beat = beat
                                self._audio_metronome.play(beat)
                        else:
                            last_beat = None
                            self._metronome_awaiting_downbeat = True
                    except Exception:
                        pass
                else:
                    last_beat = None
                if self._metronome_on_2 and not self._metronome_end_muted:
                    try:
                        quantum = float(max(1, self._beats_per_bar_cache))
                        offset_micros_2 = int(self._metronome_latency_ms_cache_2 * 1000)
                        snapshot_2 = self.link.snapshot(quantum=quantum, offset_micros=offset_micros_2)
                        connected_2 = self.link.num_peers >= 1 and snapshot_2["is_playing"]
                        if connected_2:
                            beat_2 = self._metronome_next_beat(
                                quantum, snapshot_2, "_metronome_beat_in_bar_2",
                                "_metronome_prev_fractional_2", "_metronome_last_update_time_2",
                                "_metronome_awaiting_downbeat_2",
                            )
                            if beat_2 is not None and beat_2 != last_beat_2:
                                last_beat_2 = beat_2
                                self._audio_metronome_2.play(beat_2)
                        else:
                            last_beat_2 = None
                            self._metronome_awaiting_downbeat_2 = True
                    except Exception:
                        pass
                else:
                    last_beat_2 = None
            else:
                last_beat = None
                last_beat_2 = None
            self._metronome_thread_stop.wait(0.01)

    def _metronome_next_beat(
        self, quantum: float, snapshot: dict,
        beat_attr: str, prev_fractional_attr: str, last_update_attr: str, awaiting_attr: str,
    ) -> int | None:
        """Calcule le temps courant dans la mesure pour une sortie métronome
        (voir _metronome_loop) : rattrape d'un coup le nombre exact de temps
        écoulés depuis le dernier appel (tempo x temps réel écoulé), au lieu
        de se contenter de détecter un seul rebouclage par itération, pour ne
        pas prendre de retard si le thread est retardé un instant.

        Tant que `awaiting_attr` est vrai (juste après une (re)connexion),
        renvoie None (aucun clic) tant que phase % quantum n'atteint pas
        réellement le temps 1 : comme _awaiting_downbeat pour l'affichage,
        pour ne jamais déclencher un clic "temps 1" avant le vrai début de
        mesure côté Link."""
        fractional = snapshot["phase"] % 1.0
        if getattr(self, awaiting_attr):
            if int(snapshot["phase"] % quantum) + 1 != 1:
                return None
            setattr(self, awaiting_attr, False)
            setattr(self, beat_attr, 1)
            setattr(self, prev_fractional_attr, fractional)
            setattr(self, last_update_attr, time.monotonic())
            return 1
        now = time.monotonic()
        prev_fractional = getattr(self, prev_fractional_attr)
        last_update = getattr(self, last_update_attr)
        if prev_fractional is not None and last_update is not None:
            dt = now - last_update
            elapsed_beats = snapshot["bpm"] / 60.0 * dt
            wraps = round(elapsed_beats - (fractional - prev_fractional))
            if wraps > 0:
                beat_in_bar = getattr(self, beat_attr)
                setattr(self, beat_attr, ((beat_in_bar - 1 + wraps) % int(quantum)) + 1)
        setattr(self, prev_fractional_attr, fractional)
        setattr(self, last_update_attr, now)
        return getattr(self, beat_attr)

    # ----------------------------------------------------------- Display --
    def _get_canvas_item(self, key: str, create) -> tuple[int, bool]:
        """Renvoie l'item persistant `key` (le crée une seule fois via
        `create`) et si c'est une création (pour ne (re)configurer les
        options coûteuses, ex. la police, qu'à la création/au changement)."""
        item = self._canvas_items.get(key)
        if item is not None:
            return item, False
        item = create()
        self._canvas_items[key] = item
        return item, True

    def _hide_canvas_items(self, *keys: str) -> None:
        for key in keys:
            item = self._canvas_items.get(key)
            if item is not None:
                self.display.itemconfigure(item, state="hidden")

    def _draw_lyrics_zone_split(self, top_bg: str) -> None:
        """Rectangle couvrant la moitié haute du canvas (zone chiffres/points)
        à sa couleur de flash normale, par-dessus le fond noir du canvas
        (moitié basse, zone des paroles, jamais flashée)."""
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        item, _ = self._get_canvas_item("lyrics_zone_bg", lambda: canvas.create_rectangle(0, 0, 0, 0))
        canvas.coords(item, 0, 0, width, height / 2)
        canvas.itemconfigure(item, fill=top_bg, outline=top_bg, state="normal")
        canvas.tag_lower(item)

    def _draw_lyrics_scroll(self, beat: int, fractional: float) -> None:
        """Fait défiler les paroles (lyrics.py) dans la moitié basse du
        canvas, façon générique de fin : chaque ligne du CSV mesure
        exactement LYRICS_BEATS_PER_LINE temps (indépendant des mesures/COUNT
        de la feuille de scène), et traverse toute la zone réservée en
        LYRICS_SCROLL_BEATS temps."""
        sheet = self._lyrics_sheet
        if sheet is None or not sheet.lines or self._bar_count is None:
            self._hide_lyrics_scroll_items()
            return
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        top = height / 2
        reserved_height = height - top
        pixels_per_beat = reserved_height / LYRICS_SCROLL_BEATS
        reading_y = top + reserved_height * self.lyrics_reading_ratio_var.get()
        song_beat = self._cumulative_beats_at_bar(self._bar_count) + (beat - 1) + fractional
        # Demi-hauteur de ligne (métrique réelle de la police, mise en cache) :
        # une ligne n'est montrée que si elle tient entièrement sous `top`, pour
        # ne jamais déborder visuellement sur la zone chiffres/points (rognage
        # net à la frontière plutôt que de déplacer la position de lecture).
        if self._lyrics_line_half_height is None:
            self._lyrics_line_half_height = tkfont.Font(font=LYRICS_FONT).metrics("linespace") / 2
        top_cutoff = top + self._lyrics_line_half_height
        buffer_px = LYRICS_BEATS_PER_LINE * pixels_per_beat
        visible: dict[int, int] = {}
        for index, text in enumerate(sheet.lines):
            if not text:
                continue
            line_beat = index * LYRICS_BEATS_PER_LINE
            y = reading_y + (line_beat - song_beat) * pixels_per_beat
            if y < top_cutoff or y > height + buffer_px:
                continue
            key = f"lyrics_line_{index}"
            item, _ = self._get_canvas_item(
                key, lambda: canvas.create_text(width / 2, 0, font=LYRICS_FONT, fill=FG_TEXT),
            )
            canvas.coords(item, width / 2, y)
            canvas.itemconfigure(item, text=text, fill=FG_TEXT, state="normal")
            visible[index] = item
        for index in self._lyrics_visible_indices - visible.keys():
            item = self._canvas_items.get(f"lyrics_line_{index}")
            if item is not None:
                canvas.itemconfigure(item, state="hidden")
        self._lyrics_visible_indices = set(visible)

    def _hide_lyrics_scroll_items(self) -> None:
        for index in self._lyrics_visible_indices:
            item = self._canvas_items.get(f"lyrics_line_{index}")
            if item is not None:
                self.display.itemconfigure(item, state="hidden")
        self._lyrics_visible_indices = set()

    def _draw_digit(
        self, beat: int, fill: str = FG_TEXT, size_scale: float = 1.0, top_aligned: bool = False,
    ) -> None:
        self._hide_canvas_items("circle_left", "circle_right", "line_track", "line_fill")
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        font_size = int(min(width, height) * 0.6 * size_scale)
        cy = height * 0.25 if top_aligned else height / 2
        item, _ = self._get_canvas_item("digit", lambda: canvas.create_text(width / 2, height / 2))
        canvas.itemconfigure(
            item, text=str(beat), fill=fill, font=("Helvetica", font_size, "bold"), state="normal",
        )
        canvas.coords(item, width / 2, cy)

    def _draw_two_circles(
        self, beat: int, bg: str, fill: str = FG_TEXT, size_scale: float = 1.0, top_aligned: bool = False,
    ) -> None:
        # Deux cercles côte à côte : celui de gauche se remplit aux temps
        # impairs (1, 3...), celui de droite aux temps pairs (2, 4...) —
        # l'alternance rend le pulse visible à chaque temps.
        self._hide_canvas_items("digit", "line_track", "line_fill")
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        diameter = min(width, height) * 0.6 * 0.8 * size_scale
        radius = diameter / 2
        cy = height * 0.25 if top_aligned else height / 2
        left_cx = width / 2 - diameter * 0.7
        right_cx = width / 2 + diameter * 0.7
        left_filled = beat % 2 == 1
        for key, cx, filled in (("circle_left", left_cx, left_filled), ("circle_right", right_cx, not left_filled)):
            item, _ = self._get_canvas_item(key, lambda: canvas.create_oval(0, 0, 0, 0))
            canvas.coords(item, cx - radius, cy - radius, cx + radius, cy + radius)
            canvas.itemconfigure(
                item, fill=fill if filled else bg, outline=fill, width=3, state="normal",
            )

    def _draw_scroll_line(
        self, beats_per_bar: int, beat: int, fractional: float, top_aligned: bool = False,
    ) -> None:
        # À l'arrêt : la ligne se remplit de gauche à droite au milieu de
        # l'écran (façon barre de progression), synchronisée sur le temps réel
        # (vide au temps 1, pleine à la fin du dernier temps de la mesure).
        # Remontée en haut (top_aligned) comme les chiffres/points quand les
        # paroles sont activées, pour ne jamais empiéter sur leur zone.
        self._hide_canvas_items("digit", "circle_left", "circle_right")
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        beats_per_bar = max(1, beats_per_bar)
        bar_phase = ((beat - 1) + fractional) / beats_per_bar
        x0, x1 = width * 0.15, width * 0.85
        y = height * 0.25 if top_aligned else height / 2
        track, _ = self._get_canvas_item("line_track", lambda: canvas.create_line(0, 0, 0, 0))
        canvas.coords(track, x0, y, x1, y)
        canvas.itemconfigure(track, fill="#3a3a3a", width=4, state="normal")
        fill_end = x0 + bar_phase * (x1 - x0)
        fill_item = self._canvas_items.get("line_fill")
        if fill_end > x0:
            fill_item, _ = self._get_canvas_item("line_fill", lambda: canvas.create_line(0, 0, 0, 0))
            canvas.coords(fill_item, x0, y, fill_end, y)
            canvas.itemconfigure(fill_item, fill=FG_TEXT, width=4, state="normal")
        elif fill_item is not None:
            canvas.itemconfigure(fill_item, state="hidden")

    def _scene_flash_bg(self, bg: str) -> str:
        """Double flash blanc (2×150 ms, séparés d'un court silence) au-dessus
        de la couleur de fond normale, déclenché au lancement d'une scène."""
        if self._scene_flash_start <= 0:
            return bg
        elapsed = time.monotonic() - self._scene_flash_start
        for pulse_start in (0.0, SCENE_FLASH_PULSE + SCENE_FLASH_GAP):
            if pulse_start <= elapsed < pulse_start + SCENE_FLASH_PULSE:
                return _lerp_color(SCENE_FLASH_WHITE, bg, (elapsed - pulse_start) / SCENE_FLASH_PULSE)
        return bg

    def _update_display(
        self, beat: int, beats_per_bar: int, fractional: float, bpm: float | None, connected: bool, running: bool,
    ) -> None:
        self._set_action_active("play", connected and running)
        # En mode Link, le clic est déclenché par _metronome_loop (thread à
        # part, insensible aux gels de _poll/after() sous macOS) ; ici on ne
        # s'en occupe qu'en MIDI Clock, dont l'état n'existe que via _poll.
        if not connected:
            self._last_played_beat = None
            self._last_played_beat_2 = None
        elif self._mode_cache != "link":
            if self._metronome_on and not self._metronome_end_muted and beat != self._last_played_beat:
                self._last_played_beat = beat
                self._audio_metronome.play(beat)
            if self._metronome_on_2 and not self._metronome_end_muted and beat != self._last_played_beat_2:
                self._last_played_beat_2 = beat
                self._audio_metronome_2.play(beat)
        # Mesure HIGHLIGHT (scene_sheet.py, valeur 1) : flash blanc du fond
        # sur TOUS les temps (pas seulement 1/3), digits/dots eux-mêmes
        # fondus du blanc vers leur couleur normale, et 50% plus grands ;
        # remplace entièrement le flash jaune/bleu habituel pour ces mesures.
        highlighted = connected and self._scene_sheet_row is not None and self._scene_sheet_row.highlight
        if highlighted:
            bg = _lerp_color(FLASH_HIGHLIGHT, BG_IDLE, fractional)
            digit_fill = _lerp_color(FLASH_HIGHLIGHT, FG_TEXT, fractional)
            size_scale = HIGHLIGHT_SIZE_SCALE
        else:
            # Flash jaune vif du fond au temps 1, bleu outremer au temps 3, qui
            # s'estompent tous deux vers le fond normal.
            if connected and beat == 1:
                bg = _lerp_color(FLASH_YELLOW, BG_IDLE, fractional)
            elif connected and beat == 3:
                bg = _lerp_color(FLASH_BLUE, BG_IDLE, fractional)
            else:
                bg = BG_IDLE
            digit_fill = FG_TEXT
            size_scale = 1.0
        bg = self._scene_flash_bg(bg)
        # Paroles activées : chiffres/points réduits de moitié et remontés en
        # haut du canvas, pour laisser 50% de l'espace libre en bas (futur
        # défilement des paroles, façon générique de fin) — cette zone reste
        # noire, sans flash, la moitié haute (chiffres/points) gardant le
        # flash normal.
        lyrics_enabled = self.lyrics_var.get()
        if lyrics_enabled:
            size_scale *= 0.5
            self.display.configure(bg="#000000")
            self._draw_lyrics_zone_split(bg)
        else:
            self._hide_canvas_items("lyrics_zone_bg")
            self.display.configure(bg=bg)
        if bpm:
            self._last_bpm = bpm
        # Sans source fiable (pas de clock MIDI / aucun pair Link), un chiffre
        # affiché au hasard serait trompeur pour le batteur : rien du tout.
        if connected:
            if self.dots_var.get():
                self._draw_two_circles(beat, bg, fill=digit_fill, size_scale=size_scale, top_aligned=lyrics_enabled)
            else:
                self._draw_digit(beat, fill=digit_fill, size_scale=size_scale, top_aligned=lyrics_enabled)
            if lyrics_enabled:
                self._draw_lyrics_scroll(beat, fractional)
            else:
                self._hide_lyrics_scroll_items()
        elif self._last_bpm and not running:
            self._draw_scroll_line(beats_per_bar, beat, fractional, top_aligned=lyrics_enabled)
            self._hide_lyrics_scroll_items()
        else:
            self._hide_canvas_items("digit", "circle_left", "circle_right", "line_track", "line_fill")
            self._hide_lyrics_scroll_items()

        if bpm:
            self.bpm_label.config(text=f"{bpm:.1f} BPM")

        source = "Ableton Link" if self.mode_var.get() == "link" else (self.listener.port_name or "MIDI")
        if self.mode_var.get() == "link":
            # is_playing dépend d'un réglage optionnel côté Live (Start Stop
            # Sync) : on affiche "Lecture" seulement quand c'est confirmé,
            # sans afficher un "Arrêté" potentiellement faux sinon.
            transport = "Lecture — " if running else ""
        else:
            transport = ("Lecture" if running else "Arrêté") + " — "
        self.status_label.config(text=f"{transport}{source}")

    def on_close(self) -> None:
        save_config(self.config)
        # Arrêt AVANT self.link.close() : _metronome_loop lit self.link
        # depuis un autre thread.
        self._metronome_thread_stop.set()
        self._metronome_thread.join(timeout=1.0)
        self._close_link_dialog()
        self.listener.close()
        if self.link is not None:
            self.link.close()
        self.live_osc.close()
        self.controller.close()
        self.hui_bridge.close()
        self.hui_bridge_2.close()
        self._audio_metronome.close()
        self._audio_metronome_2.close()
        # Affiche OFFLINE sur la page web avant de couper le serveur : sans
        # ça, la page reste figée sur le dernier chiffre/ligne affiché sans
        # prévenir que CLIC a quitté (la page web poll toutes les 60ms).
        self.shared_state.set_offline()
        time.sleep(0.4)
        self.web_server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    # Sur macOS, Cmd+Q (menu appli) n'envoie pas WM_DELETE_WINDOW : sans ce
    # remplacement, on_close (donc le message OFFLINE côté web) est sauté.
    root.createcommand("::tk::mac::Quit", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
