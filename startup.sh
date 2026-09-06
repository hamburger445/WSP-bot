#!/bin/sh
set -eu

REPO_URL="${REPO_URL:-https://github.com/hamburger445/WSP-bot.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
APP_DIR="${APP_DIR:-/home/container/WSP-bot}"
DATA_DIR="${DATA_DIR:-/home/container/data}"
DEPS_DIR="${DEPS_DIR:-/home/container/.local}"

case "$APP_DIR" in
  /home/container|/home/container/)
    echo "APP_DIR must be a subdirectory, not /home/container"
    exit 1
    ;;
esac

echo "Updating bot source from ${REPO_URL} (${REPO_BRANCH})"
mkdir -p "$(dirname "$APP_DIR")" "$DATA_DIR" "$DEPS_DIR"

if [ -d "${APP_DIR}/.git" ]; then
  git -C "$APP_DIR" fetch --prune origin "$REPO_BRANCH"
  git -C "$APP_DIR" checkout -q "$REPO_BRANCH"
  git -C "$APP_DIR" reset --hard -q "origin/${REPO_BRANCH}"
else
  rm -rf "$APP_DIR"
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi

if [ -f /home/container/.env ]; then
  cp /home/container/.env "${APP_DIR}/.env"
fi

PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

"$PYTHON_BIN" -m pip install --disable-pip-version-check --upgrade --prefix "$DEPS_DIR" -r "${APP_DIR}/requirements.txt"
PYTHON_VERSION=$($PYTHON_BIN -c 'import sys; print("{}.{}".format(sys.version_info[0], sys.version_info[1]))')
export PYTHONPATH="${DEPS_DIR}/lib/python${PYTHON_VERSION}/site-packages:${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
export DATABASE_PATH="${DATABASE_PATH:-${DATA_DIR}/wsp.db}"
export DATA_DIR

if [ -z "${DISCORD_TOKEN:-}" ] && ! grep -q '^DISCORD_TOKEN[[:space:]]*=' "${APP_DIR}/.env" 2>/dev/null; then
  echo "DISCORD_TOKEN is not configured. Add it to the panel environment or /home/container/.env."
  exit 1
fi

cd "$APP_DIR"
exec "$PYTHON_BIN" bot.py