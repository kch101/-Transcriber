#!/bin/zsh
# Собирает «Расшифровка.app» — лёгкую обёртку, которая запускает main.py из Python-окружения.
# Использование: ./build_app.sh [папка назначения]   (по умолчанию ~/Applications)
# Python берётся из $PYTHON, иначе из .venv рядом со скриптом, иначе из ../work_env.
set -e
APP_DIR="${0:A:h}"
if [[ -n "$PYTHON" ]]; then PY="$PYTHON"
elif [[ -x "$APP_DIR/.venv/bin/python" ]]; then PY="$APP_DIR/.venv/bin/python"
else PY="${APP_DIR:h}/work_env/bin/python"; fi
DEST="${1:-$HOME/Applications}"
NAME="Расшифровка"
BUNDLE="$DEST/$NAME.app"

[[ -x "$PY" ]] || { echo "Не найден Python окружения: $PY. Создайте .venv (см. README)"; exit 1; }
echo "Python: $PY"
mkdir -p "$DEST"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

# Иконка
ICONSET="$(mktemp -d)/icon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$APP_DIR/assets/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$APP_DIR/assets/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUNDLE/Contents/Resources/icon.icns"
rm -rf "${ICONSET:h}"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>local.rasshifrovka</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$BUNDLE/Contents/MacOS/launcher" <<LAUNCH
#!/bin/zsh
LOG="\$HOME/Library/Logs/$NAME.log"
exec "$PY" "$APP_DIR/main.py" >>"\$LOG" 2>&1
LAUNCH
chmod +x "$BUNDLE/Contents/MacOS/launcher"
touch "$BUNDLE"
echo "Готово: $BUNDLE"
