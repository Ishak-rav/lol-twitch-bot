"""
LoL Twitch Auto-Stream Bot
Surveille une liste de joueurs LoL et lance un stream Twitch automatiquement
quand l'un d'eux est en game.
"""

import json
import time
import logging
import subprocess
import sys
import os
import base64
import urllib3
import requests
import websocket
import psutil

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.json")

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

# ─── Riot API ────────────────────────────────────────────────────────────────
def get_active_game(puuid: str, config: dict) -> dict | None:
    """Retourne les infos de la game en cours pour un joueur, ou None."""
    region = config["region"]
    url = f"https://{region}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    headers = {"X-Riot-Token": config["riot_api_key"]}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            return None  # Pas en game
        elif resp.status_code == 403:
            log.error("Clé API Riot invalide ou expirée !")
            return None
        else:
            log.warning(f"Riot API réponse inattendue: {resp.status_code}")
            return None
    except requests.RequestException as e:
        log.error(f"Erreur Riot API: {e}")
        return None


def get_champion_name(champion_id: int) -> str:
    """Retourne le nom du champion depuis son ID (via DDragon)."""
    try:
        # Récupère la version actuelle de LoL
        versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5).json()
        latest = versions[0]
        champs = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/fr_FR/champion.json",
            timeout=10
        ).json()["data"]
        for name, data in champs.items():
            if int(data["key"]) == champion_id:
                return data["name"]
    except Exception:
        pass
    return f"Champion#{champion_id}"


# ─── OBS WebSocket ───────────────────────────────────────────────────────────
import hashlib
import base64
import json as jsonlib

class OBSController:
    """Contrôle OBS via WebSocket (protocole obs-websocket v5)."""

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.ws = None
        self._msg_id = 1

    def connect(self) -> bool:
        try:
            self.ws = websocket.create_connection(
                f"ws://{self.host}:{self.port}"
            )
            # Handshake Hello
            hello = jsonlib.loads(self.ws.recv())
            if hello.get("op") != 0:
                log.error("OBS WebSocket: Hello inattendu")
                return False

            challenge = hello["d"].get("authentication", {}).get("challenge", "")
            salt = hello["d"].get("authentication", {}).get("salt", "")

            if challenge and salt:
                secret = base64.b64encode(
                    hashlib.sha256((self.password + salt).encode()).digest()
                ).decode()
                auth = base64.b64encode(
                    hashlib.sha256((secret + challenge).encode()).digest()
                ).decode()
            else:
                auth = ""

            # Identify
            identify = {
                "op": 1,
                "d": {
                    "rpcVersion": 1,
                    "authentication": auth,
                    "eventSubscriptions": 0
                }
            }
            self.ws.send(jsonlib.dumps(identify))
            identified = jsonlib.loads(self.ws.recv())
            if identified.get("op") == 2:
                log.info("OBS WebSocket connecté ✅")
                return True
            else:
                log.error(f"OBS WebSocket: identification échouée: {identified}")
                return False
        except Exception as e:
            log.error(f"OBS WebSocket connexion impossible: {e}")
            return False

    def _send_request(self, request_type: str, data: dict = None) -> dict:
        msg_id = str(self._msg_id)
        self._msg_id += 1
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": msg_id,
                "requestData": data or {}
            }
        }
        self.ws.send(jsonlib.dumps(payload))
        resp = jsonlib.loads(self.ws.recv())
        return resp

    def start_stream(self) -> bool:
        try:
            resp = self._send_request("StartStream")
            success = resp.get("d", {}).get("requestStatus", {}).get("result", False)
            if success:
                log.info("OBS: Stream démarré ✅")
            else:
                # Peut-être déjà en stream
                code = resp.get("d", {}).get("requestStatus", {}).get("code")
                log.warning(f"OBS StartStream code: {code}")
            return success
        except Exception as e:
            log.error(f"OBS StartStream erreur: {e}")
            return False

    def stop_stream(self) -> bool:
        try:
            resp = self._send_request("StopStream")
            success = resp.get("d", {}).get("requestStatus", {}).get("result", False)
            if success:
                log.info("OBS: Stream arrêté ✅")
            return success
        except Exception as e:
            log.error(f"OBS StopStream erreur: {e}")
            return False

    def is_streaming(self) -> bool:
        try:
            resp = self._send_request("GetStreamStatus")
            return resp.get("d", {}).get("responseData", {}).get("outputActive", False)
        except Exception:
            return False

    def disconnect(self):
        if self.ws:
            self.ws.close()


