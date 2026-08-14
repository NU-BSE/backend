#!/usr/bin/env bash
# Build a servable GGUF of the teacher model with the chat adapter baked in.
#
# Runs as a one-shot compose service before llama-server starts, and does
# nothing on every run after the first: each step is skipped when its output
# already exists, so `docker compose up` is cheap and the volume survives.
#
# The adapter is merged with llama.cpp's own tooling rather than PEFT. Merging
# in Python would mean torch, transformers and peft in the image — several GB,
# and a second implementation of a merge that llama-export-lora already does
# against the exact format llama-server will load.
#
#   HF base  --convert_hf_to_gguf-->  base f16
#   LoRA     --convert_lora_to_gguf-> adapter f16
#   base + adapter --llama-export-lora--> merged f16
#   merged   --llama-quantize------->  merged Q4_K_M   (what gets served)
set -euo pipefail

BASE_REPO="${LLAMA_BASE_REPO:-mPLUG/GUI-Owl-1.5-2B-Instruct}"
ADAPTER_DIR="${LLAMA_ADAPTER_DIR:-/adapter}"
OUT_DIR="${LLAMA_MODEL_DIR:-/models}"
QUANT="${LLAMA_QUANT:-Q4_K_M}"
# The served file. Named after the quantization so changing QUANT produces a
# new file rather than silently serving the old one.
FINAL="${OUT_DIR}/gui-owl-1.5-2b-chat-${QUANT,,}.gguf"

HF_DIR="${OUT_DIR}/hf-base"
BASE_GGUF="${OUT_DIR}/gui-owl-1.5-2b-f16.gguf"
LORA_GGUF="${OUT_DIR}/chat-adapter-f16.gguf"
MERGED_GGUF="${OUT_DIR}/gui-owl-1.5-2b-chat-f16.gguf"

log() { printf '[convert] %s\n' "$*" >&2; }

if [[ -f "$FINAL" ]]; then
  log "already built: $FINAL"
  exit 0
fi

mkdir -p "$OUT_DIR"

# llama.cpp's layout differs between the official images and a source build.
find_tool() {
  local name="$1"
  local candidate
  for candidate in "/app/${name}" "/app/build/bin/${name}" "$(command -v "${name}" 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

find_script() {
  local name="$1"
  local candidate
  for candidate in "/app/${name}" "/opt/llama.cpp/${name}" "/${name}"; do
    if [[ -f "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# 1. Base weights.
if [[ ! -d "$HF_DIR" || -z "$(ls -A "$HF_DIR" 2>/dev/null)" ]]; then
  log "downloading ${BASE_REPO}"
  python3 - "$BASE_REPO" "$HF_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download

repo, target = sys.argv[1], sys.argv[2]
snapshot_download(
    repo_id=repo,
    local_dir=target,
    # Weights and config only. The originals include duplicate .bin copies of
    # the safetensors, which doubles a 4GB download for nothing.
    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.jinja"],
)
PY
else
  log "base weights present"
fi

# 2. Base -> GGUF.
if [[ ! -f "$BASE_GGUF" ]]; then
  convert_hf="$(find_script convert_hf_to_gguf.py)" || {
    log "FATAL: convert_hf_to_gguf.py not found in this image"
    exit 1
  }
  log "converting base to f16 GGUF"
  python3 "$convert_hf" "$HF_DIR" --outfile "$BASE_GGUF" --outtype f16
else
  log "base GGUF present"
fi

# 3. Adapter -> GGUF.
#
# The adapter directory is nested one level in the artifacts tree, so accept
# either the directory holding adapter_config.json or its parent.
if [[ ! -f "$LORA_GGUF" ]]; then
  adapter_src="$ADAPTER_DIR"
  if [[ ! -f "${adapter_src}/adapter_config.json" && -f "${adapter_src}/chat/adapter_config.json" ]]; then
    adapter_src="${adapter_src}/chat"
  fi
  if [[ ! -f "${adapter_src}/adapter_config.json" ]]; then
    log "FATAL: no adapter_config.json under ${ADAPTER_DIR}"
    exit 1
  fi

  convert_lora="$(find_script convert_lora_to_gguf.py)" || {
    log "FATAL: convert_lora_to_gguf.py not found in this image"
    exit 1
  }
  log "converting chat adapter from ${adapter_src}"
  python3 "$convert_lora" "$adapter_src" --base "$HF_DIR" --outfile "$LORA_GGUF" --outtype f16
else
  log "adapter GGUF present"
fi

# 4. Merge.
#
# Merging into f16 and quantizing after is deliberate. llama-server can apply a
# LoRA to an already-quantized base at load time, but the adapter was trained
# against full-precision weights, and applying it on top of Q4 rounding is
# where a chat adapter's behaviour quietly degrades. Merge first, quantize once.
if [[ ! -f "$MERGED_GGUF" ]]; then
  if export_lora="$(find_tool llama-export-lora)"; then
    log "merging adapter into base"
    "$export_lora" -m "$BASE_GGUF" --lora "$LORA_GGUF" -o "$MERGED_GGUF"
  else
    log "llama-export-lora unavailable; serving base and applying the adapter at load time"
    cp "$BASE_GGUF" "$MERGED_GGUF"
    printf '%s' "$LORA_GGUF" > "${OUT_DIR}/.runtime-lora"
  fi
else
  log "merged GGUF present"
fi

# 5. Quantize.
quantize="$(find_tool llama-quantize)" || {
  log "FATAL: llama-quantize not found in this image"
  exit 1
}
log "quantizing to ${QUANT}"
"$quantize" "$MERGED_GGUF" "${FINAL}.partial" "$QUANT"
# Rename only on success. A crash mid-quantize would otherwise leave a
# truncated file at the final path, which the skip-if-exists check above would
# then treat as a completed build forever.
mv "${FINAL}.partial" "$FINAL"

# The f16 intermediates are ~4GB together and are not needed to serve. Keep
# them only when asked, for re-quantizing without re-downloading.
if [[ "${LLAMA_KEEP_INTERMEDIATES:-0}" != "1" ]]; then
  rm -f "$MERGED_GGUF"
  rm -rf "$HF_DIR"
fi

log "built ${FINAL}"
