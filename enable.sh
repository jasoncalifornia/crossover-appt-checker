#!/bin/bash
# Enable the every-15-min appointment checker
PLIST_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/com.jason.crossover-appt-checker.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.jason.crossover-appt-checker.plist"

# Make scripts executable
chmod +x "$(dirname "${BASH_SOURCE[0]}")/run.sh"
chmod +x "$(dirname "${BASH_SOURCE[0]}")/debug.sh"

cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"
echo "Appointment checker enabled — running every 15 minutes"
echo "Logs: $(dirname "${BASH_SOURCE[0]}")/check.log"
echo "To disable: ./disable.sh"
