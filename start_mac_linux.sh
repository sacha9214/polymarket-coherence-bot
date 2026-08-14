#!/usr/bin/env bash
# Lance le Coherence Bot (crée l'environnement au premier démarrage).
cd "$(dirname "$0")" || exit 1
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
exec ./venv/bin/python bot.py