# ─── Twitch API ──────────────────────────────────────────────────────────────
def update_twitch_title(title: str, config: dict):
    """Met à jour le titre du stream Twitch."""
    token = config.get("twitch_access_token")
    client_id = config.get("twitch_client_id")
    channel = config.get("twitch_channel_name")

    if not token or not client_id or not channel:
        log.info("Twitch API non configurée — titre non mis à jour")
        return

    # Récupère l'ID du broadcaster
    try:
        resp = requests.get(
            f"https://api.twitch.tv/helix/users?login={channel}",
            headers={"Authorization": f"Bearer {token}", "Client-Id": client_id},
            timeout=10
        )
        broadcaster_id = resp.json()["data"][0]["id"]

        # Met à jour le titre
        requests.patch(
            "https://api.twitch.tv/helix/channels",
            params={"broadcaster_id": broadcaster_id},
            json={"title": title, "game_name": "League of Legends"},
            headers={"Authorization": f"Bearer {token}", "Client-Id": client_id},
            timeout=10
        )
        log.info(f"Twitch titre mis à jour: {title}")
    except Exception as e:
        log.warning(f"Twitch update échoué: {e}")


# ─── LoL Spectateur ──────────────────────────────────────────────────────────
def get_lol_base_dir(config: dict) -> str:
    """Retourne le dossier racine de LoL."""
    return os.path.dirname(config["lol_path"])


def read_lockfile(config: dict) -> dict | None:
    """Lit le fichier lockfile du client LoL pour obtenir port + password."""
    lockfile_path = os.path.join(get_lol_base_dir(config), "lockfile")
    if not os.path.exists(lockfile_path):
        return None
    with open(lockfile_path, "r") as f:
        parts = f.read().strip().split(":")
    # Format: name:pid:port:password:protocol
    return {"port": parts[2], "password": parts[3]}


def is_lol_client_running() -> bool:
    """Vérifie si le client LoL est en cours d'exécution."""
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] in ("LeagueClient.exe", "LeagueClientUx.exe"):
            return True
    return False


def dump_lcu_spectate_endpoints(port: str, headers: dict, config: dict):
    """Fetch le swagger LCU et sauvegarde les endpoints spectate."""
    try:
        resp = requests.get(
            f"https://127.0.0.1:{port}/swagger/v3/openapi.json",
            headers=headers, verify=False, timeout=15
        )
        swagger = resp.json()
        paths = swagger.get("paths", {})
        spectate_paths = {k: v for k, v in paths.items() if "spectate" in k.lower()}
        out_path = os.path.join(os.path.dirname(__file__), "..", "logs", "lcu_spectate_endpoints.json")
        with open(out_path, "w") as f:
            json.dump(spectate_paths, f, indent=2)
        log.info(f"Endpoints spectate sauvegardés dans logs/lcu_spectate_endpoints.json")
        log.info(f"Endpoints trouvés: {list(spectate_paths.keys())}")
    except Exception as e:
        log.warning(f"dump_lcu_spectate_endpoints erreur: {e}")


def debug_lcu_friends(port: str, headers: dict):
    """Debug: affiche les amis en game et leur présence."""
    try:
        resp = requests.get(
            f"https://127.0.0.1:{port}/lol-chat/v1/friends",
            headers=headers, verify=False, timeout=10
        )
        friends = resp.json()
        in_game = [f for f in friends if f.get("availability") in ("ingame", "inGame")]
        log.info(f"Amis en game: {[f.get('gameName', f.get('name', '?')) for f in in_game]}")
        for f in in_game:
            log.info(f"  → {f.get('gameName')}#{f.get('gameTag')} | presence: {f.get('lol', {})}")
    except Exception as e:
        log.warning(f"debug_lcu_friends erreur: {e}")


