"""Client OSC minimal pour piloter Ableton Live via le remote script AbletonOSC
(voir README.md pour l'installation côté Live). Aucune dépendance externe :
un message OSC (adresse + arguments float) tient en quelques lignes.
"""
from __future__ import annotations

import queue
import socket
import struct
import threading

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11000  # port d'écoute par défaut d'AbletonOSC
DEFAULT_RESPONSE_PORT = 11001  # port sur lequel AbletonOSC envoie ses réponses


def _osc_string(value: str) -> bytes:
    data = value.encode("utf-8") + b"\x00"
    padding = (4 - len(data) % 4) % 4
    return data + b"\x00" * padding


def _osc_read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    value = data[offset:end].decode("utf-8")
    total_len = end - offset + 1  # inclut le \0 terminal
    padded_len = (total_len + 3) // 4 * 4
    return value, offset + padded_len


def _osc_message(address: str, *args: float | str | bool) -> bytes:
    type_tags = ","
    for arg in args:
        if isinstance(arg, bool):
            type_tags += "T" if arg else "F"
        elif isinstance(arg, str):
            type_tags += "s"
        else:
            type_tags += "f"
    message = _osc_string(address) + _osc_string(type_tags)
    for value in args:
        if isinstance(value, bool):
            continue  # les tags T/F du protocole OSC ne portent aucun octet de donnée
        message += _osc_string(value) if isinstance(value, str) else struct.pack(">f", value)
    return message


def _osc_parse(data: bytes) -> tuple[str, list]:
    """Décode un message OSC reçu en (adresse, liste d'arguments)."""
    address, offset = _osc_read_string(data, 0)
    type_tags, offset = _osc_read_string(data, offset)
    if not type_tags.startswith(","):
        raise ValueError("message OSC sans type tags")
    args: list = []
    for tag in type_tags[1:]:
        if tag == "i":
            args.append(struct.unpack_from(">i", data, offset)[0])
            offset += 4
        elif tag == "f":
            args.append(struct.unpack_from(">f", data, offset)[0])
            offset += 4
        elif tag == "s":
            value, offset = _osc_read_string(data, offset)
            args.append(value)
        elif tag in ("T", "F"):
            args.append(tag == "T")
        else:
            raise ValueError(f"type OSC non géré : {tag}")
    return address, args


class LiveOSC:
    """Envoie des commandes OSC à AbletonOSC, et reçoit ses réponses en tâche
    de fond (nécessaire pour les requêtes get/, ex. nom d'une scène)."""

    def __init__(
        self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, response_port: int = DEFAULT_RESPONSE_PORT,
    ):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._replies: "queue.Queue[tuple[str, list]]" = queue.Queue()
        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv_sock.bind(("0.0.0.0", response_port))
        self._recv_sock.settimeout(0.5)
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._recv_sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._replies.put(_osc_parse(data))
            except ValueError:
                continue

    def poll_replies(self) -> list[tuple[str, list]]:
        """Vide et retourne les réponses OSC reçues depuis le dernier appel."""
        replies = []
        try:
            while True:
                replies.append(self._replies.get_nowait())
        except queue.Empty:
            pass
        return replies

    def send(self, address: str, *args: float | str | bool) -> None:
        self._sock.sendto(_osc_message(address, *args), self._addr)

    def jump_song_by(self, beats: float) -> None:
        """Décale la position de lecture de Live (donc tous les clips en
        cours, sur toutes les pistes) de `beats` temps, en avant (positif)
        ou en arrière (négatif) — correspond à Song.jump_by() du LOM.
        Déplace aussi le compteur général de Live (horloge unique du Set)."""
        self.send("/live/song/jump_by", float(beats))

    def jump_tracks_by(self, beats: float) -> None:
        """Décale de `beats` temps le clip en cours de lecture de chaque piste
        (Track.jump_in_running_session_clip), sans toucher au compteur
        général de Live, contrairement à jump_song_by. Nécessite le handler
        ajouté dans track.py d'AbletonOSC (absent du script d'origine)."""
        self.send("/live/track/jump_in_running_session_clip", "*", float(beats))

    def get_num_scenes(self) -> None:
        self.send("/live/song/get/num_scenes")

    def get_selected_scene(self) -> None:
        self.send("/live/view/get/selected_scene")

    def get_scene_name(self, index: int) -> None:
        self.send("/live/scene/get/name", index)

    def set_selected_scene(self, index: int) -> None:
        self.send("/live/view/set/selected_scene", index)

    def fire_scene(self, index: int) -> None:
        self.send("/live/scene/fire", index)

    def set_track_volume(self, track_index: int, value: float) -> None:
        """Position du volume d'une piste (0.0 à 1.0, cf. Track.volume du LOM)."""
        self.send("/live/track/set/volume", track_index, value)

    def set_track_mute(self, track_index: int, muted: bool) -> None:
        """Coupe (True) ou réactive (False) le son d'une piste (Track.mute du LOM)."""
        self.send("/live/track/set/mute", track_index, bool(muted))

    def start_playing(self) -> None:
        """Démarre la lecture (Song.start_playing) — utilisé après le
        lancement d'une scène "tempo seul" (nom purement numérique), qui ne
        contient pas de clip et ne démarre donc pas le transport toute seule."""
        self.send("/live/song/start_playing")

    def stop_playing(self) -> None:
        """Arrête la lecture (Song.stop_playing). Appelé deux fois de suite,
        ça reproduit le double-Stop natif de Live qui ramène le curseur au
        début (1:1:1)."""
        self.send("/live/song/stop_playing")

    def close(self) -> None:
        self._stop.set()
        self._recv_sock.close()
        self._sock.close()

