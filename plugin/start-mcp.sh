#!/bin/bash
export EACN3_NETWORK_URL="${EACN3_NETWORK_URL:-http://127.0.0.1:8000}"
exec node /home/user/eacn-dev/plugin/dist/server.js