def ensure_lol_client_running(config: dict) -> bool:
    """Lance le client LoL si nécessaire et attend qu'il soit prêt."""
    if is_lol_client_running():
        log.info("Client LoL déjà lancé ✅")
        return True

    log.info("Lancement du client LoL...")
    subprocess.Popen([config["lol_path"]])

    # Attend jusqu'à 120s que le lockfile apparaisse
    for _ in range(24):
        time.sleep(5)
        if read_lockfile(config):
            log.info("Client LoL prêt ✅")
            return True

    log.error("Timeout: le client LoL n'a pas démarré")
    return False


def launch_spectator_bat(game: dict, config: dict):
    """Lance le spectateur via un .bat temporaire + os.startfile (ShellExecute)."""
    try:
        import tempfile
        game_id        = game["gameId"]
        encryption_key = game["observers"]["encryptionKey"]
        base           = os.path.dirname(config["lol_path"])
        lol_game_path  = os.path.join(base, "Game", "League of Legends.exe")
        cwd            = os.path.join(base, "Game")
        spectate_server = "spectator.euw1.lol.pvp.net:80"

        bat_path = os.path.join(tempfile.gettempdir(), "lol_spectate.bat")
        with open(bat_path, "w") as f:
            f.write(f'@echo off\n')
            f.write(f'cd /d "{cwd}"\n')
            f.write(f'"League of Legends.exe" spectator {spectate_server} "{encryption_key}" {game_id} EUW1\n')

        log.info(f"Lancement spectateur via .bat: {bat_path}")
        log.info(f"Commande: League of Legends.exe spectator {spectate_server} [KEY] {game_id} EUW1")
        os.startfile(bat_path)
        return True
    except Exception as e:
        log.error(f"Lancement .bat échoué: {e}")
        return False


