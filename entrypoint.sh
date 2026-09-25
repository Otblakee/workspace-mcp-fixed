#!/bin/sh
# Container entrypoint for the OTB Workspace MCP.
#
# Why this exists: Render mounts a persistent disk owned by root, but the
# app runs as the non-root user "app". On a fresh disk the app cannot create
# /data/credentials, the start-up permission check raises, and the service
# crash-loops. So the container now starts as root, hands the mount to
# "app", and drops privileges with gosu before running the app.
#
# Without a disk (no /data directory) nothing is chowned and the app still
# runs as "app", exactly as it did before this script existed.
#
# The command is passed as a single string (see CMD in the Dockerfile) and
# run through /bin/sh -c so that ${TOOL_TIER} and ${TOOLS} expand at run
# time, the same as the previous ENTRYPOINT ["/bin/sh", "-c"] did.
set -eu

DATA_DIR="${WORKSPACE_DATA_DIR:-/data}"
APP_USER="${WORKSPACE_APP_USER:-app}"

if [ "$(id -u)" = "0" ]; then
    if [ -d "$DATA_DIR" ]; then
        # Only touch ownership when it is wrong. The disk holds tokens and
        # short-lived attachments, so a recursive chown stays cheap.
        if [ "$(stat -c %U "$DATA_DIR")" != "$APP_USER" ]; then
            echo "entrypoint: giving $DATA_DIR to $APP_USER"
            chown -R "$APP_USER":"$APP_USER" "$DATA_DIR"
        fi
    fi
    exec gosu "$APP_USER" /bin/sh -c "$*"
fi

# Already non-root (for example a local run as your own user).
exec /bin/sh -c "$*"
