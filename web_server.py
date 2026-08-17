"""Petit serveur HTTP local pour afficher le compteur de temps sur un
navigateur (smartphone, tablette...) connecté au même réseau Wi-Fi que
l'ordinateur qui fait tourner Ableton Live.

Chaque appareil peut régler son propre décalage de latence (mémorisé dans le
navigateur) : le serveur transmet la position (phase) et le tempo bruts, et
c'est le client (ou l'appli desktop) qui projette le temps affiché en
fonction de son propre réglage.

Aucune dépendance externe : uniquement la bibliothèque standard.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no, viewport-fit=cover">
<title>Temps - Ableton Live</title>
<style>
  html, body {
    margin: 0; padding: 0; width: 100%;
    height: 100vh; height: 100dvh;
    background: #1e1e1e; color: #f5f5f5;
    font-family: -apple-system, Helvetica, Arial, sans-serif;
    overflow: hidden;
    box-sizing: border-box;
  }
  body {
    display: flex; flex-direction: column;
    /* Marge de sécurité : évite que les contrôles ne passent sous l'encoche
       ou la barre d'adresse (iOS/Android) qui grignotent le haut/bas. */
    padding-top: max(14px, env(safe-area-inset-top));
    padding-bottom: max(14px, env(safe-area-inset-bottom));
  }
  #latency {
    flex: 0 0 auto; margin-top: 6px;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 4px; font-size: 3vh; color: #888888;
  }
  #latency .row { display: flex; align-items: center; gap: 12px; }
  #latency input[type=range] { width: 60vw; }
  #latency .ticks {
    width: 60vw; display: flex; justify-content: space-between;
    font-size: 2vh; color: #555555; padding: 0 2px;
  }
  #beat {
    flex: 1 1 auto; min-height: 0;
    position: relative;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 1.5vh;
  }
  #dot {
    display: none;
    font-size: 20vh; color: #f5f5f5;
    -webkit-user-select: none; user-select: none;
  }
  #digit {
    display: none;
    font-size: 40vh; font-weight: bold; color: #f5f5f5;
    -webkit-user-select: none; user-select: none;
  }
  #scrollLine {
    display: none;
    position: absolute; top: 15%; bottom: 15%; left: 0;
    width: 0.8vh; background: #f5f5f5;
  }
  #info {
    flex: 0 0 auto; margin-bottom: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 5vh; color: #bbbbbb;
  }
  @keyframes flashYellow { from { background: #f5c518; } to { background: #1e1e1e; } }
  @keyframes flashBlue { from { background: #2b4bff; } to { background: #1e1e1e; } }
  @keyframes scrollAcross { from { left: 0; } to { left: calc(100% - 0.8vh); } }
  body.flash { animation: flashYellow 300ms ease-out; }
  body.flash-blue { animation: flashBlue 300ms ease-out; }
</style>
</head>
<body>
  <div id="latency">
    <div class="row">
      <span>Délai</span>
      <input type="range" id="latencySlider" min="-60" max="60" step="1" value="0">
      <span id="latencyValue">0 ms</span>
    </div>
    <div class="ticks"><span>-60</span><span>0 (référence)</span><span>+60</span></div>
  </div>
  <div id="beat">
    <div id="dot">•</div>
    <div id="digit"></div>
    <div id="scrollLine"></div>
  </div>
  <div id="info">-- BPM</div>
<script>
const KEY = 'beatDisplayLatencyMs';
const slider = document.getElementById('latencySlider');
const latencyValueEl = document.getElementById('latencyValue');
slider.value = localStorage.getItem(KEY) || 0;
latencyValueEl.textContent = slider.value + ' ms';
slider.addEventListener('input', () => {
  latencyValueEl.textContent = slider.value + ' ms';
  localStorage.setItem(KEY, slider.value);
});

let lastBeat = null;
let scrolling = false;
const beatEl = document.getElementById('beat');
const dotEl = document.getElementById('dot');
const digitEl = document.getElementById('digit');
const scrollLineEl = document.getElementById('scrollLine');

function retrigger(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth; // force le recalcul de style pour rejouer l'animation
  el.classList.add(cls);
}

async function poll() {
  try {
    const res = await fetch('/state?latency_ms=' + slider.value, {cache: 'no-store'});
    const data = await res.json();
    const infoEl = document.getElementById('info');
    if (data.connected) {
      dotEl.style.display = 'none';
      scrollLineEl.style.display = 'none';
      scrolling = false;
      digitEl.style.display = 'block';
      digitEl.textContent = data.beat;
      if (data.beat !== lastBeat) {
        lastBeat = data.beat;
        if (data.beat === 1) {
          document.body.classList.remove('flash-blue');
          retrigger(document.body, 'flash');
        } else if (data.beat === 3) {
          document.body.classList.remove('flash');
          retrigger(document.body, 'flash-blue');
        }
      }
    } else {
      digitEl.style.display = 'none';
      lastBeat = null;
      if (data.bpm) {
        // À l'arrêt (mais tempo connu) : une ligne blanche défile au milieu
        // de l'écran, en boucle sur la durée d'une mesure.
        dotEl.style.display = 'none';
        if (!scrolling) {
          scrolling = true;
          const barMs = (data.beats_per_bar || 4) * 60000 / data.bpm;
          scrollLineEl.style.display = 'block';
          scrollLineEl.style.animation = 'none';
          void scrollLineEl.offsetWidth;
          scrollLineEl.style.animation = `scrollAcross ${barMs}ms linear infinite`;
        }
      } else {
        // Pas de signal fiable ni de tempo connu : un chiffre ou une ligne
        // affichés au hasard seraient trompeurs.
        scrollLineEl.style.display = 'none';
        scrolling = false;
        dotEl.style.display = 'block';
      }
    }
    let suffix = '';
    if (data.mode === 'link') {
      // Le "Lecture/Arrêt" Link nécessite un réglage optionnel côté Live :
      // on ne l'affiche que quand il est confirmé, jamais en faux négatif.
      suffix = data.running ? ' (lecture)' : '';
    } else {
      suffix = data.running ? '' : ' (arrêté)';
    }
    infoEl.textContent = (data.bpm ? data.bpm.toFixed(1) : '--') + ' BPM' + suffix;
  } catch (e) {
    // Réseau momentanément indisponible : on réessaie au prochain tick.
  }
}
setInterval(poll, 60);
poll();
</script>
</body>
</html>
"""


