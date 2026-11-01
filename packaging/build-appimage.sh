#!/usr/bin/env bash
# Builds dist/Touch-Seq-VERSION-ARCH.AppImage and dist/touchseq-xyfx-VERSION-ARCH.tar.gz:
# a portable Python (python-appimage) with PySide6 + python-rtmidi, Carla (build-carla.sh), XYFX and the app.
# Needs: git, cmake, a C++ toolchain, curl, X11 headers (see .github/workflows/release.yml).
# Usage: packaging/build-appimage.sh   (ARCH defaults to this machine's)
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export ARCH=${ARCH:-$(uname -m)}
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/touchseq.py")
PY=3.12
WORK="$ROOT/build/appimage-$ARCH"
DIST="$ROOT/dist"
rm -rf "$WORK/AppDir"
mkdir -p "$WORK" "$DIST"
cd "$WORK"

echo "== Python $PY ($ARCH)"
url=$(curl -fsSL "https://api.github.com/repos/niess/python-appimage/releases/tags/python$PY" | python3 -c '
import json, os, sys
assets = json.load(sys.stdin)["assets"]
want = "cp312-cp312-manylinux2014_" + os.environ["ARCH"] + ".AppImage"
print(next(a["browser_download_url"] for a in assets if a["name"].endswith(want)))')
curl -fsSL -o python.AppImage "$url"
chmod +x python.AppImage
APPIMAGE_EXTRACT_AND_RUN=1 ./python.AppImage --appimage-extract >/dev/null
mv squashfs-root AppDir
PYBIN="AppDir/opt/python$PY/bin/python$PY"
[[ -x "$PYBIN" ]] || { echo "python not at $PYBIN:"; ls AppDir/opt; exit 1; }
"$PYBIN" -m pip install --no-warn-script-location -q -r "$ROOT/packaging/requirements.txt"
rm -rf AppDir/*.desktop AppDir/*.png AppDir/.DirIcon AppDir/usr/share/metainfo AppDir/usr/share/applications

echo "== Carla"
CARLA_SRC="$WORK/carla-src" "$ROOT/packaging/build-carla.sh" AppDir/usr  # sources beside, not in, the AppImage

echo "== XYFX"
cmake -B xyfx-build -S "$ROOT/xyfx" -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build xyfx-build -j"$(nproc)" >/dev/null
xyfx-build/xyfx-test
install -Dm755 xyfx-build/bin/touchseq-xyfx.clap AppDir/usr/lib/clap/touchseq-xyfx.clap
mkdir -p AppDir/usr/lib/lv2
cp -r xyfx-build/bin/touchseq-xyfx.lv2 AppDir/usr/lib/lv2/
tar -C xyfx-build/bin -czf "$DIST/touchseq-xyfx-$VERSION-$ARCH.tar.gz" touchseq-xyfx.clap touchseq-xyfx.lv2

echo "== app"
install -Dm644 "$ROOT/touchseq.py" AppDir/usr/share/touch-seq/touchseq.py
install -Dm644 "$ROOT/plugins.py" AppDir/usr/share/touch-seq/plugins.py
install -Dm644 "$ROOT/LICENSE" AppDir/usr/share/touch-seq/LICENSE
install -Dm755 "$ROOT/packaging/AppRun" AppDir/AppRun
install -Dm644 "$ROOT/packaging/touch-seq.desktop" AppDir/touch-seq.desktop
install -Dm644 "$ROOT/packaging/touch-seq.png" AppDir/touch-seq.png
ln -sf touch-seq.png AppDir/.DirIcon
"$PYBIN" -c "import sys; sys.path.insert(0, 'AppDir/usr/share/touch-seq'); import touchseq; print('app', touchseq.__version__)"

echo "== AppImage"
rm -rf "AppDir/opt/python$PY/include"  # C headers: not needed to run
chmod -R a+rX AppDir  # the extracted Python's folders are 0700; others must be able to run the AppImage
curl -fsSL -o appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
chmod +x appimagetool
APPIMAGE_EXTRACT_AND_RUN=1 ./appimagetool --no-appstream AppDir "$DIST/Touch-Seq-$VERSION-$ARCH.AppImage"
ls -la "$DIST"
