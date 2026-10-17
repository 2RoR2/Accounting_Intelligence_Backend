#!/usr/bin/env bash
# Run the unchanged parser using the existing WSL environment and local outputs.
set -euo pipefail

m2_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
m2_python="${M2_PYTHON:-$HOME/.venvs/accounting-intelligence/bin/python}"

if [[ ! -x "$m2_python" ]]; then
  echo "Python environment not found: $m2_python" >&2
  echo 'Set M2_PYTHON to a Python 3.12 executable with the recorded Docling dependencies.' >&2
  exit 1
fi
if [[ ! -d "$m2_root/models/docling" ]]; then
  echo "Docling models unavailable at $m2_root/models/docling" >&2
  echo 'Connect the SSD and check the existing local model link.' >&2
  exit 1
fi

# Keep cache and temporary writes in the backend checkout, away from SSD sources.
export HF_HOME="$m2_root/.local/parser-cache/huggingface"
export XDG_CACHE_HOME="$m2_root/.local/parser-cache/xdg"
export TORCH_HOME="$m2_root/.local/parser-cache/torch"
export TMPDIR="$m2_root/.local/parser-temp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1
export PYTHONDONTWRITEBYTECODE=1
mkdir -p -- "$HF_HOME" "$XDG_CACHE_HOME" "$TORCH_HOME" "$TMPDIR"

"$m2_python" -B -c 'import sys; from importlib.metadata import version; print("Python:", sys.executable); print(sys.version); print("Docling:", version("docling"))'

for m2_sample in synthetic_invoice_001.pdf changed_values.pdf missing_number.pdf; do
  echo "Parsing copied sample: $m2_sample"
  "$m2_python" -B "$m2_root/project/scripts/baseline_parser.py" \
    "$m2_root/data/development/$m2_sample" --output-dir "$m2_root/runs"
done
