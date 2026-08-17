# Compteur de temps Ableton Live (pour batteur)

Petite appli (Python/Tkinter) qui affiche en grand le temps courant de la mesure
(1, 2, 3, 4...). Le temps 1 s'affiche en rouge pour repérer facilement le
premier temps. Deux sources de synchronisation sont disponibles :

- **Ableton Link** (recommandé) : aucune configuration MIDI, toujours aligné
  sur le vrai temps 1 de Live même si l'appli se connecte en cours de
  lecture, fonctionne aussi si le batteur rejoint plus tard.
- **MIDI Clock** : à utiliser avec un logiciel qui n'a pas Ableton Link.

L'appli lance aussi une **petite page web locale** pour afficher le même
compteur sur un smartphone (voir plus bas).

## Installation

```bash
cd "/Users/tanaismusic/Documents/CLIC"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Utilisation avec Ableton Link (recommandé)

1. Dans Ableton Live : **Préférences > Link/Tempo/MIDI**, activer le bouton
   **Link** (ou l'icône Link dans la barre de contrôle en haut de Live).
2. Lancer l'appli :
   ```bash
   source .venv/bin/activate
   python3 beat_display.py
   ```
3. Choisir la source **Ableton Link** (sélectionné par défaut). Dès que Link
   est activé côté Live, le nombre de "Pairs Link connectés" passe à 1 et le
   compteur se synchronise automatiquement — rien d'autre à régler.
4. Ajuster **Temps par mesure** si le morceau n'est pas en 4/4.
5. Ajuster **Latence (ms)** une seule fois pour compenser le délai de
   transmission/affichage perçu par le batteur (valeur positive = l'appli
   affiche le temps un peu en avance). Ce réglage est mémorisé automatiquement
   (fichier `config.json`) : le batteur n'a jamais besoin d'y toucher.

### Compilation de la bibliothèque Link (déjà faite dans ce projet)

L'appli utilise la bibliothèque C officielle `abl_link` d'Ableton (dans
`link-src/`, sous-module `extensions/abl_link` du SDK Link), compilée en
`link-lib/libabl_link.dylib`. Si jamais il faut la recompiler :

```bash
cd link-src/build
cmake .. -DLINK_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build . --target abl_link -j4
cd ../..
clang++ -std=c++17 -shared -install_name @rpath/libabl_link.dylib \
  -o link-lib/libabl_link.dylib \
  -Wl,-force_load,link-src/build/libabl_link.a -lc++
```

## Utilisation avec MIDI Clock (logiciel sans Link)

### Configuration côté macOS (port MIDI virtuel)

1. Ouvrir **Audio MIDI Setup** (Applications > Utilitaires).
2. Menu **Fenêtre > Afficher la fenêtre MIDI**.
3. Double-cliquer sur **IAC Driver**, cocher **Le dispositif est activé**,
   vérifier qu'un port existe. Créer un port au besoin avec le **+**.

### Configuration côté logiciel (ex. Ableton Live)

1. **Live > Préférences > Link/Tempo/MIDI**.
2. Dans le tableau **Ports MIDI**, repérer la ligne correspondant au port IAC
   utilisé (colonne Output) et activer la case **Sync** (Sortie).
3. Vérifier que la lecture est lancée : le logiciel enverra alors en continu
   le MIDI Clock (Start/Stop/Continue + horloge 24 ppqn) sur ce port.

### Dans l'appli

1. Choisir la source **MIDI Clock**.
2. Choisir le port IAC dans le menu déroulant puis cliquer sur **Connecter**.
3. Lancer la lecture : le chiffre change à chaque temps.

> Limite connue du MIDI Clock : si l'appli se connecte alors que la lecture a
> déjà commencé (sans recevoir de message Start ou Song Position Pointer), le
> comptage peut être décalé par rapport au vrai temps 1. Ableton Link n'a pas
> cette limite.

Le bouton **Démo 120 BPM** permet de tester l'affichage sans aucune source
externe (simulation interne d'une horloge à 120 BPM).

## Affichage sur smartphone (page web locale)

Au lancement, l'appli démarre automatiquement un petit serveur web local et
affiche son adresse dans la fenêtre (ex. `http://192.168.1.144:8765`). Sur le
téléphone du batteur (connecté au **même réseau Wi-Fi** que l'ordinateur),
ouvrir cette adresse dans un navigateur : le même compteur (grand chiffre +
BPM) s'affiche et se met à jour en continu, sans rien installer.

