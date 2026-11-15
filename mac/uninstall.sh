#!/bin/bash
LABEL="com.ammofinder.monitor"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Monitor stopped and removed from startup."
echo "Your settings, price history and log are kept in:"
echo "  $HOME/Library/Application Support/AmmoFinder"
echo "Delete that folder too if you want everything gone."
