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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Sons de clic (extraits d'Ableton Live, usage privé local, voir .gitignore).
_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"
_SOUND_FILES = {"click.wav", "click_up.wav"}

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
    position: absolute; left: 15%; right: 15%; top: 50%;
    height: 0.8vh; transform: translateY(-50%);
    overflow: hidden;
    background: rgba(245, 245, 245, 0.15);
  }
  #scrollThumb {
    position: absolute; top: 0; bottom: 0; left: 0;
    width: 0%; background: #f5f5f5;
    transition: width 0.06s linear;
  }
  #sceneName {
    flex: 0 0 auto; margin-top: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 3.5vh; color: #ff4d4d;
    min-height: 1em;
  }
  #sceneName.launched { color: #3ddc57; font-size: 5.25vh; }
  #barCount {
    flex: 0 0 auto; margin-top: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 5vh; font-weight: bold; color: #bbbbbb;
    min-height: 1em;
  }
  #offline {
    display: none;
    font-size: 10vh; font-weight: bold; color: #ff4d4d;
    -webkit-user-select: none; user-select: none;
  }
  #info {
    flex: 0 0 auto; margin-bottom: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 5vh; color: #bbbbbb;
  }
  @keyframes flashYellow { from { background: #f5c518; } to { background: #1e1e1e; } }
  @keyframes flashBlue { from { background: #2b4bff; } to { background: #1e1e1e; } }
  @keyframes flashWhite { from { background: #ffffff; } to { background: #1e1e1e; } }
  body.flash { animation: flashYellow 300ms ease-out; }
  body.flash-blue { animation: flashBlue 300ms ease-out; }
  body.flash-white { animation: flashWhite 150ms ease-out; }
  #muteBtn {
    margin-top: 8px; padding: 6px 16px; font-size: 2.4vh;
    background: #333333; color: #f5f5f5; border: none; border-radius: 6px;
    -webkit-user-select: none; user-select: none;
  }
  #muteBtn.unmuted { background: #2b7a2b; }
</style>
</head>
<body>
  <div id="latency">
    <div class="row">
      <span>Délai</span>
      <input type="range" id="latencySlider" min="-120" max="120" step="1" value="0">
    </div>
    <div class="ticks"><span>Retard</span><span>Référence</span><span>Avance</span></div>
    <button id="muteBtn">🔇 Son coupé</button>
  </div>
  <div id="beat">
    <div id="dot">•</div>
    <div id="digit"></div>
    <div id="offline">OFFLINE</div>
    <div id="scrollLine"><div id="scrollThumb"></div></div>
  </div>
  <div id="barCount"></div>
  <div id="sceneName"></div>
  <div id="info">-- BPM</div>
<script>
const KEY = 'beatDisplayLatencyMs';
const slider = document.getElementById('latencySlider');
slider.value = localStorage.getItem(KEY) || 0;
slider.addEventListener('input', () => {
  localStorage.setItem(KEY, slider.value);
});

let lastBeat = null;
let lastBarPhase = 0;
let lastSceneLaunched = false;
const SCENE_FLASH_PULSE_MS = 150;
const SCENE_FLASH_GAP_MS = 100;

function sceneFlashDouble() {
  retrigger(document.body, 'flash-white');
  setTimeout(() => document.body.classList.remove('flash-white'), SCENE_FLASH_PULSE_MS);
  setTimeout(() => {
    retrigger(document.body, 'flash-white');
    setTimeout(() => document.body.classList.remove('flash-white'), SCENE_FLASH_PULSE_MS);
  }, SCENE_FLASH_PULSE_MS + SCENE_FLASH_GAP_MS);
}
const beatEl = document.getElementById('beat');
const dotEl = document.getElementById('dot');
const digitEl = document.getElementById('digit');
const scrollLineEl = document.getElementById('scrollLine');
const scrollThumbEl = document.getElementById('scrollThumb');

const MUTE_KEY = 'beatDisplayMuted';
const muteBtn = document.getElementById('muteBtn');
let muted = localStorage.getItem(MUTE_KEY) !== '0';

// Web Audio API plutôt que <audio> : lecture bien plus précise/rapide,
// nécessaire pour rester synchrone avec l'affichage des chiffres.
let audioCtx = null;
let clickBuffer = null;
let clickUpBuffer = null;

async function loadBuffer(ctx, url) {
  const res = await fetch(url);
  const data = await res.arrayBuffer();
  return await ctx.decodeAudioData(data);
}

async function initAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioCtx.state === 'suspended') {
    await audioCtx.resume();
  }
  if (!clickBuffer) clickBuffer = await loadBuffer(audioCtx, '/sounds/click.wav');
  if (!clickUpBuffer) clickUpBuffer = await loadBuffer(audioCtx, '/sounds/click_up.wav');
}

function updateMuteBtn() {
  muteBtn.textContent = muted ? '🔇 Son coupé' : '🔊 Son actif';
  muteBtn.classList.toggle('unmuted', !muted);
}
updateMuteBtn();
if (!muted) {
  initAudio().catch(() => {});
}

// iOS/Safari ne débloque le son que sur un vrai geste utilisateur : le
// premier tap n'importe où sur la page relance le contexte s'il est encore
// suspendu (ex. init faite au chargement de la page, sans geste).
function unlockAudio() {
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume().catch(() => {});
  }
}
document.addEventListener('pointerdown', unlockAudio);
document.addEventListener('touchend', unlockAudio);

muteBtn.addEventListener('click', () => {
  muted = !muted;
  localStorage.setItem(MUTE_KEY, muted ? '1' : '0');
  updateMuteBtn();
  if (!muted) {
    // Débloque/charge l'audio (nécessite un geste utilisateur sur mobile).
    initAudio().catch(() => {});
  }
});

function playClick(beat) {
  if (muted || !audioCtx || !clickBuffer || !clickUpBuffer) return;
  const source = audioCtx.createBufferSource();
  source.buffer = beat === 1 ? clickUpBuffer : clickBuffer;
  source.connect(audioCtx.destination);
  source.start(0);
}

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
    const offlineEl = document.getElementById('offline');
    if (data.offline) {
      dotEl.style.display = 'none';
      digitEl.style.display = 'none';
      scrollLineEl.style.display = 'none';
      offlineEl.style.display = 'block';
      lastBeat = null;
      return;
    }
    offlineEl.style.display = 'none';
    if (data.connected) {
      dotEl.style.display = 'none';
      scrollLineEl.style.display = 'none';
      digitEl.style.display = 'block';
      digitEl.textContent = data.beat;
      if (data.beat !== lastBeat) {
        lastBeat = data.beat;
        playClick(data.beat);
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
      if (data.bpm && !data.running) {
        // À l'arrêt (mais tempo connu) : la ligne se remplit de gauche à
        // droite, synchronisée sur le temps réel (vide au temps 1, pleine
        // à la fin du dernier temps de la mesure).
        dotEl.style.display = 'none';
        scrollLineEl.style.display = 'block';
        const barPhase = data.bar_phase || 0;
        if (barPhase < lastBarPhase) {
          // Nouvelle mesure : on revide instantanément, sans animer le retour à 0.
          scrollThumbEl.style.transition = 'none';
          scrollThumbEl.style.width = '0%';
          void scrollThumbEl.offsetWidth;
          scrollThumbEl.style.transition = '';
        }
        lastBarPhase = barPhase;
        scrollThumbEl.style.width = (barPhase * 100) + '%';
      } else {
        // Pas de tempo connu, ou transport relancé mais pas encore
        // resynchronisé sur le temps 1 : rien qui induirait en erreur.
        scrollLineEl.style.display = 'none';
        dotEl.style.display = data.bpm ? 'none' : 'block';
      }
    }
    document.getElementById('sceneName').textContent = data.scene_name || '';
    document.getElementById('sceneName').classList.toggle('launched', !!data.scene_launched);
    document.getElementById('barCount').textContent = data.bar_count ? ('Mes. ' + data.bar_count) : '';
    if (data.scene_launched && !lastSceneLaunched) {
      sceneFlashDouble();
    }
    lastSceneLaunched = !!data.scene_launched;
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
        self._scene_name = ""
        self._scene_launched = False
        self._bar_count: int | None = None
        self._offline = False

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

    def set_scene_name(self, scene_name: str) -> None:
        """Nom de scène Live affiché sur la page web (mis à jour indépendamment
        de `update()`, qui tourne toutes les ~30ms et ne connaît pas la scène).
        Une nouvelle sélection de scène repart toujours à l'état « non lancée »."""
        with self._lock:
            self._scene_name = scene_name
            self._scene_launched = False

    def set_scene_launched(self, launched: bool) -> None:
        with self._lock:
            self._scene_launched = launched

    def set_bar_count(self, bar_count: int | None) -> None:
        """Numéro de mesure depuis le lancement du morceau en cours (voir
        beat_display._update_bar_count), None = rien à afficher."""
        with self._lock:
            self._bar_count = bar_count

    def set_offline(self) -> None:
        """Signale la fermeture imminente de CLIC : affiche OFFLINE sur la
        page web à la place des chiffres/de la ligne, avant même que le
        serveur web ne s'arrête réellement (voir BeatDisplayApp.on_close)."""
        with self._lock:
            self._offline = True

    def compute(self, latency_ms: float = 0.0) -> dict:
        """Calcule {beat, bpm, connected, running, mode} pour un décalage donné."""
        with self._lock:
            data = dict(self._data)
            scene_name = self._scene_name
            scene_launched = self._scene_launched
            bar_count = self._bar_count
            offline = self._offline
        # Le rafraîchissement (toutes les ~30ms) garde la référence quasi à
        # jour : on ajoute le petit delta réel au décalage demandé.
        elapsed_ms = (time.monotonic() - data["ref_monotonic"]) * 1000.0
        total_latency_ms = latency_ms + (elapsed_ms if data["connected"] else 0.0)
        beat = project_beat(data["phase"], data["beats_per_bar"], data["bpm"], data["connected"], total_latency_ms)
        beats_per_bar = data["beats_per_bar"] or 4
        phase = project_phase(data["phase"], data["bpm"], data["connected"], total_latency_ms)
        # Position (0..1) dans la mesure : synchronise l'animation d'attente
        # (segment défilant) sur le tempo réel, même à l'arrêt.
        bar_phase = (phase % beats_per_bar) / beats_per_bar
        return {
            "beat": beat, "beats_per_bar": data["beats_per_bar"], "bpm": data["bpm"],
            "bar_phase": bar_phase,
            "connected": data["connected"], "running": data["running"], "mode": data["mode"],
            "scene_name": scene_name,
            "scene_launched": scene_launched,
            "bar_count": bar_count,
            "offline": offline,
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
            elif parsed.path.startswith("/sounds/") and parsed.path[len("/sounds/"):] in _SOUND_FILES:
                file_path = _SOUNDS_DIR / parsed.path[len("/sounds/"):]
                if file_path.is_file():
                    body = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
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
