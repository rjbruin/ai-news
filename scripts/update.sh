#!/usr/bin/env bash
#
# AI News updater. Given a version number, downloads that GitHub release,
# builds a fresh virtualenv, runs migrations, flips the `current` symlink,
# restarts the service, health-checks it, and rolls back on failure.
#
# Usage:   sudo ./update.sh <version>          e.g.  sudo ./update.sh 0.2.0
#
# Directory layout on the VPS:
#   /opt/ainews/
#     ├─ shared/.env            (persistent secrets + config)
#     ├─ shared/instance/       (persistent SQLite DB + uploads)
#     ├─ releases/<version>/    (unpacked release)
#     └─ current -> releases/<version>
#
set -euo pipefail

REPO="rjbruin/ai-news"
APP_ROOT="${AINEWS_ROOT:-/opt/ainews}"
SERVICE="${AINEWS_SERVICE:-ainews}"
PORT="${AINEWS_PORT:-5090}"
HEALTH_URL="http://127.0.0.1:${PORT}/"

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 <version>   (e.g. $0 0.2.0)" >&2
  exit 1
fi
VERSION="${VERSION#v}"  # tolerate a leading 'v'

RELEASES="$APP_ROOT/releases"
SHARED="$APP_ROOT/shared"
TARGET="$RELEASES/$VERSION"
CURRENT_LINK="$APP_ROOT/current"
PREVIOUS="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"

TARBALL_URL="https://github.com/$REPO/archive/refs/tags/v${VERSION}.tar.gz"

echo "==> Updating AI News to v${VERSION}"
mkdir -p "$RELEASES" "$SHARED/instance"

if [[ -d "$TARGET" ]]; then
  echo "    Release dir already exists, reusing: $TARGET"
else
  echo "==> Downloading $TARBALL_URL"
  tmp="$(mktemp -d)"
  curl -fSL "$TARBALL_URL" -o "$tmp/release.tar.gz"
  mkdir -p "$TARGET"
  tar -xzf "$tmp/release.tar.gz" -C "$TARGET" --strip-components=1
  rm -rf "$tmp"
fi

echo "==> Stamping VERSION"
echo "$VERSION" > "$TARGET/VERSION"

echo "==> Linking shared config + data"
ln -sfn "$SHARED/.env" "$TARGET/.env"
rm -rf "$TARGET/instance"
ln -sfn "$SHARED/instance" "$TARGET/instance"

echo "==> Building virtualenv"
python3 -m venv "$TARGET/venv"
"$TARGET/venv/bin/pip" install --upgrade pip >/dev/null
"$TARGET/venv/bin/pip" install -r "$TARGET/requirements.txt"

echo "==> Running database migrations"
pushd "$TARGET" >/dev/null
if [[ -d migrations ]]; then
  FLASK_APP=wsgi.py "$TARGET/venv/bin/flask" db upgrade || {
    echo "    (no migrations applied / alembic not initialised — falling back to init-db)"
    "$TARGET/venv/bin/python" manage.py init-db
  }
else
  "$TARGET/venv/bin/python" manage.py init-db
fi

echo "==> Seeding global tags"
"$TARGET/venv/bin/python" manage.py seed-tags
popd >/dev/null

echo "==> Switching symlink and restarting $SERVICE"
ln -sfn "$TARGET" "$CURRENT_LINK"
systemctl restart "$SERVICE"

echo "==> Health check"
ok=0
for i in $(seq 1 15); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done

if [[ "$ok" -ne 1 ]]; then
  echo "!! Health check FAILED — rolling back" >&2
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    ln -sfn "$PREVIOUS" "$CURRENT_LINK"
    systemctl restart "$SERVICE"
    echo "   Rolled back to $PREVIOUS"
  fi
  exit 1
fi

echo "==> AI News v${VERSION} is live."
echo "    Old releases are kept in $RELEASES (prune manually if needed)."
