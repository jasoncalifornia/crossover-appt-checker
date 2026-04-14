#!/bin/bash
# Run with visible browser + force re-login (use this for first run / debugging)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source venv/bin/activate
python check_appointments.py --debug --login
