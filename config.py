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
    "dots_only": False,  # affiche un gros point au lieu du numéro de temps
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
