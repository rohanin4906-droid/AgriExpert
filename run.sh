#!/bin/bash
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt
if [ ! -f .env ]; then
  printf "Paste your Gemini API key and press Enter: "
  read KEY
  KEY=$(echo "$KEY" | tr -d ' "')
  printf "GEMINI_API_KEY=%s\nGEMINI_MODEL=gemini-2.5-flash\nDATA_GOV_KEY=\n" "$KEY" > .env
fi
(sleep 4; open http://127.0.0.1:5050) &
python3 server.py
