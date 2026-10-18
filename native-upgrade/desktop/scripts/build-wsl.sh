set -eu
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="${MAIMI_DESKTOP_BUILD_ROOT:-$HOME/maimi-r65}"
STAGE="$BASE/desktop-native"
mkdir -p "$BASE"
case "$BASE" in /*) ;; *) echo ABSOLUTE_BUILD_ROOT_REQUIRED; exit 2;; esac
if test -e "$STAGE"; then echo DESKTOP_NATIVE_STAGE_ALREADY_EXISTS; exit 2; fi
mkdir "$STAGE"
printf "%s  %s\n" "157903b22c2881d3678d846755afe957a48df834e747deda805a1195ce25a146" "$SRC/upstream/source-687f282.tar.gz" | sha256sum -c -
tar -xzf "$SRC/upstream/source-687f282.tar.gz" --strip-components=1 -C "$STAGE"
cd "$STAGE"
git apply "$SRC/desktop/patches/native-shell.patch"
cp "$SRC/desktop/src/company-shell-host.ts" apps/desktop/src/company-shell-host.ts
mkdir apps/desktop/maimi
cp -R "$SRC/desktop/src/." apps/desktop/maimi/
npm install --prefix "$BASE/desktop-build-tools" --ignore-scripts --no-audit --no-fund pnpm@11.28.5
node "$BASE/desktop-build-tools/node_modules/pnpm/bin/pnpm.mjs" install --frozen-lockfile --ignore-scripts --reporter=append-only > "$BASE/desktop-native-install.log" 2>&1
printf 'NATIVE_FROZEN_INSTALL_COMPLETE\n'
