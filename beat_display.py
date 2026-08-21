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
from tkinter import ttk

import rtmidi

from config import load_config, save_config
from hui_bridge import HuiBridge
from link_client import AbletonLink, LinkUnavailable
from live_osc import LiveOSC
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

        # -- Contrôleur MIDI (ex. Behringer BCF2000) pour les 6 boutons (nudge,
        # navigation scènes, stop, lancer la scène) --
        self._controller_queue: "queue.Queue[list[int]]" = queue.Queue()
        self.controller = ControllerListener(self._controller_queue)
        self._action_order = ["minus", "plus", "scene_prev", "scene_next", "stop", "play"]
        self._action_labels = {
            "minus": "−1", "plus": "+1", "scene_prev": "▲", "scene_next": "▼", "stop": "■", "play": "▶",
        }
        self._action_commands = {
            "minus": lambda: self._jump_beats(-1),
            "plus": lambda: self._jump_beats(1),
            "scene_prev": lambda: self._scene_step(-1),
            "scene_next": lambda: self._scene_step(1),
            "stop": self._stop_return_to_start,
            "play": self._scene_launch,
        }
        self.controller_map: dict[str, tuple[str, int, int] | None] = {
            action: _as_key(self.config.get(f"controller_map_{action}"))
            for action in self._action_order
        }
        self._learning: str | None = None

        # -- Pont HUI -> OSC (ex. Yamaha 01V96V2) pour faders/mutes des pistes --
        # La console répartit ses 16 voies sur 2 ports MIDI (8 tranches chacun) :
        # bridge principal = voies 1-8, bridge_2 = voies 9-16 (offset de piste +8).
        self.hui_bridge = HuiBridge(self.live_osc, log=lambda msg: print(f"[HUI] {msg}"))
        self.hui_bridge_2 = HuiBridge(self.live_osc, log=lambda msg: print(f"[HUI] {msg}"), channel_offset=8)

        # À la reprise (connecté/en lecture après ne pas l'avoir été), on
        # n'affiche les temps qu'à partir du prochain temps 1 réel, pour ne
        # pas commencer au milieu d'une mesure.
        self._was_connected = False
        self._awaiting_downbeat = False
        # Dernier tempo connu, pour animer la ligne de défilement à l'arrêt
        # (même quand la source ne fournit plus de temps courant fiable).
        self._last_bpm: float | None = None
        # Flash blanc ponctuel du canvas au lancement d'une scène.
        self._scene_flash_start: float = 0.0

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

        # -- Affichage principal du temps : un carré, gros pour 1/3, petit pour 2/4 --
        self.display = tk.Canvas(self.root, bg=BG_IDLE, highlightthickness=0)
        self.display.pack(expand=True, fill="both", padx=10, pady=4)

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

    def _set_hui_listen(self, channel_offset: int, listen: bool) -> None:
        """Abonne/désabonne aux changements de volume, mute et nom d'Ableton
        pour les 8 pistes couvertes par un pont HUI, pour le retour vers la
        console (fader/LED mute/nom qui reflètent l'état réel de Live)."""
        for track in range(channel_offset, channel_offset + 8):
            if listen:
                self.live_osc.start_listen_track_volume(track)
                self.live_osc.start_listen_track_mute(track)
                self.live_osc.start_listen_track_name(track)
                # start_listen ne renvoie que les changements futurs : on
                # demande aussi la valeur actuelle pour l'état de départ.
                self.live_osc.get_track_volume(track)
                self.live_osc.get_track_mute(track)
                self.live_osc.get_track_name(track)
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

    def _poll_scene_replies(self) -> None:
        for address, args in self.live_osc.poll_replies():
            if address == "/live/error":
                print(f"[OSC] erreur renvoyée par AbletonOSC : {args}")
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
                elif (
                    self._scene_index is not None
                    and index == self._scene_index + 1
                    and self._scene_name.strip().isdigit()
                ):
                    self._update_scene_label(next_name=name)

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

    # -------------------------------------------------------- Boucle poll --
    def _poll(self) -> None:
        self._poll_controller()
        self._poll_scene_replies()
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
            self._update_display(beat, self.midi_state.beats_per_bar, phase % 1.0, self.midi_state.bpm, connected, running)
            self.shared_state.update(
                self.midi_state.phase(), self.midi_state.beats_per_bar,
                self.midi_state.bpm, connected, running, "midi",
            )
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
                self._was_connected = connected
                phase = project_phase(snapshot["phase"], snapshot["bpm"], connected, self.latency_var.get())
                beat = int(phase % quantum) + 1
                if connected and self._awaiting_downbeat and beat == 1:
                    self._awaiting_downbeat = False
                connected = connected and not self._awaiting_downbeat
                self._update_display(beat, int(quantum), phase % 1.0, snapshot["bpm"], connected, snapshot["is_playing"])
                self.link_peers_label.config(text=f"Pairs Link connectés : {link.num_peers}")
                self.shared_state.update(
                    snapshot["phase"], quantum, snapshot["bpm"], connected, snapshot["is_playing"], "link",
                )

        self.root.after(30, self._poll)

    # ----------------------------------------------------------- Display --
    def _draw_digit(self, beat: int) -> None:
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        font_size = int(min(width, height) * 0.6)
        canvas.create_text(
            width / 2, height / 2, text=str(beat), fill=FG_TEXT, font=("Helvetica", font_size, "bold"),
        )

    def _draw_two_circles(self, beat: int, bg: str) -> None:
        # Deux cercles côte à côte : celui de gauche se remplit aux temps
        # impairs (1, 3...), celui de droite aux temps pairs (2, 4...) —
        # l'alternance rend le pulse visible à chaque temps.
        canvas = self.display
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        diameter = min(width, height) * 0.6 * 0.8
        radius = diameter / 2
        cy = height / 2
        left_cx = width / 2 - diameter * 0.7
        right_cx = width / 2 + diameter * 0.7
        left_filled = beat % 2 == 1
        for cx, filled in ((left_cx, left_filled), (right_cx, not left_filled)):
            canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                fill=FG_TEXT if filled else bg, outline=FG_TEXT, width=3,
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
        # Flash jaune vif du fond au temps 1, bleu outremer au temps 3, qui
        # s'estompent tous deux vers le fond normal.
        if connected and beat == 1:
            bg = _lerp_color(FLASH_YELLOW, BG_IDLE, fractional)
        elif connected and beat == 3:
            bg = _lerp_color(FLASH_BLUE, BG_IDLE, fractional)
        else:
            bg = BG_IDLE
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
                self._draw_two_circles(beat, bg)
            else:
                self._draw_digit(beat)
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
        self.listener.close()
        if self.link is not None:
            self.link.close()
        self.live_osc.close()
        self.controller.close()
        self.hui_bridge.close()
        self.hui_bridge_2.close()
        self.web_server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
