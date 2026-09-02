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
    /* 1px de plus que l'écran : rend la page "défilante" (voir le script en
       bas de page) pour que Safari accepte de replier sa barre d'adresse,
       comme sur un site normal — invisible, aucun contenu ne bouge. */
    min-height: calc(100dvh + 1px);
    background: #1e1e1e; color: #f5f5f5;
    font-family: -apple-system, Helvetica, Arial, sans-serif;
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior-y: none;
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
  /* Paysage : hauteur d'écran trop réduite pour se permettre cette rangée en
     plus des boutons (voir #btnRow, gardé). */
  @media (orientation: landscape) {
    #latency .row { display: none; }
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
  #dotsPair {
    display: none;
    gap: 6vw;
  }
  #dotsPair .circle {
    width: 18vh; height: 18vh; border-radius: 50%;
    border: 3px solid #f5f5f5; box-sizing: border-box;
  }
  #dotsPair .circle.filled { background: #f5f5f5; }
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
  #sceneLabel {
    flex: 0 0 auto; margin-top: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 4vh; font-weight: bold; color: #7fb2ff;
    min-height: 1em;
  }
  /* Label suivant (scene_sheet.py, voir _apply_scene_sheet_row) : annoncé à
     droite du label courant, dans le même groupe centré (justify-content sur
     le parent #sceneLabel ci-dessus centre les deux ensemble ; margin-left
     appliqué seulement via .hasNext pour que le label courant reste centré
     seul quand il n'y a pas d'annonce). Blanc clignotant SANS fade
     (steps(1,end) : bascule nette, pas d'interpolation), durée calée sur une
     demi-temps au tempo courant, retriggé à CHAQUE temps (classe .pulse
     propre à l'élément, pas les classes body.flash/flash-blue qui suivent
     un rythme différent), jamais recréé au poll (seul le textContent
     change) pour ne pas interrompre l'animation en cours. */
  #sceneLabelNext { color: transparent; }
  #sceneLabelNext.hasNext { margin-left: 0.5em; }
  #sceneLabelNext.pulse {
    animation: sceneLabelNextPulse 300ms steps(1, end);
  }
  @keyframes sceneLabelNextPulse {
    from { color: #ffffff; }
    to { color: transparent; }
  }
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
  #btnRow {
    display: flex; flex-direction: row; justify-content: center;
    gap: 8px; margin-top: 8px;
  }
  #btnRow button {
    padding: 6px 14px; font-size: 2.2vh;
    background: #333333; color: #f5f5f5; border: none; border-radius: 6px;
    -webkit-user-select: none; user-select: none;
  }
  #btnRow button.active { background: #2b7a2b; }
  #lyricsScroll {
    display: none;
    flex: 0 0 auto; position: relative; overflow: hidden;
    width: 100%; height: 30vh;
    background: #000000;
    /* Étend le fond noir jusqu'au vrai bas d'écran, par-dessus la marge de
       sécurité réservée par le body (sinon le fond gris du métronome
       apparaît sous les paroles) : le body coupe (overflow hidden) pile au
       bord de l'écran, donc ce débordement négatif ne dépasse jamais. */
    margin-bottom: calc(-1 * max(14px, env(safe-area-inset-bottom)));
    /* pan-y (et non "none") : le geste vertical reste dispo pour Safari
       (replie sa barre d'adresse comme sur les autres pages), tout en
       bloquant zoom/pan horizontal. Le calage de hauteur (voir pointermove)
       fonctionne pareil, les événements pointer ne dépendent pas de ceci. */
    touch-action: pan-y;
    -webkit-user-select: none; user-select: none;
  }
  #lyricsScroll .lyricsLineText {
    position: absolute; left: 0; right: 0;
    text-align: center; padding: 0 4vw;
    font-size: 2.9vw; font-weight: bold; color: #f5f5f5;
    transform: translateY(-50%);
    /* Autorise le retour à la ligne, plafonné à 4 lignes visuelles par
       entrée CSV (au-delà, tronqué avec … plutôt que de déborder). */
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 4;
    overflow: hidden;
  }
  /* Paysage : taille basée sur la largeur (2.9vw, mesuré pour que les lignes
     les plus fournies remplissent l'écran quel que soit le format, un vh
     seul donnait du texte minuscule sur un écran large et bas). Portrait :
     police (vh) calibrée pour que toutes les paroles tiennent dans la
     limite de 4 lignes sans déborder de la fente réservée (voir
     LYRICS_VISIBLE_LINES). */
  @media (orientation: portrait) {
    #lyricsScroll .lyricsLineText { font-size: 3.4vh; }
  }
  /* Mode paroles affichées : zone de défilement à 50% de l'écran, BPM
     masqué. Le chiffre/les points ne sont réduits qu'en portrait (voir plus
     bas) : en paysage, ils gardent leur taille normale. */
  body.lyrics-mode #info { display: none; }
  body.lyrics-mode #lyricsScroll { height: 50vh; }
  @media (orientation: portrait) {
    body.lyrics-mode #digit { font-size: 30vh; }
    body.lyrics-mode #dotsPair .circle { width: 13.5vh; height: 13.5vh; }
  }
  /* Mesure HIGHLIGHT (scene_sheet.py) : chiffre/points 50% plus grands,
     comme le grand écran (HIGHLIGHT_SIZE_SCALE) — jamais en mode paroles,
     où la place est déjà réduite pour le défilement. */
  body.highlighted:not(.lyrics-mode) #digit { font-size: 60vh; }
  body.highlighted:not(.lyrics-mode) #dotsPair .circle { width: 27vh; height: 27vh; }
  /* Paysage + paroles affichées : le chiffre à 40vh débordait sur le label de
     section et les boutons (hauteur d'écran bien plus petite qu'en portrait)
     — réduit de 50%, uniquement dans ce cas précis. */
  @media (orientation: landscape) {
    body.lyrics-mode #digit { font-size: 20vh; }
    body.lyrics-mode #dotsPair .circle { width: 9vh; height: 9vh; }
  }
  /* Prompteur : ne laisse que le LABEL + le défilement des paroles (plein
     écran), un cadre de 10px qui flashe à la place des chiffres pour rester
     synchrone, et un unique bouton EXIT pour revenir au mode précédent. */
  #promptFrame {
    display: none;
    position: fixed; inset: 0; pointer-events: none;
    border: 10px solid transparent; box-sizing: border-box; z-index: 500;
  }
  body.prompter-mode #promptFrame { display: block; }
  body.prompter-mode #latency,
  body.prompter-mode #beat,
  body.prompter-mode #sceneName,
  body.prompter-mode #barCount,
  body.prompter-mode #info { display: none; }
  #exitPromptBtn {
    display: none;
    position: fixed; top: 10px; right: 10px; z-index: 600;
    padding: 4px 10px; font-size: 1.6vh;
    background: rgba(51, 51, 51, 0.4); color: rgba(245, 245, 245, 0.5);
    border: none; border-radius: 4px;
    -webkit-user-select: none; user-select: none;
  }
  body.prompter-mode #exitPromptBtn { display: block; }
  body.prompter-mode #lyricsScroll { height: 92vh; }
  @keyframes flashBorderYellow { from { border-color: #f5c518; } to { border-color: transparent; } }
  @keyframes flashBorderBlue { from { border-color: #2b4bff; } to { border-color: transparent; } }
  @keyframes flashBorderWhite { from { border-color: #ffffff; } to { border-color: transparent; } }
  /* En mode prompteur, le flash de fond habituel (body.flash...) est
     remplacé par le flash du cadre : jamais les deux à la fois. */
  body.prompter-mode.flash, body.prompter-mode.flash-blue, body.prompter-mode.flash-white {
    animation: none; background: #1e1e1e;
  }
  body.prompter-mode.flash #promptFrame { animation: flashBorderYellow 300ms ease-out; }
  body.prompter-mode.flash-blue #promptFrame { animation: flashBorderBlue 300ms ease-out; }
  body.prompter-mode.flash-white #promptFrame { animation: flashBorderWhite 150ms ease-out; }
  body.prompter-mode.is-offline #promptFrame { border-color: #ff4d4d; }
</style>
</head>
<body>
  <div id="promptFrame"></div>
  <div id="latency">
    <div class="row">
      <span>Délai</span>
      <input type="range" id="latencySlider" min="-120" max="120" step="1" value="0">
    </div>
    <div id="btnRow">
      <button id="muteBtn">Son coupé</button>
      <button id="lyricsBtn">Paroles masquées</button>
      <button id="dotsBtn">Points</button>
      <button id="promptBtn">PROMPTEUR</button>
    </div>
  </div>
  <div id="beat">
    <div id="dot">•</div>
    <div id="digit"></div>
    <div id="dotsPair"><div id="dotLeft" class="circle"></div><div id="dotRight" class="circle"></div></div>
    <div id="offline">OFFLINE</div>
    <div id="scrollLine"><div id="scrollThumb"></div></div>
  </div>
  <div id="sceneLabel"><span id="sceneLabelCurrent"></span><span id="sceneLabelNext"></span></div>
  <div id="barCount"></div>
  <div id="sceneName"></div>
  <div id="lyricsScroll"></div>
  <button id="exitPromptBtn">EXIT</button>
  <div id="info">-- BPM</div>
<script>
// Repliement de la barre d'adresse Safari : la page est rendue "défilante"
// de 1px (voir min-height en CSS) juste pour ça ; ce petit scroll forcé
// suffit en général à convaincre Safari de la replier, comme sur un site
// normal, sans que rien ne bouge visuellement à l'écran.
function nudgeScrollForSafariChrome() {
  window.scrollTo(0, 1);
}
window.addEventListener('load', () => setTimeout(nudgeScrollForSafariChrome, 50));
window.addEventListener('orientationchange', () => setTimeout(nudgeScrollForSafariChrome, 300));

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
const dotsPairEl = document.getElementById('dotsPair');
const dotLeftEl = document.getElementById('dotLeft');
const dotRightEl = document.getElementById('dotRight');
const scrollLineEl = document.getElementById('scrollLine');
const scrollThumbEl = document.getElementById('scrollThumb');

// Chiffres/points : préférence propre à cet appareil (comme le son et les
// paroles), indépendante du réglage équivalent du grand écran.
const DOTS_KEY = 'beatDisplayShowDots';
const dotsBtn = document.getElementById('dotsBtn');
let showDots = localStorage.getItem(DOTS_KEY) === '1';

function updateDotsBtn() {
  dotsBtn.textContent = showDots ? 'Chiffres' : 'Points';
  dotsBtn.classList.toggle('active', showDots);
}
updateDotsBtn();

dotsBtn.addEventListener('click', () => {
  showDots = !showDots;
  localStorage.setItem(DOTS_KEY, showDots ? '1' : '0');
  updateDotsBtn();
});

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
  muteBtn.textContent = muted ? 'Son coupé' : 'Son actif';
  muteBtn.classList.toggle('active', !muted);
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

// Affichage des paroles : préférence propre à cet appareil (indépendante de
// la case «Afficher les paroles» du grand écran), désactivée par défaut.
// Retire aussi le nom du morceau et le numéro de mesure pour libérer de la
// place à l'écran (le défilement des paroles les remplace).
const LYRICS_KEY = 'beatDisplayShowLyrics';
const LYRICS_BEATS_PER_LINE = 8;
// LYRICS_VISIBLE_LINES réduit (était 4) : interligne plus grand entre les
// entrées CSV, pour laisser la place au retour à la ligne (jusqu'à 4 lignes,
// voir -webkit-line-clamp) sans chevaucher l'entrée voisine — fait aussi
// défiler le texte plus vite (même distance à parcourir en moins de "cases").
const LYRICS_VISIBLE_LINES = 3;
const lyricsBtn = document.getElementById('lyricsBtn');
const lyricsScrollEl = document.getElementById('lyricsScroll');
const sceneNameEl = document.getElementById('sceneName');
const barCountEl = document.getElementById('barCount');
const lyricsLineEls = new Map();
let showLyrics = localStorage.getItem(LYRICS_KEY) === '1';

function updateLyricsBtn() {
  lyricsBtn.textContent = showLyrics ? 'Paroles affichées' : 'Paroles masquées';
  lyricsBtn.classList.toggle('active', showLyrics);
  document.body.classList.toggle('lyrics-mode', showLyrics);
  sceneNameEl.style.display = showLyrics ? 'none' : '';
  barCountEl.style.display = showLyrics ? 'none' : '';
  if (!showLyrics) lyricsScrollEl.style.display = 'none';
}
updateLyricsBtn();

lyricsBtn.addEventListener('click', () => {
  showLyrics = !showLyrics;
  localStorage.setItem(LYRICS_KEY, showLyrics ? '1' : '0');
  updateLyricsBtn();
});

// Mode prompteur : force l'affichage des paroles (sans toucher au réglage
// mémorisé de l'appareil) et masque tout le reste ; EXIT restaure l'état
// d'avant l'entrée dans ce mode.
const promptBtn = document.getElementById('promptBtn');
const exitPromptBtn = document.getElementById('exitPromptBtn');
let prompterPrevShowLyrics = showLyrics;

function enterPrompterMode() {
  prompterPrevShowLyrics = showLyrics;
  if (!showLyrics) {
    showLyrics = true;
    updateLyricsBtn();
  }
  document.body.classList.add('prompter-mode');
}

function exitPrompterMode() {
  document.body.classList.remove('prompter-mode');
  if (showLyrics !== prompterPrevShowLyrics) {
    showLyrics = prompterPrevShowLyrics;
    updateLyricsBtn();
  }
}

promptBtn.addEventListener('click', enterPrompterMode);
exitPromptBtn.addEventListener('click', exitPrompterMode);

// Hauteur du défilement des paroles : préférence propre à cet appareil
// (chacun règle la sienne), réglée en glissant le doigt directement sur la
// zone de paroles (au lieu d'un curseur séparé), pour recaler à la volée.
const LYRICS_HEIGHT_KEY = 'beatDisplayLyricsHeight';
let lyricsHeightRatio = parseFloat(localStorage.getItem(LYRICS_HEIGHT_KEY)) || 0.5;

let lyricsDragStartY = null;
let lyricsDragStartRatio = 0.5;

lyricsScrollEl.addEventListener('pointerdown', (event) => {
  lyricsDragStartY = event.clientY;
  lyricsDragStartRatio = lyricsHeightRatio;
  lyricsScrollEl.setPointerCapture(event.pointerId);
});

lyricsScrollEl.addEventListener('pointermove', (event) => {
  if (lyricsDragStartY === null) return;
  const boxHeight = lyricsScrollEl.clientHeight || 1;
  const deltaRatio = (event.clientY - lyricsDragStartY) / boxHeight;
  lyricsHeightRatio = Math.min(1, Math.max(0, lyricsDragStartRatio + deltaRatio));
  localStorage.setItem(LYRICS_HEIGHT_KEY, lyricsHeightRatio);
});

function endLyricsDrag(event) {
  if (lyricsDragStartY === null) return;
  lyricsDragStartY = null;
  lyricsScrollEl.releasePointerCapture(event.pointerId);
}
lyricsScrollEl.addEventListener('pointerup', endLyricsDrag);
lyricsScrollEl.addEventListener('pointercancel', endLyricsDrag);

// Molette de souris (navigateur desktop) : même effet que le glisser du doigt.
lyricsScrollEl.addEventListener('wheel', (event) => {
  event.preventDefault();
  const boxHeight = lyricsScrollEl.clientHeight || 1;
  const deltaRatio = event.deltaY / boxHeight;
  lyricsHeightRatio = Math.min(1, Math.max(0, lyricsHeightRatio + deltaRatio));
  localStorage.setItem(LYRICS_HEIGHT_KEY, lyricsHeightRatio);
}, { passive: false });

// Fait défiler les paroles à la même vitesse que le grand écran (voir
// beat_display._draw_lyrics_scroll) : chaque ligne du CSV équivaut à
// LYRICS_BEATS_PER_LINE temps, positionnée selon songBeat (position continue
// depuis le début du morceau), avec LYRICS_VISIBLE_LINES lignes visibles à la
// fois (éléments DOM réutilisés, comme les items du canvas côté bureau).
//
// Rendu via requestAnimationFrame (comme foenix7777-player-deploy/player.html
// pour son propre défilement), pas dans poll() : poll() n'arrive que toutes
// les ~60ms par le réseau, avec une gigue (latence fetch/JSON variable) qui
// rendait le texte saccadé. Ici, chaque frame extrapole localement songBeat
// depuis le dernier échantillon serveur + le temps écoulé réel (lyricsBaseBpm),
// exactement comme scheduleUpcomingClicks extrapole déjà les clics audio.
let lyricsBaseSongBeat = null; // dernier songBeat connu (voir poll)
let lyricsBaseBpm = null;      // tempo au moment de cet échantillon ; null = ne pas extrapoler (arrêté/inconnu)
let lyricsBaseAt = 0;          // performance.now() de l'échantillon
let lyricsLinesCache = [];

function renderLyricsScroll(lines, songBeat) {
  if (!showLyrics || !lines.length || typeof songBeat !== 'number') {
    lyricsScrollEl.style.display = 'none';
    return;
  }
  lyricsScrollEl.style.display = 'block';
  const boxHeight = lyricsScrollEl.clientHeight || 1;
  const pixelsPerBeat = boxHeight / (LYRICS_VISIBLE_LINES * LYRICS_BEATS_PER_LINE);
  // Hauteur réglée en glissant le doigt sur la zone (lyricsHeightRatio, 0..1), propre à cet appareil.
  const centerY = boxHeight * lyricsHeightRatio;
  const bufferPx = LYRICS_BEATS_PER_LINE * pixelsPerBeat;
  const visible = new Set();
  lines.forEach((text, index) => {
    if (!text) return;
    const y = centerY + (index * LYRICS_BEATS_PER_LINE - songBeat) * pixelsPerBeat;
    if (y < -bufferPx || y > boxHeight + bufferPx) return;
    let el = lyricsLineEls.get(index);
    if (!el) {
      el = document.createElement('div');
      el.className = 'lyricsLineText';
      lyricsScrollEl.appendChild(el);
      lyricsLineEls.set(index, el);
    }
    el.textContent = text;
    el.style.top = y + 'px';
    el.style.display = 'block';
    visible.add(index);
  });
  for (const [index, el] of lyricsLineEls) {
    if (!visible.has(index)) el.style.display = 'none';
  }
}

function lyricsAnimationLoop() {
  let songBeat = lyricsBaseSongBeat;
  if (songBeat !== null && lyricsBaseBpm) {
    const elapsedS = (performance.now() - lyricsBaseAt) / 1000;
    songBeat += elapsedS * (lyricsBaseBpm / 60);
  }
  renderLyricsScroll(lyricsLinesCache, songBeat);
  requestAnimationFrame(lyricsAnimationLoop);
}
requestAnimationFrame(lyricsAnimationLoop);

// Planification "lookahead" (horloge audio, pas le timer JS) : au lieu de
// jouer le clic au moment où poll() détecte un changement de temps (sujet
// aux à-coups du setInterval/réseau, cause des lags occasionnels), on
// planifie les prochains clics un peu à l'avance via audioCtx.currentTime.
// Une fois planifié, un clic part à l'heure pile même si le thread JS
// bloque momentanément après coup.
let nextClickAt = null;   // audioCtx.currentTime du prochain clic déjà planifié
let nextClickBeat = null; // numéro de temps (1..beats_per_bar) de ce clic
const SCHEDULE_AHEAD_S = 0.15;

function scheduleClick(at, beat) {
  const source = audioCtx.createBufferSource();
  source.buffer = beat === 1 ? clickUpBuffer : clickBuffer;
  source.connect(audioCtx.destination);
  source.start(at);
}

function scheduleUpcomingClicks(data) {
  if (muted || data.metronome_end_muted || !audioCtx || !clickBuffer || !clickUpBuffer || !data.bpm) return;
  const beatsPerBar = data.beats_per_bar || 4;
  const secondsPerBeat = 60 / data.bpm;
  const fractionalBeat = (data.bar_phase * beatsPerBar) % 1;
  const now = audioCtx.currentTime;
  const trueNextAt = now + (1 - fractionalBeat) * secondsPerBeat;
  const trueNextBeat = (data.beat % beatsPerBar) + 1;
  // Écart trop grand avec le planning en cours (reconnexion, changement de
  // tempo, saut de scène) : on repart de la vraie position du serveur.
  if (nextClickAt === null || Math.abs(trueNextAt - nextClickAt) > secondsPerBeat * 0.5) {
    nextClickAt = trueNextAt;
    nextClickBeat = trueNextBeat;
  }
  while (nextClickAt < now + SCHEDULE_AHEAD_S) {
    scheduleClick(nextClickAt, nextClickBeat);
    nextClickAt += secondsPerBeat;
    nextClickBeat = (nextClickBeat % beatsPerBar) + 1;
  }
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
    document.body.classList.toggle('highlighted', !!data.highlighted);
    document.body.classList.toggle('is-offline', !!data.offline);
    if (data.offline) {
      dotEl.style.display = 'none';
      digitEl.style.display = 'none';
      dotsPairEl.style.display = 'none';
      scrollLineEl.style.display = 'none';
      offlineEl.style.display = 'block';
      lastBeat = null;
      nextClickAt = null;
      return;
    }
    offlineEl.style.display = 'none';
    if (data.connected) {
      dotEl.style.display = 'none';
      scrollLineEl.style.display = 'none';
      if (showDots) {
        digitEl.style.display = 'none';
        dotsPairEl.style.display = 'flex';
        const leftFilled = data.beat % 2 === 1;
        dotLeftEl.classList.toggle('filled', leftFilled);
        dotRightEl.classList.toggle('filled', !leftFilled);
      } else {
        dotsPairEl.style.display = 'none';
        digitEl.style.display = 'block';
        digitEl.textContent = data.beat;
      }
      scheduleUpcomingClicks(data);
      if (data.beat !== lastBeat) {
        lastBeat = data.beat;
        if (data.beat === 1) {
          document.body.classList.remove('flash-blue');
          retrigger(document.body, 'flash');
        } else if (data.beat === 3) {
          document.body.classList.remove('flash');
          retrigger(document.body, 'flash-blue');
        }
        const nextLabelPulseEl = document.getElementById('sceneLabelNext');
        // 2x plus vite que l'ancien "un temps sur deux" : retrigger à CHAQUE
        // temps désormais.
        if (data.bpm) nextLabelPulseEl.style.animationDuration = (30000 / data.bpm) + 'ms';
        retrigger(nextLabelPulseEl, 'pulse');
      }
    } else {
      digitEl.style.display = 'none';
      dotsPairEl.style.display = 'none';
      lastBeat = null;
      nextClickAt = null;
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
    document.getElementById('sceneLabelCurrent').textContent = data.scene_label || '';
    const nextLabelEl = document.getElementById('sceneLabelNext');
    nextLabelEl.textContent = data.next_scene_label ? (' ' + data.next_scene_label) : '';
    nextLabelEl.classList.toggle('hasNext', !!data.next_scene_label);
    lyricsLinesCache = Array.isArray(data.lyrics_lines) ? data.lyrics_lines : [];
    if (typeof data.lyrics_song_beat === 'number') {
      lyricsBaseSongBeat = data.lyrics_song_beat;
      // N'extrapole (voir lyricsAnimationLoop) que si la lecture avance
      // vraiment : sinon (arrêté/inconnu) la position reste figée pile sur
      // le dernier échantillon, comme avant.
      lyricsBaseBpm = (data.connected && typeof data.bpm === 'number') ? data.bpm : null;
      lyricsBaseAt = performance.now();
    } else {
      lyricsBaseSongBeat = null;
      lyricsBaseBpm = null;
    }
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

# Page minimale, en JS ES5 pur (var/function, XMLHttpRequest, pas de
# fetch/const/let/arrow functions/template literals/Promise/Map) : Safari sur
# iOS 9.3.5 (iPad mini 1/iPad 2, bloqués à cette version) exécute un moteur
# JS trop ancien pour _PAGE, dont le premier "const"/arrow function fait
# échouer TOUT le script (page qui reste figée sur les valeurs par défaut,
# HTML/CSS seuls rendus). Réutilise le même /state (JSON, indépendant du JS
# client) et les mêmes /sounds/*.wav : aucun changement côté serveur/Tk
# nécessaire. Repris de _PAGE : bouton Points (chiffre/2 points), bouton
# Muet (Web Audio API, supportée dès iOS 6 via le préfixe webkit, chargée en
# XHR+decodeAudioData plutôt que fetch), bouton Paroles (défilement, glissé
# au doigt via touchstart/touchmove/touchend — les Pointer Events n'existent
# pas avant Safari 13). Mêmes clés localStorage que _PAGE (réglages
# communs, même origine).
_LEGACY_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Temps - Ableton Live</title>
<style>
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: #1e1e1e; color: #f5f5f5;
    font-family: Helvetica, Arial, sans-serif;
    overflow: hidden;
  }
  @-webkit-keyframes flashYellow { from { background: #f5c518; } to { background: #1e1e1e; } }
  @-webkit-keyframes flashBlue { from { background: #2b4bff; } to { background: #1e1e1e; } }
  @keyframes flashYellow { from { background: #f5c518; } to { background: #1e1e1e; } }
  @keyframes flashBlue { from { background: #2b4bff; } to { background: #1e1e1e; } }
  body.flash { -webkit-animation: flashYellow 300ms ease-out; animation: flashYellow 300ms ease-out; }
  body.flash-blue { -webkit-animation: flashBlue 300ms ease-out; animation: flashBlue 300ms ease-out; }
  body { display: -webkit-box; display: -webkit-flex; display: flex; -webkit-flex-direction: column; flex-direction: column; }
  #latencyRow {
    -webkit-flex: 0 0 auto; flex: 0 0 auto;
    padding: 8px 0; text-align: center; font-size: 2.2vh; color: #888888;
  }
  #latencyRow input[type=range] { width: 60%; vertical-align: middle; }
  #btnRow { margin-top: 6px; }
  #btnRow button {
    padding: 6px 14px; font-size: 2.2vh; margin: 0 4px;
    background: #333333; color: #f5f5f5; border: none; border-radius: 6px;
  }
  #btnRow button.active { background: #2b7a2b; }
  #beat {
    -webkit-box-flex: 1; -webkit-flex: 1 1 auto; flex: 1 1 auto;
    position: relative;
    display: -webkit-box; display: -webkit-flex; display: flex;
    -webkit-flex-direction: column; flex-direction: column;
    -webkit-box-pack: center; -webkit-justify-content: center; justify-content: center;
    -webkit-box-align: center; -webkit-align-items: center; align-items: center;
    text-align: center;
  }
  #dot { display: none; font-size: 20vh; color: #f5f5f5; margin: 1vh 0; }
  #digit { display: none; font-size: 40vh; font-weight: bold; color: #f5f5f5; margin: 1vh 0; }
  #dotsPair { display: none; margin: 1vh 0; }
  #dotsPair .circle {
    display: inline-block; width: 18vh; height: 18vh; border-radius: 50%;
    border: 3px solid #f5f5f5; box-sizing: border-box; margin: 0 3vw;
  }
  #dotsPair .circle.filled { background: #f5f5f5; }
  #scrollLine {
    display: none;
    position: absolute; left: 15%; right: 15%; top: 50%;
    height: 0.8vh; margin-top: -0.4vh;
    overflow: hidden;
    background: rgba(245, 245, 245, 0.15);
  }
  #scrollThumb {
    position: absolute; top: 0; bottom: 0; left: 0;
    width: 0%; background: #f5f5f5;
  }
  #offline { display: none; font-size: 10vh; font-weight: bold; text-align: center; color: #ff4d4d; }
  #sceneLabel, #barCount, #sceneName {
    -webkit-flex: 0 0 auto; flex: 0 0 auto;
    text-align: center; min-height: 1.2em;
  }
  #sceneName { font-size: 3.5vh; color: #ff4d4d; }
  #sceneName.launched { color: #3ddc57; }
  #sceneLabel { font-size: 4vh; font-weight: bold; color: #7fb2ff; }
  #sceneLabelNext { color: transparent; }
  #sceneLabelNext.hasNext { margin-left: 0.5em; }
  @-webkit-keyframes sceneLabelNextPulse { from { color: #ffffff; } to { color: transparent; } }
  @keyframes sceneLabelNextPulse { from { color: #ffffff; } to { color: transparent; } }
  #sceneLabelNext.pulse {
    -webkit-animation: sceneLabelNextPulse 300ms steps(1, end); animation: sceneLabelNextPulse 300ms steps(1, end);
  }
  #barCount { font-size: 5vh; font-weight: bold; color: #bbbbbb; }
  #info {
    -webkit-flex: 0 0 auto; flex: 0 0 auto;
    text-align: center; font-size: 5vh; color: #bbbbbb; padding: 6px 0;
  }
  #lyricsScroll {
    display: none;
    -webkit-flex: 0 0 auto; flex: 0 0 auto;
    position: relative; overflow: hidden;
    width: 100%; height: 30vh;
    background: #000000;
  }
  #lyricsScroll .lyricsLineText {
    position: absolute; left: 0; right: 0;
    text-align: center; padding: 0 4vw;
    font-size: 3.6vh; font-weight: bold; color: #f5f5f5;
    margin-top: -1.5em;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 4;
    overflow: hidden;
  }
  /* Paysage : taille basée sur la largeur (voir page principale) pour que
     les lignes les plus fournies remplissent l'écran ; le portrait garde
     3.6vh (comportement historique, non modifié). */
  @media (orientation: landscape) {
    #lyricsScroll .lyricsLineText { font-size: 2.9vw; }
  }
  body.lyrics-mode #info { display: none; }
  body.lyrics-mode #lyricsScroll { height: 50vh; }
  body.lyrics-mode #digit { font-size: 25vh; }
  #promptFrame {
    display: none;
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    border: 10px solid transparent; box-sizing: border-box; z-index: 500;
    pointer-events: none;
  }
  body.prompter-mode #promptFrame { display: block; }
  body.prompter-mode #latencyRow,
  body.prompter-mode #beat,
  body.prompter-mode #sceneName,
  body.prompter-mode #barCount,
  body.prompter-mode #info { display: none; }
  #exitPromptBtn {
    display: none;
    position: fixed; top: 10px; right: 10px; z-index: 600;
    padding: 4px 10px; font-size: 1.6vh;
    background: rgba(51, 51, 51, 0.4); color: rgba(245, 245, 245, 0.5);
    border: none; border-radius: 4px;
  }
  body.prompter-mode #exitPromptBtn { display: block; }
  body.prompter-mode #lyricsScroll { height: 92vh; }
  @-webkit-keyframes flashBorderYellow { from { border-color: #f5c518; } to { border-color: transparent; } }
  @-webkit-keyframes flashBorderBlue { from { border-color: #2b4bff; } to { border-color: transparent; } }
  @keyframes flashBorderYellow { from { border-color: #f5c518; } to { border-color: transparent; } }
  @keyframes flashBorderBlue { from { border-color: #2b4bff; } to { border-color: transparent; } }
  body.prompter-mode.flash, body.prompter-mode.flash-blue {
    -webkit-animation: none; animation: none; background: #1e1e1e;
  }
  body.prompter-mode.flash #promptFrame {
    -webkit-animation: flashBorderYellow 300ms ease-out; animation: flashBorderYellow 300ms ease-out;
  }
  body.prompter-mode.flash-blue #promptFrame {
    -webkit-animation: flashBorderBlue 300ms ease-out; animation: flashBorderBlue 300ms ease-out;
  }
  body.prompter-mode.is-offline #promptFrame { border-color: #ff4d4d; }
</style>
</head>
<body>
  <div id="promptFrame"></div>
  <div id="latencyRow">
    <span>Delai</span>
    <input type="range" id="latencySlider" min="-120" max="120" step="1" value="0">
    <div id="btnRow">
      <button id="muteBtn">Son coupe</button>
      <button id="lyricsBtn">Paroles masquees</button>
      <button id="dotsBtn">Points</button>
      <button id="promptBtn">PROMPTEUR</button>
    </div>
  </div>
  <div id="beat">
    <div id="dot">&bull;</div>
    <div id="digit">--</div>
    <div id="dotsPair"><span id="dotLeft" class="circle"></span><span id="dotRight" class="circle"></span></div>
    <div id="offline">OFFLINE</div>
    <div id="scrollLine"><div id="scrollThumb"></div></div>
  </div>
  <div id="sceneLabel"><span id="sceneLabelCurrent"></span><span id="sceneLabelNext"></span></div>
  <div id="barCount"></div>
  <div id="sceneName"></div>
  <div id="lyricsScroll"></div>
  <button id="exitPromptBtn">EXIT</button>
  <div id="info">-- BPM</div>
<script>
var KEY = 'beatDisplayLatencyMs';
var slider = document.getElementById('latencySlider');
var saved = localStorage.getItem(KEY);
if (saved !== null) { slider.value = saved; }
function saveLatency() { localStorage.setItem(KEY, slider.value); }
slider.onchange = saveLatency;
slider.oninput = saveLatency;

var dotEl = document.getElementById('dot');
var digitEl = document.getElementById('digit');
var dotsPairEl = document.getElementById('dotsPair');
var dotLeftEl = document.getElementById('dotLeft');
var dotRightEl = document.getElementById('dotRight');
var scrollLineEl = document.getElementById('scrollLine');
var scrollThumbEl = document.getElementById('scrollThumb');
var offlineEl = document.getElementById('offline');
var infoEl = document.getElementById('info');
var sceneNameEl = document.getElementById('sceneName');
var barCountEl = document.getElementById('barCount');
var sceneLabelCurrentEl = document.getElementById('sceneLabelCurrent');
var sceneLabelNextEl = document.getElementById('sceneLabelNext');
var lastBeat = null;
var lastBarPhase = 0;

function retrigger(className) {
  // Force le recalcul de style pour rejouer l'animation même si la même
  // classe (donc la même @keyframes) était déjà active juste avant ;
  // préserve les classes "collantes" (lyrics-mode, prompter-mode,
  // is-offline), seules autres classes possibles sur body.
  var base = stickyClassName();
  document.body.className = base;
  void document.body.offsetWidth;
  document.body.className = (base ? base + ' ' : '') + className;
}

// Comme retrigger() ci-dessus mais pour un élément quelconque (pas de
// classes "collantes" à préserver) : sert au pulse du label suivant, appelé
// à chaque temps (voir render()) pour son propre rythme,
// indépendant de body.flash/flash-blue.
function retriggerEl(el, className) {
  el.classList.remove(className);
  void el.offsetWidth;
  el.classList.add(className);
}

var STICKY_CLASSES = ['lyrics-mode', 'prompter-mode', 'is-offline'];

function stickyClassName() {
  var parts = [];
  var i;
  for (i = 0; i < STICKY_CLASSES.length; i++) {
    if (document.body.className.indexOf(STICKY_CLASSES[i]) !== -1) { parts.push(STICKY_CLASSES[i]); }
  }
  return parts.join(' ');
}

function toggleStickyClass(name, on) {
  var has = document.body.className.indexOf(name) !== -1;
  if (on && !has) {
    document.body.className = (document.body.className + ' ' + name).replace(/^\\s+/, '');
  } else if (!on && has) {
    document.body.className = document.body.className.replace(name, '').replace(/\\s+/g, ' ').replace(/^\\s+|\\s+$/g, '');
  }
}

// --- Points / chiffre ---
var DOTS_KEY = 'beatDisplayShowDots';
var dotsBtn = document.getElementById('dotsBtn');
var showDots = localStorage.getItem(DOTS_KEY) === '1';
function updateDotsBtn() {
  dotsBtn.innerHTML = showDots ? 'Chiffres' : 'Points';
  dotsBtn.className = showDots ? 'active' : '';
}
updateDotsBtn();
dotsBtn.onclick = function () {
  showDots = !showDots;
  localStorage.setItem(DOTS_KEY, showDots ? '1' : '0');
  updateDotsBtn();
};

// --- Son (Web Audio API, chargement via XHR) ---
var MUTE_KEY = 'beatDisplayMuted';
var muteBtn = document.getElementById('muteBtn');
var muted = localStorage.getItem(MUTE_KEY) !== '0';
var audioCtx = null;
var clickBuffer = null;
var clickUpBuffer = null;
var audioLoading = false;

function loadBuffer(ctx, url, onDone) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', url, true);
  xhr.responseType = 'arraybuffer';
  xhr.onload = function () {
    if (xhr.status !== 200) { return; }
    ctx.decodeAudioData(xhr.response, function (buffer) { onDone(buffer); }, function () {});
  };
  xhr.send(null);
}

function initAudio() {
  if (audioLoading) { return; }
  audioLoading = true;
  var Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) { return; }
  if (!audioCtx) { audioCtx = new Ctor(); }
  if (audioCtx.state === 'suspended' && audioCtx.resume) { audioCtx.resume(); }
  if (!clickBuffer) { loadBuffer(audioCtx, '/sounds/click.wav', function (b) { clickBuffer = b; }); }
  if (!clickUpBuffer) { loadBuffer(audioCtx, '/sounds/click_up.wav', function (b) { clickUpBuffer = b; }); }
}

function updateMuteBtn() {
  muteBtn.innerHTML = muted ? 'Son coupe' : 'Son actif';
  muteBtn.className = muted ? '' : 'active';
}
updateMuteBtn();
if (!muted) { initAudio(); }

function unlockAudio() {
  if (audioCtx && audioCtx.state === 'suspended' && audioCtx.resume) { audioCtx.resume(); }
}
document.addEventListener('touchend', unlockAudio, false);
document.addEventListener('mousedown', unlockAudio, false);

muteBtn.onclick = function () {
  muted = !muted;
  localStorage.setItem(MUTE_KEY, muted ? '1' : '0');
  updateMuteBtn();
  if (!muted) { initAudio(); }
};

var nextClickAt = null;
var nextClickBeat = null;
var SCHEDULE_AHEAD_S = 0.15;

function scheduleClick(at, beat) {
  var source = audioCtx.createBufferSource();
  source.buffer = beat === 1 ? clickUpBuffer : clickBuffer;
  source.connect(audioCtx.destination);
  source.start(at);
}

function scheduleUpcomingClicks(data) {
  if (muted || data.metronome_end_muted || !audioCtx || !clickBuffer || !clickUpBuffer || !data.bpm) { return; }
  var beatsPerBar = data.beats_per_bar || 4;
  var secondsPerBeat = 60 / data.bpm;
  var fractionalBeat = (data.bar_phase * beatsPerBar) % 1;
  var now = audioCtx.currentTime;
  var trueNextAt = now + (1 - fractionalBeat) * secondsPerBeat;
  var trueNextBeat = (data.beat % beatsPerBar) + 1;
  if (nextClickAt === null || Math.abs(trueNextAt - nextClickAt) > secondsPerBeat * 0.5) {
    nextClickAt = trueNextAt;
    nextClickBeat = trueNextBeat;
  }
  while (nextClickAt < now + SCHEDULE_AHEAD_S) {
    scheduleClick(nextClickAt, nextClickBeat);
    nextClickAt += secondsPerBeat;
    nextClickBeat = (nextClickBeat % beatsPerBar) + 1;
  }
}

// --- Paroles ---
var LYRICS_KEY = 'beatDisplayShowLyrics';
var LYRICS_BEATS_PER_LINE = 8;
var LYRICS_VISIBLE_LINES = 3;
var LYRICS_HEIGHT_KEY = 'beatDisplayLyricsHeight';
var lyricsBtn = document.getElementById('lyricsBtn');
var lyricsScrollEl = document.getElementById('lyricsScroll');
var lyricsLineEls = {};
var showLyrics = localStorage.getItem(LYRICS_KEY) === '1';
var lyricsHeightRatio = parseFloat(localStorage.getItem(LYRICS_HEIGHT_KEY)) || 0.5;
var lyricsDragStartY = null;
var lyricsDragStartRatio = 0.5;
var lyricsBaseSongBeat = null;
var lyricsBaseBpm = null;
var lyricsBaseAt = 0;
var lyricsLinesCache = [];

function setLyricsModeClass(on) {
  toggleStickyClass('lyrics-mode', on);
}

function updateLyricsBtn() {
  lyricsBtn.innerHTML = showLyrics ? 'Paroles affichees' : 'Paroles masquees';
  lyricsBtn.className = showLyrics ? 'active' : '';
  setLyricsModeClass(showLyrics);
  sceneNameEl.style.display = showLyrics ? 'none' : '';
  barCountEl.style.display = showLyrics ? 'none' : '';
  if (!showLyrics) { lyricsScrollEl.style.display = 'none'; }
}
updateLyricsBtn();

lyricsBtn.onclick = function () {
  showLyrics = !showLyrics;
  localStorage.setItem(LYRICS_KEY, showLyrics ? '1' : '0');
  updateLyricsBtn();
};

// Mode prompteur : force l'affichage des paroles (sans toucher au reglage
// memorise de l'appareil) et masque tout le reste ; EXIT restaure l'etat
// d'avant l'entree dans ce mode.
var promptBtn = document.getElementById('promptBtn');
var exitPromptBtn = document.getElementById('exitPromptBtn');
var prompterPrevShowLyrics = showLyrics;

function enterPrompterMode() {
  prompterPrevShowLyrics = showLyrics;
  if (!showLyrics) {
    showLyrics = true;
    updateLyricsBtn();
  }
  toggleStickyClass('prompter-mode', true);
}

function exitPrompterMode() {
  toggleStickyClass('prompter-mode', false);
  if (showLyrics !== prompterPrevShowLyrics) {
    showLyrics = prompterPrevShowLyrics;
    updateLyricsBtn();
  }
}

promptBtn.onclick = enterPrompterMode;
exitPromptBtn.onclick = exitPrompterMode;

function lyricsPointFromEvent(event) {
  if (event.touches && event.touches.length) { return event.touches[0].clientY; }
  return event.clientY;
}

lyricsScrollEl.addEventListener('touchstart', function (event) {
  lyricsDragStartY = lyricsPointFromEvent(event);
  lyricsDragStartRatio = lyricsHeightRatio;
}, false);
lyricsScrollEl.addEventListener('mousedown', function (event) {
  lyricsDragStartY = lyricsPointFromEvent(event);
  lyricsDragStartRatio = lyricsHeightRatio;
}, false);

function lyricsDragMove(event) {
  if (lyricsDragStartY === null) { return; }
  var boxHeight = lyricsScrollEl.clientHeight || 1;
  var deltaRatio = (lyricsPointFromEvent(event) - lyricsDragStartY) / boxHeight;
  lyricsHeightRatio = Math.min(1, Math.max(0, lyricsDragStartRatio + deltaRatio));
  localStorage.setItem(LYRICS_HEIGHT_KEY, lyricsHeightRatio);
}
lyricsScrollEl.addEventListener('touchmove', lyricsDragMove, false);
lyricsScrollEl.addEventListener('mousemove', lyricsDragMove, false);

function endLyricsDrag() { lyricsDragStartY = null; }
lyricsScrollEl.addEventListener('touchend', endLyricsDrag, false);
lyricsScrollEl.addEventListener('touchcancel', endLyricsDrag, false);
lyricsScrollEl.addEventListener('mouseup', endLyricsDrag, false);

function renderLyricsScroll(lines, songBeat) {
  if (!showLyrics || !lines.length || typeof songBeat !== 'number') {
    lyricsScrollEl.style.display = 'none';
    return;
  }
  lyricsScrollEl.style.display = 'block';
  var boxHeight = lyricsScrollEl.clientHeight || 1;
  var pixelsPerBeat = boxHeight / (LYRICS_VISIBLE_LINES * LYRICS_BEATS_PER_LINE);
  var centerY = boxHeight * lyricsHeightRatio;
  var bufferPx = LYRICS_BEATS_PER_LINE * pixelsPerBeat;
  var visible = {};
  var index, text, y, el;
  for (index = 0; index < lines.length; index++) {
    text = lines[index];
    if (!text) { continue; }
    y = centerY + (index * LYRICS_BEATS_PER_LINE - songBeat) * pixelsPerBeat;
    if (y < -bufferPx || y > boxHeight + bufferPx) { continue; }
    el = lyricsLineEls[index];
    if (!el) {
      el = document.createElement('div');
      el.className = 'lyricsLineText';
      lyricsScrollEl.appendChild(el);
      lyricsLineEls[index] = el;
    }
    el.innerHTML = text;
    el.style.top = y + 'px';
    el.style.display = 'block';
    visible[index] = true;
  }
  for (index in lyricsLineEls) {
    if (lyricsLineEls.hasOwnProperty(index) && !visible[index]) {
      lyricsLineEls[index].style.display = 'none';
    }
  }
}

function lyricsAnimationLoop() {
  var songBeat = lyricsBaseSongBeat;
  if (songBeat !== null && lyricsBaseBpm) {
    var elapsedS = (performance.now() - lyricsBaseAt) / 1000;
    songBeat += elapsedS * (lyricsBaseBpm / 60);
  }
  renderLyricsScroll(lyricsLinesCache, songBeat);
  if (window.requestAnimationFrame) {
    requestAnimationFrame(lyricsAnimationLoop);
  } else {
    setTimeout(lyricsAnimationLoop, 60);
  }
}
lyricsAnimationLoop();

function render(data) {
  toggleStickyClass('is-offline', !!data.offline);
  if (data.offline) {
    dotEl.style.display = 'none';
    digitEl.style.display = 'none';
    dotsPairEl.style.display = 'none';
    scrollLineEl.style.display = 'none';
    offlineEl.style.display = 'block';
    return;
  }
  offlineEl.style.display = 'none';
  if (data.connected) {
    dotEl.style.display = 'none';
    scrollLineEl.style.display = 'none';
    if (showDots) {
      digitEl.style.display = 'none';
      dotsPairEl.style.display = 'block';
      var leftFilled = data.beat % 2 === 1;
      dotLeftEl.className = leftFilled ? 'circle filled' : 'circle';
      dotRightEl.className = leftFilled ? 'circle' : 'circle filled';
    } else {
      dotsPairEl.style.display = 'none';
      digitEl.style.display = 'block';
      digitEl.innerHTML = String(data.beat);
    }
    scheduleUpcomingClicks(data);
    if (data.beat !== lastBeat) {
      lastBeat = data.beat;
      if (data.beat === 1) { retrigger('flash'); }
      else if (data.beat === 3) { retrigger('flash-blue'); }
      // 2x plus vite que l'ancien "un temps sur deux" : retrigger à CHAQUE
      // temps désormais.
      if (data.bpm) sceneLabelNextEl.style.animationDuration = (30000 / data.bpm) + 'ms';
      retriggerEl(sceneLabelNextEl, 'pulse');
    }
  } else {
    digitEl.style.display = 'none';
    dotsPairEl.style.display = 'none';
    lastBeat = null;
    nextClickAt = null;
    if (data.bpm && !data.running) {
      dotEl.style.display = 'none';
      scrollLineEl.style.display = 'block';
      var barPhase = data.bar_phase || 0;
      if (barPhase < lastBarPhase) {
        scrollThumbEl.style.width = '0%';
      }
      lastBarPhase = barPhase;
      scrollThumbEl.style.width = (barPhase * 100) + '%';
    } else {
      scrollLineEl.style.display = 'none';
      dotEl.style.display = data.bpm ? 'none' : 'block';
    }
  }
  sceneNameEl.innerHTML = data.scene_name ? data.scene_name : '';
  sceneNameEl.className = data.scene_launched ? 'launched' : '';
  barCountEl.innerHTML = data.bar_count ? ('Mes. ' + data.bar_count) : '';
  sceneLabelCurrentEl.innerHTML = data.scene_label ? data.scene_label : '';
  sceneLabelNextEl.innerHTML = data.next_scene_label ? (' ' + data.next_scene_label) : '';
  // classList.toggle (pas une réaffectation de className) : préserve la
  // classe "pulse" qui vient d'être (re)posée juste au-dessus par retriggerEl.
  sceneLabelNextEl.classList.toggle('hasNext', !!data.next_scene_label);
  lyricsLinesCache = (data.lyrics_lines && data.lyrics_lines.length) ? data.lyrics_lines : [];
  if (typeof data.lyrics_song_beat === 'number') {
    lyricsBaseSongBeat = data.lyrics_song_beat;
    lyricsBaseBpm = (data.connected && typeof data.bpm === 'number') ? data.bpm : null;
    lyricsBaseAt = performance.now();
  } else {
    lyricsBaseSongBeat = null;
    lyricsBaseBpm = null;
  }
  var suffix = '';
  if (data.mode === 'link') {
    suffix = data.running ? ' (lecture)' : '';
  } else {
    suffix = data.running ? '' : ' (arret)';
  }
  infoEl.innerHTML = (data.bpm ? data.bpm.toFixed(1) : '--') + ' BPM' + suffix;
}

function poll() {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/state?latency_ms=' + slider.value, true);
  xhr.onreadystatechange = function () {
    if (xhr.readyState !== 4 || xhr.status !== 200) { return; }
    var data;
    try { data = JSON.parse(xhr.responseText); } catch (e) { return; }
    render(data);
  };
  xhr.send(null);
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
        self._scene_label = ""
        self._next_scene_label = ""
        self._offline = False
        self._lyrics_lines: list[str] = []
        self._lyrics_song_beat: float | None = None
        self._highlighted = False
        self._metronome_end_muted = False

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

    def set_scene_label(self, label: str) -> None:
        """Texte de section (INTRO/COUPLET/REFRAIN..., voir scene_sheet.py) :
        "collant", laissé tel quel entre deux mesures étiquetées (voir
        beat_display._apply_scene_sheet_row)."""
        with self._lock:
            self._scene_label = label

    def set_next_scene_label(self, label: str) -> None:
        """Prochain LABEL non vide à venir (voir scene_sheet.SceneSheet.label_after) :
        affiché en rouge clignotant à côté du label courant, pour l'annoncer
        à l'avance (voir beat_display._apply_scene_sheet_row)."""
        with self._lock:
            self._next_scene_label = label

    def set_offline(self) -> None:
        """Signale la fermeture imminente de CLIC : affiche OFFLINE sur la
        page web à la place des chiffres/de la ligne, avant même que le
        serveur web ne s'arrête réellement (voir BeatDisplayApp.on_close)."""
        with self._lock:
            self._offline = True

    def set_lyrics_lines(self, lines: list[str]) -> None:
        """Texte complet des paroles (lyrics.py) du morceau en cours, poussé
        une seule fois par chargement (voir beat_display._scene_launch) :
        la page web calcule elle-même quelles lignes afficher/faire défiler
        à partir de set_lyrics_position, indépendamment de la case «Afficher
        les paroles» du grand écran."""
        with self._lock:
            self._lyrics_lines = lines

    def set_lyrics_position(self, song_beat: float | None) -> None:
        """Position continue (en temps, depuis le début du morceau) utilisée
        par la page web pour faire défiler les paroles à la même vitesse que
        le grand écran (voir beat_display._push_lyrics_state) ; None = pas de
        position fiable (paroles masquées côté page web)."""
        with self._lock:
            self._lyrics_song_beat = song_beat

    def set_highlighted(self, highlighted: bool) -> None:
        """Mesure HIGHLIGHT (scene_sheet.py) en cours (voir
        beat_display._update_display, HIGHLIGHT_SIZE_SCALE) : la page web
        grossit le chiffre/les points pareil que le grand écran, sauf en mode
        paroles (voir body.lyrics-mode dans _PAGE)."""
        with self._lock:
            self._highlighted = highlighted

    def set_metronome_end_muted(self, muted: bool) -> None:
        """Coupe le clic audio de la page web pendant le label END, comme les
        métronomes M1/M2 (voir beat_display._metronome_end_muted)."""
        with self._lock:
            self._metronome_end_muted = muted

    def compute(self, latency_ms: float = 0.0) -> dict:
        """Calcule {beat, bpm, connected, running, mode} pour un décalage donné."""
        with self._lock:
            data = dict(self._data)
            scene_name = self._scene_name
            scene_launched = self._scene_launched
            bar_count = self._bar_count
            scene_label = self._scene_label
            next_scene_label = self._next_scene_label
            offline = self._offline
            lyrics_lines = self._lyrics_lines
            lyrics_song_beat = self._lyrics_song_beat
            highlighted = self._highlighted
            metronome_end_muted = self._metronome_end_muted
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
            "scene_label": scene_label,
            "next_scene_label": next_scene_label,
            "offline": offline,
            "lyrics_lines": lyrics_lines,
            "lyrics_song_beat": lyrics_song_beat,
            "highlighted": highlighted,
            "metronome_end_muted": metronome_end_muted,
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
                file_name = parsed.path[len("/sounds/"):]
                file_path = _SOUNDS_DIR / file_name
                if not file_path.is_file():
                    # Pas de click.wav/click_up.wav à la racine de sounds/ :
                    # les kits (voir audio_metronome.py) vivent dans des
                    # sous-dossiers (sounds/Kit1/click.wav...), on replie sur
                    # le kit par défaut plutôt que 404 (la page web n'a pas
                    # connaissance du kit choisi pour le métronome audio).
                    file_path = _SOUNDS_DIR / "Kit1" / file_name
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
            elif parsed.path in ("/legacy", "/legacy/"):
                # Page ES5 pour Safari trop ancien (iOS 9.3.5, iPad mini
                # 1/iPad 2) : voir _LEGACY_PAGE.
                body = _LEGACY_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # Sans ceci, un téléphone garde parfois en cache une ancienne
                # version de la page (HTML+JS embarqué) même après un
                # changement côté serveur : on force un rechargement à chaque
                # visite (comme /state, jamais de cache pour du contenu qui
                # change avec le code).
                self.send_header("Cache-Control", "no-store")
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
