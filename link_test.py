"""Script de test rapide pour Ableton Link : à lancer depuis un vrai Terminal
(pas depuis l'assistant) afin que macOS puisse afficher/traiter normalement
une éventuelle alerte de pare-feu pour les connexions réseau entrantes.

Utilisation :
    source .venv/bin/activate
    python3 link_test.py
"""
import time

from link_client import AbletonLink

link = AbletonLink(120.0)
link.enable(True)
print("Link activé. Recherche de pairs sur le réseau local pendant 15 secondes...")
try:
    for _ in range(30):
        snap = link.snapshot(quantum=4.0)
        print(
            f"pairs={link.num_peers}  bpm={snap['bpm']:.2f}  "
            f"phase={snap['phase']:.3f}  lecture={snap['is_playing']}"
        )
        time.sleep(0.5)
finally:
    link.close()
