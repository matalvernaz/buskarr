#!/bin/bash
# Run the acquisition worker and the web UI in one container: they share the SQLite file and
# splitting them would mean coordinating two deployments for no benefit at this size.
set -u
mkdir -p /state/staging "$HOME/.tiddl"

# tiddl keeps its auth in $HOME/.tiddl/auth.json and REWRITES it when the token refreshes.
# HOME therefore has to live on the persistent volume, or every restart loses the session — the
# same trap that silently downgraded YouTube fetches to unauthenticated for hours.
if [ -f "${TIDAL_AUTH_JSON:-}" ] && [ ! -f "$HOME/.tiddl/auth.json" ]; then
    cp "$TIDAL_AUTH_JSON" "$HOME/.tiddl/auth.json"
    chmod 600 "$HOME/.tiddl/auth.json"
    echo "seeded tiddl auth from $TIDAL_AUTH_JSON"
fi
# Both children run under this shell as PID 1. The previous form ended in `exec uvicorn`, which
# replaced the shell and discarded the trap with it: on container stop the worker was never sent
# SIGTERM and died only when teardown killed the process group — possibly mid-move or mid-write.
# `wait -n` also means either child exiting takes the container down, so a worker that dies on
# startup surfaces as a restart instead of a healthy web UI that silently acquires nothing.
python -m buskarr.worker &
WORKER=$!
uvicorn buskarr.web:app --host 0.0.0.0 --port 8000 --no-access-log &
WEB=$!

shutdown() {
    kill -TERM "$WORKER" "$WEB" 2>/dev/null
    wait "$WORKER" "$WEB" 2>/dev/null
    exit 0
}
trap shutdown TERM INT

wait -n
echo "a child process exited; shutting down so the container restarts"
shutdown
