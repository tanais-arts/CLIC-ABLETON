"""Persistance simple de la configuration de l'appli (fichier JSON local).

Objectif : le batteur ne doit rien régler. Une fois le mode et le décalage
de latence choisis (par la personne qui gère Ableton Live), ils sont
mémorisés et rechargés automatiquement au prochain lancement.
"""
from __future__ import annotations

import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "mode": "link",  # "link" ou "midi"
    "midi_port": "",
    "beats_per_bar": 4,
    "latency_ms": 0,
    "web_port": 8765,
    # YAMAHA 01V96 (Note On canal 1) : G-2 pour -1, Sol#-2 pour +1.
    "controller_map_minus": ["note", 0, 7],
    "controller_map_plus": ["note", 0, 8],
    # F#-2 pour la scène suivante (DOWN), F-2 pour la scène précédente (UP).
    "controller_map_scene_prev": ["note", 0, 5],
    "controller_map_scene_next": ["note", 0, 6],
    # C#-2 pour Stop, D-2 pour Lancer la scène (Play).
    "controller_map_stop": ["note", 0, 1],
    "controller_map_play": ["note", 0, 2],
    # D#-2 pour activer/désactiver notre métronome audio local (voir
    # audio_metronome.py) — ne commande plus le métronome interne de Live.
    "controller_map_metronome": ["note", 0, 3],
    "controller_map_metronome_2": ["note", 0, 4],
    # Carte son et nombre de canaux pour le métronome audio local.
    # "" = périphérique de sortie par défaut du système. channels : 2 = paire
    # stéréo, 1 = mono (toujours les premiers canaux du périphérique).
    "metronome_audio_device": "",
    "metronome_audio_channels": 2,
    # Dossier de sons de clic (sounds/<kit>/click.wav + click_up.wav), voir
    # audio_metronome.list_kits().
    "metronome_kit": "Kit1",
    # Compensation de latence du clic audio local (ms, +/-) : positif =
    # déclenche le clic plus tôt (compense la latence de la carte son/de
    # l'ampli), distinct de "latency_ms" qui ne concerne que l'affichage.
    "metronome_audio_latency_ms": 70,
    # Deuxième sortie métronome (2e carte son + kit + latence), jouée en
    # parallèle de la première quand activée (2 musiciens, clics différents).
    "metronome_audio_device_2": "",
    "metronome_audio_channels_2": 2,
    "metronome_kit_2": "Kit1",
    "metronome_audio_latency_ms_2": 0,
    # Mapping piste Live (index) -> tranche HUI/Yamaha (valeur), 0-14 (la
    # tranche 15/canal 16 est réservée au contrôle du tempo, voir
    # TEMPO_FADER_ZONE dans beat_display.py, donc absente du mapping).
    # Diagonale par défaut (piste 1 -> tranche 1, etc.), modifiable dans
    # l'interface ("Configurer le mapping des faders…").
    "hui_track_mapping": [min(i, 14) for i in range(16)],
    # Plage du fader 16 dédié au tempo : "3"/"6"/"10"/"20"/"100" (pourcentage
    # autour du tempo de référence).
    "tempo_fader_range": "6",
    # Affichage réduit (points uniquement, sans chiffres).
    "dots_only": False,
    "offline_bpm": 120.0,
    # Réduit de 50% et remonte en haut les chiffres/points pour laisser de
    # la place au défilement des paroles (voir beat_display._update_display).
    "lyrics_enabled": False,
}


def load_config() -> dict:
    config = dict(DEFAULTS)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as handle:
            config.update(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return config


def save_config(config: dict) -> None:
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
    except OSError:
        pass
