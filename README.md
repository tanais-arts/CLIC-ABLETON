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
6. **Régler le tempo** : saisir la valeur voulue dans le champ "Régler le
   tempo" — elle est envoyée à toute la session Link (Live inclus) à la
   validation (flèches, touche Entrée ou en cliquant ailleurs), pas à chaque
   touche tapée (pour ne pas envoyer un nombre incomplet pendant la saisie).
   Nécessite au moins un pair Link connecté (Live doit avoir Link activé)
   pour que le changement soit pris en compte.
7. **Régler le tempo au fader 16 (Yamaha 01V96V2, façon pitch de platine
   Pioneer DJ MK2)** : le fader physique 16 de la console (2e port HUI) est
   entièrement dédié au tempo et n'apparaît plus dans le mapping piste/fader
   (voir "Configurer le mapping des faders…", qui ne propose plus que les
   tranches 1-15). Sa position centrale (graduation -10 en général) laisse le
   tempo inchangé (tempo "de référence", celui du morceau chargé) ; le monter
   augmente le tempo, le baisser le diminue. La plage couverte par toute la
   course du fader se règle avec le menu déroulant "Plage fader 16" à côté du
   champ de tempo : **± 3 %**, **± 6 %** (par défaut), **± 10 %**, **± 20 %**
   ou **± 100 %** autour du tempo de référence. À chaque lancement de scène
   (feuille de morceau ou tempo seul), le logiciel replace lui-même le fader
   physique à sa position centrale (0% de modification). Entre deux
   lancements, le logiciel réaffirme périodiquement au fader la position
   correspondant au tempo affiché (qu'il vienne de Live, du champ de tempo ou
   du fader lui-même) : les faders HUI motorisés reviennent tout seuls à leur
   dernière position confirmée si on ne le fait pas, sans que ça change le
   tempo. Un geste récent de la main sur le fader garde toujours la priorité
   (pas de renvoi pendant 1,5s après un mouvement).
   Le tempo de référence se remet à jour automatiquement dès qu'un
   changement de tempo ne provenant pas du fader est détecté (ex. nouveau
   morceau/scène, réglage manuel dans Live). Le champ de tempo reflète aussi
   en temps réel les changements faits côté Live (souris ou autre pair Link) :
   la mise à jour est bidirectionnelle.
   Le bouton **Mute** de cette même tranche 16 est un envoi simple (pas un
   bascule) : un appui rappelle le tempo **d'origine du morceau en cours**
   (pas simplement le tempo de référence courant) au **temps suivant**, pour
   ne pas couper le rythme en cours de temps. Ce tempo d'origine est lu dans
   le nom de la scène qui précède la scène du morceau, selon la convention du
   set (ex. le morceau "OVLM" est précédé d'une scène nommée "100" = 100 BPM).
8. **Feuille de scène (fichier `<nom de la scène>.xlsx`, ex. `Viser.xlsx` à
   côté de `beat_display.py`)** : optionnelle — si le fichier n'existe pas
   pour la scène en cours, rien ne change par rapport au comportement normal.
   Quand il existe, il permet de faire varier, mesure par mesure, le
   comptage/l'affichage sans y toucher à la main pendant le morceau. Colonnes
   attendues (première ligne) : `MES` (numéro de mesure depuis le début du
   morceau), `COUNT` (temps par mesure pour cette mesure — remplace
   temporairement "Temps par mesure", qui reflète la valeur en cours), `HIGHLIGHT`
   (`1` = mesure à surligner, `0`/vide = normale) et `LABEL` (texte libre,
   ex. INTRO/COUPLET/REFRAIN). Une mesure `HIGHLIGHT` fait clignoter le fond
   en **blanc** (au lieu du jaune/bleu habituel) sur **tous** les temps de la
   mesure, avec les chiffres/points eux-mêmes qui s'estompent du blanc vers
   leur couleur normale et une taille **50 % plus grande**. Le `LABEL` est
   affiché (desktop et page web) juste au-dessus du compteur de mesures, de
   façon "collante" : une fois affiché, il reste à l'écran tant qu'aucune
   nouvelle valeur non vide n'apparaît plus loin dans la feuille. Le champ
   **GO TO mesure** (à côté de "Temps par mesure") permet de démarrer le
   comptage à une mesure donnée du morceau (ex. reprise en cours de
   répétition) : il ne déplace que le compteur local de CLIC, jamais la
   position de lecture réelle dans Live.

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

