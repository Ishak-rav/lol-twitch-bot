@echo off
echo ========================================
echo   LoL Twitch Bot - Installation
echo ========================================

:: Vérifie Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installé !
    echo Télécharge Python sur https://python.org
    pause
    exit /b 1
)

echo [OK] Python trouvé

:: Installe les dépendances
echo Installation des dépendances...
pip install requests websocket-client

echo.
echo [OK] Dépendances installées

:: Vérifie la config
if not exist "..\config\config.json" (
    echo.
    echo [ATTENTION] config.json non trouvé !
    echo Copie config\config.example.json vers config\config.json
    echo et remplis tes informations.
    copy "..\config\config.example.json" "..\config\config.json"
    echo Fichier copié - pense à le remplir !
)

echo.
echo ========================================
echo   Installation terminée !
echo   Lance le bot avec : run_bot.bat
echo ========================================
pause
