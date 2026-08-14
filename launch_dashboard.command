#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Setting up the Morningstar MQR dashboard for the first time..."
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium

echo "Opening Morningstar MQR Research Dashboard..."
python -m streamlit run dashboard.py
