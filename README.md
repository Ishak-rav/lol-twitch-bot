# ⚗️ LoL Twitch Auto-Stream Bot

Stream automatiquement sur Twitch dès qu'un joueur de ta liste est en game sur League of Legends.

## Prérequis

- Windows 10/11
- Python 3.10+
- OBS Studio 28+ (avec plugin obs-websocket intégré)
- League of Legends installé
- Une clé API Riot Games
- Un compte Twitch avec sa stream key

## Installation

### 1. Python
Télécharge et installe Python depuis https://python.org  
⚠️ Coche "Add Python to PATH" pendant l'installation

### 2. Dépendances
Lance `scripts/setup.bat` — il installe tout automatiquement.

### 3. Configuration OBS
1. Ouvre OBS Studio
2. **Outils → obs-websocket Settings**
3. Active le serveur WebSocket
4. Définis un mot de passe
5. Note le port (défaut: 4455)

Configuration stream Twitch dans OBS :
- **Paramètres → Stream**
- Service : Twitch
- Clé de stream : ta clé Twitch

### 4. config.json
Copie `config/config.example.json` → `config/config.json` et remplis :

```json
{
  "riot_api_key": "RGAPI-...",
  "twitch_stream_key": "live_...",
  "twitch_channel_name": "ton_pseudo_twitch",
  "obs_websocket": {
    "host": "localhost",
    "port": 4455,
    "password": "ton_mdp_obs"
  },
  "poll_interval_seconds": 30,
  "region": "euw1",
  "routing": "europe",
  "lol_path": "C:\\Riot Games\\League of Legends\\LeagueClient.exe",
  "players": [
    {
      "name": "PuffGoutJool#SLY",
      "puuid": "ZdsqL41IOdmXIR2-e-lKfYkeUPTr0FuagjuAxF-ZxSqyj1tyoauqcOKIsatWb38geAfXzUcuhAs9bA",
      "display_name": "PuffGoutJool"
    }
  ]
}
```

### 5. Lancer le bot
Double-clique sur `scripts/run_bot.bat`

## Ajouter des joueurs

Dans `config.json`, ajoute des entrées dans `players` :
```json
{
  "name": "Pseudo#TAG",
  "puuid": "...",
  "display_name": "Pseudo affiché sur stream"
}
```

Pour récupérer un PUUID :
```
https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{pseudo}/{tag}
```

## Démarrage automatique avec Windows

1. Appuie sur `Win + R` → tape `shell:startup`
2. Crée un raccourci vers `scripts/run_bot.bat` dans ce dossier
3. Le bot démarre automatiquement au boot

## Logs

Les logs sont dans `logs/bot.log` — utile pour déboguer.

## Architecture

```
Poll Riot API (30s)
  └─ Joueur en game ?
       ├─ OUI → Lance LoL spectateur
       │         Attend 15s
       │         OBS StartStream
       │         Met à jour titre Twitch
       └─ NON → Rien / ou stop stream si était en cours
```