def launch_spectator(game: dict, config: dict, active_player: dict):
    """Lance le spectateur via l'API LCU du client LoL."""
    try:
        # S'assure que le client est lancé
        if not ensure_lol_client_running(config):
            return

        # Attend un peu que le lockfile soit stable
        time.sleep(3)
        creds = read_lockfile(config)
        if not creds:
            log.error("Lockfile introuvable — client LoL non prêt")
            return

        port = creds["port"]
        password = creds["password"]
        auth = base64.b64encode(f"riot:{password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        riot_id        = active_player["name"]
        encryption_key = game["observers"]["encryptionKey"]
        game_id        = game["gameId"]

        base_url = f"https://127.0.0.1:{port}"

        display_name = active_player["display_name"]  # "KC Retlaw"

        # v1 attend targetSummonerName + les infos de spectate
        payloads_to_try = [
            {
                "targetSummonerName": display_name,
                "serverAddress": "spectator.euw1.lol.pvp.net:80",
                "encryptionKey": encryption_key,
                "gameId": game_id,
            },
            {
                "targetSummonerName": display_name,
                "serverAddress": "spectator.euw1.lol.pvp.net",
                "serverPort": 80,
                "encryptionKey": encryption_key,
                "gameId": game_id,
            },
            {
                "targetSummonerName": riot_id,
                "serverAddress": "spectator.euw1.lol.pvp.net:80",
                "encryptionKey": encryption_key,
                "gameId": game_id,
            },
        ]

        # Tente le lancement via .bat (ShellExecute) — plus fiable que LCU
        if launch_spectator_bat(game, config):
            return

        # Fallback: debug LCU
        debug_lcu_friends(port, headers)
        dump_lcu_spectate_endpoints(port, headers, config)

        log.info(f"Lancement spectateur via LCU v1: {riot_id} (game {game_id})")
        success = False
        endpoint = f"{base_url}/lol-gameflow/v1/spectate/launch"

        # Le v1 attend peut-être juste le nom en string brut comme body
        name_variants = [display_name, riot_id]
        for name in name_variants:
            log.info(f"Essai string body: '{name}'")
            resp = requests.post(
                endpoint,
                data=json.dumps(name),   # body = "KC Retlaw" (string JSON)
                headers=headers,
                verify=False,
                timeout=15
            )
            if resp.status_code in (200, 204):
                log.info(f"✅ Succès avec body='{name}'")
                success = True
                break
            log.warning(f"→ '{name}' → {resp.status_code}: {resp.text[:200]}")

        # Fallback : essai avec objet JSON classique
        if not success:
            for pl in payloads_to_try:
                resp = requests.post(endpoint, json=pl, headers=headers, verify=False, timeout=15)
                if resp.status_code in (200, 204):
                    log.info(f"✅ Succès! payload keys={list(pl.keys())}")
                    success = True
                    break
                log.warning(f"→ payload={list(pl.keys())} → {resp.status_code}: {resp.text[:200]}")

        if success:
            log.info("Spectateur lancé via LCU ✅")
        else:
            log.error("Tous les endpoints LCU ont échoué")

    except Exception as e:
        log.error(f"Lancement spectateur échoué: {e}")


# ─── Bot principal ───────────────────────────────────────────────────────────
def build_stream_title(player: dict, game: dict) -> str:
    """Construit le titre du stream à partir des infos de game."""
    display_name = player["display_name"]
    queue_map = {
        420: "Ranked Solo/Duo",
        440: "Ranked Flex",
        450: "ARAM",
        400: "Normal Draft",
        430: "Normal Blind",
    }
    queue = queue_map.get(game.get("gameQueueConfigId", 0), "Custom")

    # Trouve le champion du joueur
    champion_name = "?"
    for participant in game.get("participants", []):
        if participant.get("puuid") == player["puuid"]:
            champion_name = get_champion_name(participant.get("championId", 0))
            break

    return f"🔴 {display_name} joue {champion_name} — {queue} | Auto-Spectate"


def run():
    log.info("═══════════════════════════════════")
    log.info("  LoL Twitch Bot démarré ⚗️")
    log.info("═══════════════════════════════════")

    config = load_config()
    obs = OBSController(
        host=config["obs_websocket"]["host"],
        port=config["obs_websocket"]["port"],
        password=config["obs_websocket"]["password"],
    )

    current_game_id = None  # ID de la game actuellement streamée
    obs_connected = False

    while True:
        try:
            # Connexion OBS si pas connecté
            if not obs_connected:
                obs_connected = obs.connect()
                if not obs_connected:
                    log.warning("OBS non disponible, retry dans 30s...")
                    time.sleep(30)
                    continue

            # Recharge la config à chaque cycle (permet de modifier à chaud)
            config = load_config()
            players = config.get("players", [])

            game_found = None
            active_player = None

            # Vérifie chaque joueur
            for player in players:
                game = get_active_game(player["puuid"], config)
                if game:
                    game_found = game
                    active_player = player
                    log.info(f"🎮 {player['display_name']} est en game! (ID: {game['gameId']})")
                    break  # On prend le premier joueur en game

            if game_found and active_player:
                game_id = game_found["gameId"]

                if current_game_id != game_id:
                    # Nouvelle game détectée
                    log.info(f"Nouvelle game détectée: {game_id}")
                    current_game_id = game_id

                    # Titre Twitch
                    title = build_stream_title(active_player, game_found)
                    update_twitch_title(title, config)

                    # Lance le spectateur LoL
                    launch_spectator(game_found, config, active_player)

                    # Attends 15s que LoL se charge avant de démarrer OBS
                    log.info("Attente chargement LoL (15s)...")
                    time.sleep(15)

                    # Démarre le stream OBS
                    if not obs.is_streaming():
                        obs.start_stream()
                else:
                    log.debug(f"Game {game_id} toujours en cours, on continue...")

            else:
                # Personne en game
                if current_game_id is not None:
                    log.info("Game terminée — arrêt du stream")
                    obs.stop_stream()
                    current_game_id = None
                else:
                    log.info("Aucun joueur en game, prochain check dans 30s...")

        except KeyboardInterrupt:
            log.info("Arrêt manuel du bot.")
            if obs_connected and obs.is_streaming():
                obs.stop_stream()
            obs.disconnect()
            break
        except Exception as e:
            log.error(f"Erreur inattendue: {e}", exc_info=True)
            obs_connected = False

        time.sleep(config.get("poll_interval_seconds", 30))


if __name__ == "__main__":
    run()
