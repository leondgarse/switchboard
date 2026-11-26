#!/bin/bash
# Copy room.py (and server.py to the server host) to every host in cc-menubar's config and re-install hooks.
#   ./deploy.sh            # all hosts
# Server host: SERVER_HOST (default MS). The server restarts on the next cron tick (or now, with --restart).
set -e
cd "$(dirname "$0")"
SERVER_HOST=${SERVER_HOST:-MS}
python3 - <<'PY' > /tmp/switchboard-hosts.$$
import json, os
for h in json.load(open(os.path.expanduser("~/.config/cc-menubar/config.json")))["hosts"]:
    print(h["name"], h.get("ssh") or "-")
PY
while read -r name dest; do
  inst='mkdir -p ~/.local/share/switchboard ~/.local/bin && chmod +x ~/.local/share/switchboard/room.py && ln -sf ~/.local/share/switchboard/room.py ~/.local/bin/room && ~/.local/bin/room install-hooks'
  if [ "$dest" = "-" ]; then
    mkdir -p ~/.local/share/switchboard && cp room.py ~/.local/share/switchboard/ && sh -c "$inst"
  else
    ssh -o BatchMode=yes "$dest" 'mkdir -p ~/.local/share/switchboard' </dev/null
    scp -q room.py "$dest":.local/share/switchboard/ </dev/null
    [ "$name" = "$SERVER_HOST" ] && scp -q server.py ensure-server.sh "$dest":.local/share/switchboard/ </dev/null
    ssh -o BatchMode=yes "$dest" "$inst" </dev/null
    if [ "$name" = "$SERVER_HOST" ] && [ "$1" = "--restart" ]; then
      ssh "$dest" 'pkill -f "share/switchboard/server.py"; sleep 1; ~/.local/share/switchboard/ensure-server.sh' </dev/null
    fi
  fi
  echo "deployed: $name"
done < /tmp/switchboard-hosts.$$
rm -f /tmp/switchboard-hosts.$$
