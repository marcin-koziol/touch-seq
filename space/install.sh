#!/usr/bin/env bash
# Build Touch-Seq Space, run its DSP test, install CLAP + LV2 into the user plugin folders.
# DPF comes from the xyfx/dpf submodule (git submodule update --init --recursive).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build build -j"$(nproc)" >/dev/null
./build/space-test >/dev/null
mkdir -p ~/.clap ~/.lv2
cp build/bin/touchseq-space.clap ~/.clap/
rm -rf ~/.lv2/touchseq-space.lv2
cp -r build/bin/touchseq-space.lv2 ~/.lv2/
echo "installed: ~/.clap/touchseq-space.clap, ~/.lv2/touchseq-space.lv2 (Touch-Seq: RESCAN in the plugin browser)"
