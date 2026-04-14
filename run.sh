#!/bin/bash
# Headless run (used by launchd scheduler)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source venv/bin/activate
python check_appointments.py
