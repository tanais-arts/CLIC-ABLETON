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
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import rtmidi

from config import load_config, save_config
from hui_bridge import HuiBridge
from link_client import AbletonLink, LinkUnavailable
from live_osc import LiveOSC
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

    def close(self) -> None:
        if self._midi_in is not None:
            self._midi_in.close_port()
            self._midi_in = None
            self._port_name = None

    @property
    def port_name(self) -> str | None:
        return self._port_name

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
    # Options de plage du fader 16 : (libellé affiché, clé persistée en config).
    TEMPO_RANGE_OPTIONS = [
        ("± 3 %", "3"), ("± 6 %", "6"), ("± 10 %", "10"), ("± 20 %", "20"),
        ("Libre (0-500 BPM)", "free"),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Compteur de temps - Ableton Live")
        self.root.configure(bg=BG_IDLE)
        self.root.geometry("640x520")
        self.root.minsize(520, 420)

        self.config = load_config()

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

        # -- Métronome de Live (activer/désactiver) : état tenu à jour par
        # l'abonnement OSC (reflète aussi un changement fait depuis Live). --
        self._metronome_on: bool = False
        self.live_osc.start_listen_metronome()

        # -- Contrôleur MIDI (ex. Behringer BCF2000) pour les 6 boutons (nudge,
        # navigation scènes, stop, lancer la scène) --
        self._controller_queue: "queue.Queue[list[int]]" = queue.Queue()
        self.controller = ControllerListener(self._controller_queue)
        self._action_order = ["minus", "plus", "scene_prev", "scene_next", "stop", "play", "metronome"]
        self._action_labels = {
            "minus": "−1", "plus": "+1", "scene_prev": "▲", "scene_next": "▼", "stop": "■", "play": "▶",
            "metronome": "M",
        }
        self._action_commands = {
            "minus": lambda: self._jump_beats(-1),
            "plus": lambda: self._jump_beats(1),
            "scene_prev": lambda: self._scene_step(-1),
            "scene_next": lambda: self._scene_step(1),
            "stop": self._stop_return_to_start,
            "play": self._scene_launch,
            "metronome": self._toggle_metronome,
        }
        self.controller_map: dict[str, tuple[str, int, int] | None] = {
            action: _as_key(self.config.get(f"controller_map_{action}"))
            for action in self._action_order
        }
        self._learning: str | None = None

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
        # Empêche _set_tempo de renvoyer à Link une valeur que l'on vient
        # nous-même d'écrire dans le champ suite à un changement externe (voir
        # _update_tempo_display).
        self._suspend_tempo_send = False

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
        # Compteur de mesures depuis le lancement du morceau en cours (voir
        # _scene_launch/_update_bar_count) : None = pas de comptage affiché.
        # Les scènes "tempo seul" (chiffres) ne déclenchent jamais ce compte.
        self._bar_count: int | None = None
        self._awaiting_bar_start = False
        self._bar_count_prev_beat: int | None = None
        # Numéro de mesure de départ ("GO TO mesure", voir goto_bar_var) pour le
        # prochain lancement de scène — 1 par défaut (début du morceau).
        self._bar_count_start = 1
        # Feuille de scène XLSX (scene_sheet.py) du morceau en cours, si le
        # fichier <nom de scène>.xlsx existe à côté du script ; None = aucune
        # feuille, comportement inchangé (voir _apply_scene_sheet_row).
        self._scene_sheet: SceneSheet | None = None
        self._scene_sheet_row: SceneSheetRow | None = None
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
        self._poll()
        self._ping_live()

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

        # -- Bloc Link --
        self.link_frame = tk.Frame(top, bg=BG_IDLE)
        self.link_peers_label = tk.Label(self.link_frame, text="Pairs Link connectés : —", bg=BG_IDLE, fg=FG_TEXT)
        self.link_peers_label.pack(side="left", pady=(6, 0))

        tk.Label(self.link_frame, text="  Régler le tempo :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left", pady=(6, 0))
        self.set_tempo_var = tk.DoubleVar(value=120.0)
        # Envoi en temps réel : chaque changement (flèches, saisie clavier ou
        # fader 16 dédié, voir _apply_tempo_fader) déclenche _set_tempo, pas
        # de bouton "Envoyer" à part.
        self.set_tempo_var.trace_add("write", lambda *_args: self._set_tempo())
        tk.Spinbox(
            self.link_frame, from_=0.0, to=500.0, increment=0.1, width=6,
            textvariable=self.set_tempo_var,
        ).pack(side="left", padx=(4, 4), pady=(6, 0))

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
            settings_frame, text="Points au lieu des chiffres", variable=self.dots_var,
            command=self._on_settings_change, bg=BG_IDLE, fg=FG_TEXT, selectcolor="#333333",
            activebackground=BG_IDLE, activeforeground=FG_TEXT,
        ).pack(side="left", padx=(16, 0))

        tk.Label(settings_frame, text="  GO TO mesure :", bg=BG_IDLE, fg=FG_TEXT).pack(side="left")
        self.goto_bar_var = tk.IntVar(value=1)
        tk.Spinbox(
            settings_frame, from_=1, to=999, width=5, textvariable=self.goto_bar_var,
        ).pack(side="left", padx=(6, 0))

        # -- Affichage principal du temps : un carré, gros pour 1/3, petit pour 2/4 --
        self.display = tk.Canvas(self.root, bg=BG_IDLE, highlightthickness=0)
        self.display.pack(expand=True, fill="both", padx=10, pady=4)

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

        # -- Nudge (-1/+1), navigation scènes (▲▼), Lecture (▶) et Stop (■) :
        # boutons carrés de même taille (modèle : le bouton +1), alignés sur
        # une seule ligne sous Lecture/Tempo. À gauche de chaque bouton : "A"
        # (apprendre le code MIDI) au-dessus de "E" (effacer l'apprentissage),
        # eux aussi carrés (taille fixe en pixels). --
        controls_row = tk.Frame(self.root, bg=BG_IDLE)
        controls_row.pack(fill="x", padx=10, pady=(0, 8))
        square_btn = dict(width=4, height=2, font=("Helvetica", 14, "bold"))
        MINI_SIZE = 22  # pixels : taille fixe pour que A/E soient réellement carrés
        self.learn_buttons: dict[str, tk.Button] = {}
        self.clear_buttons: dict[str, tk.Button] = {}
        self.action_buttons: dict[str, tk.Frame] = {}
        self._action_flash_after_id: dict[str, str] = {}

        def add_mini_button(parent: tk.Frame, text: str, command) -> tk.Button:
            holder = tk.Frame(parent, width=MINI_SIZE, height=MINI_SIZE, bg=BG_IDLE)
            holder.pack_propagate(False)
            holder.pack(side="top", pady=(0, 2) if text == "A" else (2, 0))
            btn = tk.Button(holder, text=text, command=command, font=("Helvetica", 9), padx=0, pady=0)
            btn.pack(fill="both", expand=True)
            return btn

        def add_control(action: str, text: str) -> None:
            group = tk.Frame(controls_row, bg=BG_IDLE)
            group.pack(side="left", padx=4)
            mini = tk.Frame(group, bg=BG_IDLE)
            mini.pack(side="left", padx=(0, 2))
            self.learn_buttons[action] = add_mini_button(mini, "A", lambda: self._start_learn(action))
            self.clear_buttons[action] = add_mini_button(mini, "E", lambda: self._clear_assignment(action))
            # macOS Aqua ignore le bg d'un tk.Button natif : on flashe ce cadre autour, pas le bouton.
            flash_holder = tk.Frame(group, bg=BG_IDLE)
            flash_holder.pack(side="left")
            btn = tk.Button(flash_holder, text=text, command=self._action_commands[action], **square_btn)
            btn.pack(padx=3, pady=3)
            self.action_buttons[action] = flash_holder

        add_control("minus", "−1")
        add_control("plus", "+1")
        add_control("scene_prev", "▲")
        add_control("scene_next", "▼")
        add_control("play", "▶")
        add_control("stop", "■")
        add_control("metronome", "M")

        # -- Contrôleur MIDI (ex. Behringer BCF2000) pour piloter les mêmes boutons --
        controller_row = tk.Frame(self.root, bg=BG_IDLE)
        controller_row.pack(fill="x", padx=10, pady=(0, 4))
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
        controller_row_2 = tk.Frame(self.root, bg=BG_IDLE)
        controller_row_2.pack(fill="x", padx=10, pady=(0, 4))
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

        controller_row_3 = tk.Frame(self.root, bg=BG_IDLE)
        controller_row_3.pack(fill="x", padx=10, pady=(0, 4))
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

        hui_mapping_row = tk.Frame(self.root, bg=BG_IDLE)
        hui_mapping_row.pack(fill="x", padx=10, pady=(0, 4))
        tk.Button(
            hui_mapping_row, text="Configurer le mapping des faders…", command=self._open_hui_mapping_dialog,
        ).pack(side="left")
        tk.Label(
            hui_mapping_row, text="  (fader 16 dédié au tempo, voir plus haut)", bg=BG_IDLE, fg="#888888",
        ).pack(side="left")

        status_row = tk.Frame(self.root, bg=BG_IDLE)
        status_row.pack(fill="x", padx=10, pady=(0, 4))
        self.controller_status_label = tk.Label(status_row, text="", bg=BG_IDLE, fg="#bbbbbb")
        self.controller_status_label.pack(side="left")

        table_row = tk.Frame(self.root, bg=BG_IDLE)
        table_row.pack(fill="x", padx=10, pady=(0, 8))
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
        if mode == "link":
            self.midi_frame.pack_forget()
            self.link_frame.pack(fill="x")
        else:
            self.link_frame.pack_forget()
            self.midi_frame.pack(fill="x")
        self._on_settings_change()

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
        défaut. Tranche 16 absente (réservée au tempo, voir
        TEMPO_FADER_ZONE). Rien n'est appliqué avant l'appui sur "Appliquer"."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Mapping faders Live ↔ Yamaha")
        dialog.configure(bg=BG_IDLE)

        tk.Label(dialog, text="Piste \\ Tranche", bg=BG_IDLE, fg=FG_TEXT).grid(row=0, column=0, padx=(4, 6))
        for col in range(15):
            tk.Label(dialog, text=str(col + 1), bg=BG_IDLE, fg=FG_TEXT, width=2).grid(
                row=0, column=col + 1, padx=1, pady=(4, 2)
            )

        row_vars: list[tk.IntVar] = []
        for track in range(16):
            var = tk.IntVar(value=self._track_mapping[track])
            row_vars.append(var)
            tk.Label(dialog, text=f"Piste {track + 1}", bg=BG_IDLE, fg=FG_TEXT).grid(
                row=track + 1, column=0, sticky="w", padx=(4, 6)
            )
            for col in range(15):
                tk.Radiobutton(
                    dialog, variable=var, value=col, bg=BG_IDLE, activebackground=BG_IDLE, selectcolor="#333333",
                ).grid(row=track + 1, column=col + 1)

        button_row = tk.Frame(dialog, bg=BG_IDLE)
        button_row.grid(row=17, column=0, columnspan=16, pady=8)
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
        self._action_flash_after_id[action] = self.root.after(200, lambda: holder.config(bg=BG_IDLE))

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
                    continue
                for action, mapped_key in self.controller_map.items():
                    if key == mapped_key:
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
        self.config["latency_ms"] = latency
        self.config["mode"] = self.mode_var.get()
        self.config["dots_only"] = self.dots_var.get()
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
        le champ n'est visible qu'en mode Link. Ignoré quand le champ vient
        d'être mis à jour par _update_tempo_display (écho d'un changement
        externe), pour ne pas le renvoyer inutilement à Link."""
        if self._suspend_tempo_send:
            return
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
        réellement de ce qui y est déjà affiché."""
        if self._tempo_last_sent_bpm is not None and abs(bpm - self._tempo_last_sent_bpm) < 0.05:
            return
        try:
            current = float(self.set_tempo_var.get())
        except (tk.TclError, ValueError):
            current = None
        if current is not None and abs(bpm - current) < 0.05:
            return
        self._suspend_tempo_send = True
        try:
            self.set_tempo_var.set(round(bpm, 1))
        finally:
            self._suspend_tempo_send = False

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
        inchangé, monter/descendre l'augmente/diminue. En mode pourcentage,
        la plage couvre toute la course du fader ; en mode "free", le fader
        représente un tempo absolu de 0 (tout en bas) à 500 BPM (tout en haut),
        indépendant du morceau chargé."""
        mode = self.config.get("tempo_fader_range", "6")
        if mode == "free":
            new_bpm = (raw / 16383.0) * 500.0
        else:
            try:
                pct = float(mode) / 100.0
            except ValueError:
                pct = 0.06
            frac = max(-1.0, min(1.0, (raw - 8191.5) / 8191.5))
            reference = self._tempo_reference_bpm if self._tempo_reference_bpm else 120.0
            new_bpm = reference * (1.0 + frac * pct)
        self._tempo_last_sent_bpm = new_bpm
        self.set_tempo_var.set(round(new_bpm, 1))

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
        self._tempo_last_sent_bpm = origin
        self.set_tempo_var.set(round(origin, 1))

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
            self.live_osc.start_listen_metronome()
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
        try:
            self.live_osc.set_selected_scene(new_index)
            self.live_osc.get_scene_name(new_index)
        except OSError as exc:
            self.scene_name_label.config(text=f"Erreur OSC : {exc}")

    def _scene_launch(self) -> None:
        # fire_selected (Scene.fire_as_selected) avance aussi la sélection vers
        # la scène suivante côté Live : on utilise fire(index) pour ne lancer
        # que la scène affichée, sans bouger la sélection.
        if self._scene_index is None:
            return
        try:
            self.live_osc.fire_scene(self._scene_index)
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
                self._scene_flash_start = time.monotonic()
                # Le compteur de mesures démarre au premier vrai temps 1 qui
                # suit ce lancement (voir _update_bar_count), pas à l'appui.
                self._bar_count = None
                self._bar_count_prev_beat = None
                self._awaiting_bar_start = True
                self.bar_count_label.config(text="")
                self.shared_state.set_bar_count(None)
                # Feuille de scène XLSX (<nom de scène>.xlsx, voir
                # scene_sheet.py) : None si le fichier n'existe pas, aucun
                # changement de comportement dans ce cas. "GO TO mesure"
                # (goto_bar_var) fixe le point de départ du comptage ; on
                # applique tout de suite la ligne correspondante (COUNT/
                # HIGHLIGHT/LABEL) pour que le tout premier temps affiché
                # soit déjà correct, sans attendre une mesure de retard.
                self._scene_sheet = load_scene_sheet(
                    self._scene_name, self.SCENE_SHEET_DIR, log=lambda msg: print(f"[Feuille de scène] {msg}"),
                )
                try:
                    self._bar_count_start = max(1, int(self.goto_bar_var.get()))
                except (tk.TclError, ValueError):
                    self._bar_count_start = 1
                self._scene_label_sticky = (
                    self._scene_sheet.label_at_or_before(self._bar_count_start)
                    if self._scene_sheet is not None else ""
                )
                self._apply_scene_sheet_row(self._bar_count_start)
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
        try:
            self.live_osc.stop_playing()
            self.root.after(120, self.live_osc.stop_playing)
        except OSError as exc:
            self.status_label.config(text=f"Erreur OSC : {exc}")

    def _toggle_metronome(self) -> None:
        """Bascule le métronome de Live. `self._metronome_on` (mis à jour par
        l'abonnement OSC) reflète l'état réel, pas seulement nos propres appuis."""
        try:
            self.live_osc.set_metronome(not self._metronome_on)
        except OSError as exc:
            self.status_label.config(text=f"Erreur OSC : {exc}")

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
            elif address == "/live/song/get/metronome":
                self._metronome_on = bool(args[0])
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
                    self._scene_name = name
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

    def _apply_scene_sheet_row(self, mes: int) -> None:
        """Applique la ligne de la feuille de scène (scene_sheet.py) pour la
        mesure `mes` : COUNT (temps par mesure, avec retour à la valeur
        configurée si absent), HIGHLIGHT (consommé par _update_display) et
        LABEL ("collant" : ne s'efface que quand une nouvelle valeur non vide
        arrive). Sans feuille (ou mesure hors feuille), comportement normal."""
        row = self._scene_sheet.get(mes) if self._scene_sheet is not None else None
        self._scene_sheet_row = row
        count = row.count if row is not None and row.count else self.config["beats_per_bar"]
        self.beats_var.set(count)
        self.midi_state.beats_per_bar = count
        if row is not None and row.label:
            self._scene_label_sticky = row.label
        self.scene_label_label.config(text=self._scene_label_sticky)
        self.shared_state.set_scene_label(self._scene_label_sticky)

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

    def _update_bar_count(self, connected: bool, beat: int) -> None:
        """Compte les mesures depuis le premier vrai temps 1 qui suit le
        lancement du morceau en cours (armé par _scene_launch pour les vraies
        scènes uniquement, jamais pour les scènes "tempo seul"/préparation)."""
        if not connected:
            self._bar_count_prev_beat = None
            return
        if self._awaiting_bar_start:
            if beat == 1:
                self._awaiting_bar_start = False
                self._bar_count = self._bar_count_start
                self._bar_count_prev_beat = 1
                self.bar_count_label.config(text=f"Mesure {self._bar_count}")
                self.shared_state.set_bar_count(self._bar_count)
            return
        if self._bar_count is not None and beat == 1 and self._bar_count_prev_beat != 1:
            self._bar_count += 1
            self._apply_scene_sheet_row(self._bar_count)
            self.bar_count_label.config(text=f"Mesure {self._bar_count}")
            self.shared_state.set_bar_count(self._bar_count)
        self._bar_count_prev_beat = beat

    def _poll(self) -> None:
        self._poll_controller()
        self._poll_scene_replies()
        self._poll_tempo_fader()
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
            phase = project_phase(self.midi_state.phase(), self.midi_state.bpm, connected, self.latency_var.get())
            beat = int(phase % self.midi_state.beats_per_bar) + 1
            if connected and self._awaiting_downbeat and beat == 1:
                self._awaiting_downbeat = False
            running = connected  # présence réelle du clock, avant masquage
            connected = connected and not self._awaiting_downbeat
            self._update_bar_count(connected, beat)
            self._update_display(beat, self.midi_state.beats_per_bar, phase % 1.0, self.midi_state.bpm, connected, running)
            self.shared_state.update(
                self.midi_state.phase(), self.midi_state.beats_per_bar,
                self.midi_state.bpm, connected, running, "midi",
            )
            # Le fader 16 pilote le tempo via Link indépendamment du mode
            # d'affichage choisi (comme les boutons -1/+1) : on garde le tempo
            # de référence et le champ de tempo à jour même si l'affichage
            # courant est en MIDI Clock.
            tempo_link = self._ensure_link()
            if tempo_link is not None:
                self._on_link_tempo_observed(tempo_link.snapshot(quantum=1.0)["bpm"])
        else:
            link = self._ensure_link()
            if link is not None:
                quantum = float(max(1, self.beats_var.get()))
                snapshot = link.snapshot(quantum=quantum)
                # On n'affiche le comptage qu'une fois le signal START de Link
                # reçu (is_playing) : la seule présence de pairs ne suffit pas,
                # sinon le compteur démarre au lancement même sans lecture.
                connected = link.num_peers >= 1 and snapshot["is_playing"]
                if connected and not self._was_connected:
                    self._awaiting_downbeat = True
                    self._link_prev_fractional = None
                self._was_connected = connected
                phase = project_phase(snapshot["phase"], snapshot["bpm"], connected, self.latency_var.get())
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
                else:
                    # Comptage en continu, indépendant du modulo Link : un
                    # rebouclage de la partie fractionnaire du temps (0.95 ->
                    # 0.05 par exemple, détecté ici) fait avancer notre PROPRE
                    # compteur de temps dans la mesure, qui reboucle sur le
                    # COUNT courant (self.beats_var) — donc correct même
                    # quand ce COUNT change d'une mesure à l'autre (feuille de
                    # scène, ex. 2 temps puis 4 temps), ce que ne permet pas
                    # phase % quantum (aligné sur la session Link depuis son
                    # tout début, pas sur le début de la mesure courante).
                    if (
                        self._link_prev_fractional is not None
                        and fractional < self._link_prev_fractional - 0.5
                    ):
                        self._link_beat_in_bar += 1
                        if self._link_beat_in_bar > int(quantum):
                            self._link_beat_in_bar = 1
                    self._link_prev_fractional = fractional
                    beat = self._link_beat_in_bar
                connected = connected and not self._awaiting_downbeat
                self._update_bar_count(connected, beat)
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

        self.root.after(30, self._poll)

    # ----------------------------------------------------------- Display --
    def _draw_digit(self, beat: int, fill: str = FG_TEXT, size_scale: float = 1.0) -> None:
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        font_size = int(min(width, height) * 0.6 * size_scale)
        canvas.create_text(
            width / 2, height / 2, text=str(beat), fill=fill, font=("Helvetica", font_size, "bold"),
        )

    def _draw_two_circles(self, beat: int, bg: str, fill: str = FG_TEXT, size_scale: float = 1.0) -> None:
        # Deux cercles côte à côte : celui de gauche se remplit aux temps
        # impairs (1, 3...), celui de droite aux temps pairs (2, 4...) —
        # l'alternance rend le pulse visible à chaque temps.
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        diameter = min(width, height) * 0.6 * 0.8 * size_scale
        radius = diameter / 2
        cy = height / 2
        left_cx = width / 2 - diameter * 0.7
        right_cx = width / 2 + diameter * 0.7
        left_filled = beat % 2 == 1
        for cx, filled in ((left_cx, left_filled), (right_cx, not left_filled)):
            canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                fill=fill if filled else bg, outline=fill, width=3,
            )

    def _draw_scroll_line(self, beats_per_bar: int, beat: int, fractional: float) -> None:
        # À l'arrêt : la ligne se remplit de gauche à droite au milieu de
        # l'écran (façon barre de progression), synchronisée sur le temps réel
        # (vide au temps 1, pleine à la fin du dernier temps de la mesure).
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        beats_per_bar = max(1, beats_per_bar)
        bar_phase = ((beat - 1) + fractional) / beats_per_bar
        x0, x1 = width * 0.15, width * 0.85
        y = height / 2
        canvas.create_line(x0, y, x1, y, fill="#3a3a3a", width=4)
        fill_end = x0 + bar_phase * (x1 - x0)
        if fill_end > x0:
            canvas.create_line(x0, y, fill_end, y, fill=FG_TEXT, width=4)

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
        # Le flash reste cantonné au canvas (digits/dots), pas à toute la fenêtre.
        self.display.configure(bg=bg)
        self.display.delete("all")
        if bpm:
            self._last_bpm = bpm
        # Sans source fiable (pas de clock MIDI / aucun pair Link), un chiffre
        # affiché au hasard serait trompeur pour le batteur : rien du tout.
        if connected:
            if self.dots_var.get():
                self._draw_two_circles(beat, bg, fill=digit_fill, size_scale=size_scale)
            else:
                self._draw_digit(beat, fill=digit_fill, size_scale=size_scale)
        elif self._last_bpm and not running:
            self._draw_scroll_line(beats_per_bar, beat, fractional)

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
        self._close_link_dialog()
        self.listener.close()
        if self.link is not None:
            self.link.close()
        self.live_osc.close()
        self.controller.close()
        self.hui_bridge.close()
        self.hui_bridge_2.close()
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
