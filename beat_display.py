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
from link_client import AbletonLink, LinkUnavailable
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

        self._demo_job = None
        self._demo_running = False
        # Devient vrai dès qu'on observe is_playing=True au moins une fois :
        # preuve que "Start Stop Sync" est activé côté Live et donc fiable.
        self._link_start_stop_confirmed = False
        # À la reprise (connecté/en lecture après ne pas l'avoir été), on
        # n'affiche les temps qu'à partir du prochain temps 1 réel, pour ne
        # pas commencer au milieu d'une mesure.
        self._was_connected = False
        self._awaiting_downbeat = False
        # Dernier tempo connu, pour animer la ligne de défilement à l'arrêt
        # (même quand la source ne fournit plus de temps courant fiable).
        self._last_bpm: float | None = None

        self._build_ui()
        self._refresh_ports()
        if self.config.get("midi_port"):
            self.port_var.set(self.config["midi_port"])
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

        self.demo_btn = tk.Button(settings_frame, text="Démo 120 BPM (sans Live)", command=self._toggle_demo)
        self.demo_btn.pack(side="right")

        # -- Affichage principal du temps : un carré, gros pour 1/3, petit pour 2/4 --
        self.display = tk.Canvas(self.root, bg=BG_IDLE, highlightthickness=0)
        self.display.pack(expand=True, fill="both", padx=10, pady=4)

        bottom = tk.Frame(self.root, bg=BG_IDLE)
        bottom.pack(fill="x", padx=10, pady=4)
        self.status_label = tk.Label(bottom, text="Déconnecté", bg=BG_IDLE, fg="#bbbbbb")
        self.status_label.pack(side="left")
        self.bpm_label = tk.Label(bottom, text="", bg=BG_IDLE, fg="#bbbbbb")
        self.bpm_label.pack(side="right")

        web_row = tk.Frame(self.root, bg=BG_IDLE)
        web_row.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(web_row, text="Affichage smartphone :", bg=BG_IDLE, fg="#bbbbbb").pack(side="left")
        tk.Label(
            web_row, text=self.web_server.url(), bg=BG_IDLE, fg="#7fb2ff",
            font=("Helvetica", 12, "bold"),
        ).pack(side="left", padx=6)

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

    # --------------------------------------------------------- Demo mode --
    def _toggle_demo(self) -> None:
        if self._demo_running:
            self._demo_running = False
            if self._demo_job is not None:
                self.root.after_cancel(self._demo_job)
                self._demo_job = None
            self.demo_btn.config(text="Démo 120 BPM (sans Live)")
            self.midi_state.running = False
            self.status_label.config(text="Démo arrêtée")
            return

        self._demo_running = True
        self.demo_btn.config(text="Arrêter la démo")
        self.midi_state.reset_position()
        self.midi_state.running = True
        self.status_label.config(text="Démo interne à 120 BPM (aucun MIDI/Link requis)")
        self._demo_tick()

    def _demo_tick(self) -> None:
        if not self._demo_running:
            return
        quarter_seconds = 60.0 / 120.0
        tick_seconds = quarter_seconds / TICKS_PER_QUARTER
        for _ in range(TICKS_PER_QUARTER):
            if self.midi_state.handle_message([CLOCK], tick_seconds):
                fractional = self.midi_state.phase() % 1.0
                self._update_display(
                    self.midi_state.beat_in_bar, self.midi_state.beats_per_bar, fractional,
                    self.midi_state.bpm, self.midi_state.running, self.midi_state.running,
                )
        self._demo_job = self.root.after(int(quarter_seconds * 1000), self._demo_tick)

    # -------------------------------------------------------- Boucle poll --
    def _poll(self) -> None:
        if self._demo_running:
            pass  # la démo pilote déjà l'affichage via son propre timer
        elif self.mode_var.get() == "midi":
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
                # Le timeline Link tourne en continu dès qu'on a un pair, donc
                # les pairs connectés servent de base pour figer/afficher.
                # "is_playing" nécessite en plus "Start Stop Sync" activé côté
                # Live : on ne l'utilise pour figer le compteur à l'arrêt que
                # dès qu'on l'a vu passer à vrai une fois (signal confirmé
                # fiable), pour ne pas figer à tort chez qui ne l'active pas.
                if snapshot["is_playing"]:
                    self._link_start_stop_confirmed = True
                connected = link.num_peers >= 1
                if self._link_start_stop_confirmed:
                    connected = connected and snapshot["is_playing"]
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
        self.root.configure(bg=bg)
        self.display.configure(bg=bg)
        self.display.delete("all")
        if bpm:
            self._last_bpm = bpm
        # Sans source fiable (pas de clock MIDI / aucun pair Link), un chiffre
        # affiché au hasard serait trompeur pour le batteur : rien du tout.
        if connected:
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
        self.web_server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