def project_phase(phase: float, bpm: float | None, extrapolate: bool, latency_ms: float) -> float:
    """Projette une phase dans le temps en tenant compte d'un décalage.

    `latency_ms` positif avance l'affichage (compense la latence perçue par
    le spectateur) ; l'extrapolation n'a lieu que si `extrapolate` est vrai,
    pour ne pas faire "avancer" un compteur sans source fiable.
    """
    if bpm and extrapolate and latency_ms:
        phase = phase + (latency_ms / 1000.0) * (bpm / 60.0)
    return phase


def project_beat(phase: float, beats_per_bar: float, bpm: float | None, extrapolate: bool, latency_ms: float) -> int:
    """Numéro de temps (1..beats_per_bar) après projection de la phase."""
    beats_per_bar = beats_per_bar or 4
    phase = project_phase(phase, bpm, extrapolate, latency_ms)
    return int(phase % beats_per_bar) + 1


class SharedBeatState:
    """Etat courant (phase/tempo bruts), partagé entre l'UI Tkinter et le serveur web."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "phase": 0.0,
            "beats_per_bar": 4,
            "bpm": None,
            "connected": False,
            "running": False,
            "mode": "link",
            "ref_monotonic": time.monotonic(),
        }

    def update(
        self, phase: float, beats_per_bar: float, bpm: float | None,
        connected: bool, running: bool, mode: str,
    ) -> None:
        """`connected` = source fiable (à utiliser pour figer/afficher le
        compteur) ; `running` = état lecture/arrêt confirmé (peut être
        inconnu pour Link sans "Start Stop Sync", voir _update_display)."""
        with self._lock:
            self._data = {
                "phase": phase,
                "beats_per_bar": beats_per_bar,
                "bpm": bpm,
                "connected": connected,
                "running": running,
                "mode": mode,
                "ref_monotonic": time.monotonic(),
            }

    def compute(self, latency_ms: float = 0.0) -> dict:
        """Calcule {beat, bpm, connected, running, mode} pour un décalage donné."""
        with self._lock:
            data = dict(self._data)
        # Le rafraîchissement (toutes les ~30ms) garde la référence quasi à
        # jour : on ajoute le petit delta réel au décalage demandé.
        elapsed_ms = (time.monotonic() - data["ref_monotonic"]) * 1000.0
        beat = project_beat(
            data["phase"], data["beats_per_bar"], data["bpm"], data["connected"],
            latency_ms + (elapsed_ms if data["connected"] else 0.0),
        )
        return {
            "beat": beat, "beats_per_bar": data["beats_per_bar"], "bpm": data["bpm"],
            "connected": data["connected"], "running": data["running"], "mode": data["mode"],
        }



def _make_handler(shared_state: SharedBeatState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # silence les logs console
            pass

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/state":
                query = parse_qs(parsed.query)
                try:
                    latency_ms = float(query.get("latency_ms", ["0"])[0])
                except ValueError:
                    latency_ms = 0.0
                body = json.dumps(shared_state.compute(latency_ms)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    return Handler


def local_ip() -> str:
    """Meilleure estimation de l'IP locale (celle utilisée pour sortir sur le réseau)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class BeatWebServer:
    def __init__(self, shared_state: SharedBeatState, port: int = 8765):
        self.shared_state = shared_state
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        handler = _make_handler(self.shared_state)
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def url(self) -> str:
        return f"http://{local_ip()}:{self.port}"
