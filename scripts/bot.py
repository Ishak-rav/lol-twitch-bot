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


def dump_lcu_full_diagnostic(port: str, headers: dict):
    """
    Dump diagnostique complet LCU :
    - /Help (spectate)
    - /swagger/v3/openapi.json (spectate paths)
    - /lol-gameflow/v1/session
    - /lol-chat/v1/friends (champs lol complets)
    Sauvegarde tout dans logs/lcu_diagnostic.json
    """
    diag = {}
    base = f"https://127.0.0.1:{port}"
    log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")

    # 1. /Help — toutes les fonctions spectate
    try:
        r = requests.post(f"{base}/Help", json={"format": "Full"}, headers=headers, verify=False, timeout=15)
        data = r.json()
        if isinstance(data, dict):
            # Cherche dans functions, events, types
            all_items = {}
            for section in ("functions", "events", "types"):
                all_items.update(data.get(section, {}))
            spectate_fns = {k: v for k, v in all_items.items() if "spectate" in k.lower()}
        elif isinstance(data, list):
            spectate_fns = [x for x in data if "spectate" in str(x).lower()]
        else:
            spectate_fns = {}
        diag["help_spectate"] = spectate_fns
        log.info(f"[DIAG] /Help spectate keys: {list(spectate_fns.keys()) if isinstance(spectate_fns, dict) else len(spectate_fns)}")
        # Sauvegarde aussi le help complet pour analyse manuelle
        with open(os.path.join(log_dir, "lcu_help_full.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        diag["help_spectate"] = f"ERREUR: {e}"
        log.warning(f"[DIAG] /Help erreur: {e}")

    # 2. Swagger — tous les paths spectate (v3 + v2)
    for swagger_path in ("/swagger/v3/openapi.json", "/swagger/v2/swagger.json"):
        try:
            r = requests.get(f"{base}{swagger_path}", headers=headers, verify=False, timeout=15)
            swagger = r.json()
            paths = swagger.get("paths", {})
            spectate_paths = {k: v for k, v in paths.items() if "spectate" in k.lower()}
            if spectate_paths:
                diag[f"swagger{swagger_path}"] = spectate_paths
                log.info(f"[DIAG] Swagger spectate paths ({swagger_path}): {list(spectate_paths.keys())}")
                break
        except Exception as e:
            log.warning(f"[DIAG] Swagger {swagger_path} erreur: {e}")

    # 3. /lol-gameflow/v1/session
    try:
        r = requests.get(f"{base}/lol-gameflow/v1/session", headers=headers, verify=False, timeout=10)
        diag["gameflow_session"] = r.json() if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:200]}"
        log.info(f"[DIAG] gameflow session phase: {diag['gameflow_session'].get('phase') if isinstance(diag['gameflow_session'], dict) else diag['gameflow_session']}")
    except Exception as e:
        diag["gameflow_session"] = f"ERREUR: {e}"

    # 4. /lol-chat/v1/friends — données complètes
    try:
        r = requests.get(f"{base}/lol-chat/v1/friends", headers=headers, verify=False, timeout=10)
        friends = r.json() if r.status_code == 200 else []
        diag["friends_count"] = len(friends)
        in_game = [f for f in friends if f.get("availability") in ("inGame", "dnd")]
        diag["friends_in_game"] = in_game
        log.info(f"[DIAG] Amis total: {len(friends)} | En game: {len(in_game)}")
        for f in in_game:
            name = f.get("gameName", f.get("name", "?"))
            tag = f.get("gameTag", "")
            log.info(f"  [IN GAME] {name}#{tag} | id={f.get('id')} | lol={json.dumps(f.get('lol', {}))}")
    except Exception as e:
        diag["friends_in_game"] = f"ERREUR: {e}"

    # 5. Sauvegarde complète
    out = os.path.join(log_dir, "lcu_diagnostic.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(diag, f, indent=2, ensure_ascii=False)
    log.info(f"[DIAG] Diagnostic complet sauvegardé: {out}")
    return diag


def dump_lcu_spectate_endpoints(port: str, headers: dict, config: dict):
    """Alias de compatibilité → appelle dump_lcu_full_diagnostic."""
    dump_lcu_full_diagnostic(port, headers)


def debug_lcu_friends(port: str, headers: dict):
    """Debug: affiche tous les amis et leur statut (avec champ lol complet)."""
    try:
        resp = requests.get(
            f"https://127.0.0.1:{port}/lol-chat/v1/friends",
            headers=headers, verify=False, timeout=10
        )
        friends = resp.json()
        log.info(f"Total amis LCU: {len(friends)}")
        in_game = [f for f in friends if f.get("availability") in ("inGame", "dnd", "chat")]
        log.info(f"Amis en game/connectés: {len(in_game)}")
        for f in friends:
            avail = f.get("availability", "?")
            name = f.get("gameName", f.get("name", "?"))
            tag = f.get("gameTag", "")
            lol_field = f.get("lol", {})
            log.info(f"  → {name}#{tag} | avail={avail} | id={f.get('id', '?')} | lol={json.dumps(lol_field)}")
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
            f.write(f'echo Lancement spectateur...\n')
            f.write(f'"League of Legends.exe" spectator {spectate_server} "{encryption_key}" {game_id} EUW1\n')
            f.write(f'echo Terminé avec code: %errorlevel%\n')
            f.write(f'pause\n')

        log.info(f"Lancement spectateur via .bat: {bat_path}")
        log.info(f"Commande: League of Legends.exe spectator {spectate_server} [KEY] {game_id} EUW1")
        os.startfile(bat_path)
        return True
    except Exception as e:
        log.error(f"Lancement .bat échoué: {e}")
        return False


def get_friend_xmpp_id(port: str, headers: dict, display_name: str, puuid: str) -> str | None:
    """Récupère l'id XMPP LCU d'un ami par son gameName ou puuid."""
    try:
        resp = requests.get(
            f"https://127.0.0.1:{port}/lol-chat/v1/friends",
            headers=headers, verify=False, timeout=10
        )
        friends = resp.json()
        for f in friends:
            fname = f.get("gameName", f.get("name", ""))
            fpuuid = f.get("puuid", "")
            if fname.lower() == display_name.lower() or fpuuid == puuid:
                xmpp_id = f.get("id", "")
                log.info(f"Ami trouvé: {fname} | id XMPP={xmpp_id} | availability={f.get('availability')}")
                return xmpp_id
    except Exception as e:
        log.warning(f"get_friend_xmpp_id erreur: {e}")
    return None


def launch_spectator(game: dict, config: dict, active_player: dict):
    """Lance le spectateur via l'API LCU du client LoL."""
    try:
        # S'assure que le client est lancé
        if not ensure_lol_client_running(config):
            return False

        # Attend un peu que le lockfile soit stable
        time.sleep(3)
        creds = read_lockfile(config)
        if not creds:
            log.error("Lockfile introuvable — client LoL non prêt")
            return False

        port = creds["port"]
        password = creds["password"]
        auth = base64.b64encode(f"riot:{password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        riot_id        = active_player["name"]          # "KC Retlaw#EUW"
        puuid          = active_player["puuid"]
        encryption_key = game["observers"]["encryptionKey"]
        game_id        = game["gameId"]
        display_name   = active_player["display_name"]  # "KC Retlaw"
        base_url       = f"https://127.0.0.1:{port}"

        # Diagnostic complet au 1er essai
        dump_lcu_full_diagnostic(port, headers)

        # Récupère l'id XMPP de l'ami si disponible
        xmpp_id = get_friend_xmpp_id(port, headers, display_name, puuid)

        log.info(f"Tentative spectate LCU: {riot_id} | gameId={game_id} | xmpp_id={xmpp_id}")
        success = False
        endpoint_v1 = f"{base_url}/lol-gameflow/v1/spectate/launch"

        # ── Séquence 1 : v1 avec string body (nom, riot id, xmpp id, puuid) ──
        name_variants = [display_name, riot_id]
        if xmpp_id:
            name_variants.insert(0, xmpp_id)
        # Essai aussi avec le puuid
        name_variants.append(puuid)

        for name in name_variants:
            log.info(f"  [v1 string] body='{name}'")
            resp = requests.post(
                endpoint_v1,
                data=json.dumps(name),
                headers=headers,
                verify=False,
                timeout=15
            )
            log.info(f"  → HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (200, 204):
                log.info(f"✅ Spectateur lancé (v1 string body='{name}')")
                return True

        # ── Séquence 2 : v2 avec objet JSON complet ──
        endpoint_v2 = f"{base_url}/lol-gameflow/v2/spectate/launch"
        spectate_server = "spectator.euw1.lol.pvp.net"

        v2_payloads = [
            # Tentative avec tous les champs connus
            {
                "dropInSpectateGameId": str(game_id),
                "gameQueueType": game.get("gameQueueConfigId", "RANKED_SOLO_5x5"),
                "allowObserveMode": "ALL",
                "puuid": puuid,
                "encryptionKey": encryption_key,
                "gameId": game_id,
                "serverAddress": f"{spectate_server}:80",
            },
            {
                "dropInSpectateGameId": str(game_id),
                "allowObserveMode": "ALL",
                "puuid": puuid,
            },
            {
                "summonerName": display_name,
                "encryptionKey": encryption_key,
                "gameId": game_id,
                "serverAddress": spectate_server,
                "serverPort": 80,
                "platformId": "EUW1",
            },
            {
                "spectatorServerAddress": spectate_server,
                "spectatorServerPort": 80,
                "spectatorEncryptionKey": encryption_key,
                "gameId": str(game_id),
                "summonerName": display_name,
            },
        ]

        for pl in v2_payloads:
            log.info(f"  [v2 JSON] keys={list(pl.keys())}")
            resp = requests.post(endpoint_v2, json=pl, headers=headers, verify=False, timeout=15)
            log.info(f"  → HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (200, 204):
                log.info(f"✅ Spectateur lancé (v2 JSON keys={list(pl.keys())})")
                return True

        # ── Séquence 3 : endpoint lol-spectator alternatif ──
        for alt_ep in [
            f"{base_url}/lol-spectator/v1/spectate",
            f"{base_url}/lol-spectator/v2/spectate/launch",
        ]:
            body = {
                "puuid": puuid,
                "gameId": game_id,
                "encryptionKey": encryption_key,
                "serverAddress": spectate_server,
                "serverPort": 80,
            }
            log.info(f"  [alt] endpoint={alt_ep.split('/')[-3]}/{alt_ep.split('/')[-2]}/{alt_ep.split('/')[-1]}")
            try:
                resp = requests.post(alt_ep, json=body, headers=headers, verify=False, timeout=10)
                log.info(f"  → HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code in (200, 204):
                    log.info(f"✅ Spectateur lancé (endpoint alternatif)")
                    return True
            except Exception:
                pass

        log.error("❌ Toutes les tentatives LCU spectate ont échoué.")
        log.error("→ Vérifier logs/lcu_diagnostic.json pour analyser l'état LCU.")
        log.error("→ Solution garantie : être ami avec le joueur sur IzakSpectate.")
        return False

    except Exception as e:
        log.error(f"Lancement spectateur échoué: {e}")
        return False


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

    current_game_id   = None   # ID de la game actuellement streamée
    spectator_launched = False  # True si le spectateur a été lancé avec succès
    obs_connected      = False

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
                    break

            if game_found and active_player:
                game_id = game_found["gameId"]

                if current_game_id != game_id:
                    # Nouvelle game détectée
                    log.info(f"Nouvelle game détectée: {game_id}")
                    current_game_id    = game_id
                    spectator_launched = False

                    # Titre Twitch
                    title = build_stream_title(active_player, game_found)
                    update_twitch_title(title, config)

                    # Démarre le stream OBS immédiatement
                    if not obs.is_streaming():
                        obs.start_stream()

                # Retente le spectateur si pas encore lancé
                if not spectator_launched:
                    log.info("Tentative lancement spectateur...")
                    spectator_launched = launch_spectator(game_found, config, active_player)
                    if spectator_launched:
                        log.info("✅ Spectateur lancé, attente chargement (15s)...")
                        time.sleep(15)
                    else:
                        log.warning("Spectateur non lancé, retry au prochain poll...")

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
