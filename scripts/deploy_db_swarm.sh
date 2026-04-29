#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/cristian/agent-messaging-mcp"
STACK="agent-messaging"

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
Docker is installed, but the current user cannot access /var/run/docker.sock.
Add cristian to the docker group or run this script from a shell with Docker access:

  sudo usermod -aG docker cristian
  newgrp docker
  /home/cristian/agent-messaging-mcp/scripts/deploy_db_swarm.sh
MSG
  exit 1
fi

state="$(docker info --format '{{.Swarm.LocalNodeState}}')"
if [[ "$state" != "active" ]]; then
  docker swarm init >/dev/null
fi

docker stack deploy -c "$ROOT/deploy/docker-stack.yml" "$STACK"
docker service ls --filter "label=com.docker.stack.namespace=$STACK"

cat <<'MSG'

Postgres for agent-messaging MCP is deploying on localhost:55432.
DSN:
  postgresql://agentmsg:agentmsg@127.0.0.1:55432/agent_messaging
MSG
