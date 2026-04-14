#!/bin/bash
# Disable the appointment checker (run this after you book your appointment)
PLIST_DST="$HOME/Library/LaunchAgents/com.jason.crossover-appt-checker.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST"
    rm "$PLIST_DST"
    echo "Appointment checker disabled"
else
    echo "Appointment checker is not currently running"
fi
