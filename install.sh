#!/usr/bin/env bash
# Install everything the survey kit needs on Debian / Ubuntu (tested on Ubuntu with GNU Radio 3.10):
#   system packages (rtl-sdr, GNU Radio, Python libraries), and gr-lora_sdr built from source with the kit's patch
#   into <kit>/opt (nothing is installed system-wide except the apt packages). Then runs the self-test.
# Usage: ./install.sh            (asks for sudo for apt)
#        ./install.sh --no-apt   (skip apt, e.g. when the packages are already there)
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT"
GR_LORA_URL=https://github.com/tapparelj/gr-lora_sdr.git
GR_LORA_COMMIT=862746d   # the version the patch was written against

if [ "${1:-}" != "--no-apt" ]; then
  command -v apt-get >/dev/null || { echo "apt-get not found: install the packages listed in README.md by hand, then run ./install.sh --no-apt"; exit 1; }
  sudo apt-get update
  sudo apt-get install -y rtl-sdr librtlsdr-dev gnuradio gnuradio-dev cmake build-essential git pkg-config \
       libvolk-dev libboost-all-dev libspdlog-dev libgmp-dev pybind11-dev python3-pybind11 \
       python3-numpy python3-scipy python3-matplotlib ieee-data
fi

for c in rtl_sdr rtl_power rtl_test cmake git python3 gnuradio-config-info; do
  command -v "$c" >/dev/null || { echo "missing: $c"; exit 1; }
done
echo "GNU Radio $(gnuradio-config-info --version)"

# gr-lora_sdr: clone, patch, build, install into $KIT/opt
SRC="$KIT/build/gr-lora_sdr"
if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$KIT/build"
  git clone "$GR_LORA_URL" "$SRC"
fi
git -C "$SRC" checkout -q -f "$GR_LORA_COMMIT"
git -C "$SRC" apply "$KIT/patches/gr-lora_sdr-sdrplan.patch"
mkdir -p "$SRC/build"
( cd "$SRC/build" && cmake -DCMAKE_INSTALL_PREFIX="$KIT/opt" -DCMAKE_BUILD_TYPE=Release .. > "$KIT/build/cmake.log" 2>&1 \
  && make -j"$(nproc)" > "$KIT/build/make.log" 2>&1 && make install > "$KIT/build/install.log" 2>&1 ) \
  || { echo "gr-lora_sdr build failed: see $KIT/build/*.log"; exit 1; }
git -C "$SRC" checkout -q -f "$GR_LORA_COMMIT"   # leave the source tree clean so a rerun can re-apply the patch

. "$KIT/lib/env.sh"
python3 -c "from gnuradio import lora_sdr" || { echo "gr-lora_sdr installed but Python cannot import it (check lib/env.sh)"; exit 1; }
[ -f "$KIT/survey.conf" ] || cp "$KIT/survey.conf.example" "$KIT/survey.conf"
chmod +x "$KIT/survey.sh"
echo
echo "Running the self-test (synthetic signals, no radio needed)..."
python3 "$KIT/tools/selftest.py"
echo
echo "Installed. Next: edit survey.conf (site name, antenna), plug in the RTL-SDR and run ./survey.sh check"
