#!/bin/sh
# Run by cron every 5 minutes and at boot on the server host: start the room server if it isn't running.
# The full path in the command line is what pgrep and deploy.sh --restart look for.
DIR="$HOME/.local/share/switchboard"
pgrep -f "$DIR/server.py" >/dev/null && exit 0
(cd "$DIR" && exec nohup /usr/bin/python3 "$DIR/server.py" --port 8766 >> server.log 2>&1) >/dev/null 2>&1 </dev/null &
