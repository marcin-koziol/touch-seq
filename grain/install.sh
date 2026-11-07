#!/usr/bin/env bash
# Build Touch-Seq Grain, run its DSP test, install CLAP + LV2 into the user plugin folders.
# DPF comes from the xyfx/dpf submodule (git submodule update --init --recursive).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build build -j"$(nproc)" >/dev/null
./build/grain-test >/dev/null
mkdir -p ~/.clap ~/.lv2
cp build/bin/touchseq-grain.clap ~/.clap/
rm -rf ~/.lv2/touchseq-grain.lv2
cp -r build/bin/touchseq-grain.lv2 ~/.lv2/
echo "installed: ~/.clap/touchseq-grain.clap, ~/.lv2/touchseq-grain.lv2 (Touch-Seq: RESCAN in the plugin browser)"
