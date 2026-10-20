# Source this to use the kit's patched gr-lora_sdr build (install.sh puts it in <kit>/opt):  . lib/env.sh
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KIT_DIR
_opt="$KIT_DIR/opt"
_lib="$(dirname "$(find -L "$_opt" -name 'libgnuradio-lora_sdr.so*' -print -quit 2>/dev/null)" 2>/dev/null)"
_py="$(dirname "$(find -L "$_opt" -type d -name lora_sdr -path '*-packages/gnuradio/*' -print -quit 2>/dev/null)" 2>/dev/null)"
[ -n "$_lib" ] && [ "$_lib" != "." ] && export LD_LIBRARY_PATH="$_lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# PYTHONPATH must point at the directory that holds the gnuradio/ package directory
[ -n "$_py" ] && [ "$_py" != "." ] && export PYTHONPATH="$(dirname "$_py")${PYTHONPATH:+:$PYTHONPATH}"
unset _opt _lib _py
