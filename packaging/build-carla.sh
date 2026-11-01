#!/usr/bin/env bash
# Builds Carla's engine library and plugin scanner (no Carla GUI) from git and lays them out the way
# `make install` would, which is what Touch-Seq looks for with CARLA_PREFIX=DEST:
#   DEST/lib/carla/libcarla_standalone2.so, DEST/lib/carla/carla-discovery-native
#   DEST/share/carla/carla_backend.py, DEST/share/carla/common/
# Released Carla (2.5.x) can't host CLAP yet, hence a pinned git commit.
# Usage: packaging/build-carla.sh DEST [commit]
set -euo pipefail
DEST=$(realpath -m "$1")
COMMIT=${2:-97a9e0740baf6df2df942495c02532a624c44682}
SRC=${CARLA_SRC:-build/carla-src}

if [[ ! -d "$SRC/.git" ]]; then
    git clone --filter=blob:none https://github.com/falkTX/Carla.git "$SRC"
fi
git -C "$SRC" checkout -q "$COMMIT"

# only what Touch-Seq uses: no Qt/PyQt frontend, no OSC, no fluidsynth/sndfile/libmagic (fewer runtime deps)
make -C "$SRC" -j"$(nproc)" backend discovery \
    HAVE_FRONTEND=false HAVE_PYQT=false HAVE_QT4=false HAVE_QT5=false HAVE_QT5PKG=false HAVE_QT6=false \
    HAVE_LIBLO=false HAVE_FLUIDSYNTH=false HAVE_SNDFILE=false HAVE_LIBMAGIC=false

install -Dm755 "$SRC/bin/libcarla_standalone2.so" "$DEST/lib/carla/libcarla_standalone2.so"
install -Dm755 "$SRC/bin/carla-discovery-native" "$DEST/lib/carla/carla-discovery-native"
install -Dm644 "$SRC/source/frontend/carla_backend.py" "$DEST/share/carla/carla_backend.py"
install -Dm644 "$SRC/source/frontend/common/__init__.py" "$DEST/share/carla/common/__init__.py"
mkdir -p "$DEST/share/carla/resources"
echo "Carla $(git -C "$SRC" rev-parse --short HEAD) -> $DEST"
