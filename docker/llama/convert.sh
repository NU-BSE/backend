#!/usr/bin/env bash
# Build a servable GGUF from the local merged GUI-Owl 2B teacher.
#
# Runs as a one-shot compose service before llama-server starts, and does
# nothing on every run after the first: each step is skipped when its output
# already exists, so `docker compose up` is cheap and the volume survives.
#
#   merged HF dir --convert_hf_to_gguf--> text f16  + mmproj f16
#   text f16      --llama-quantize------> text Q4_K_M   (what gets served)
#
# Two things differ from the pipeline this replaced, and both come from the
# model itself rather than from preference:
#
# * The weights are already merged and already on disk. The previous script
#   downloaded GUI-Owl-1.5-2B from Hugging Face and merged a LoRA with
#   llama-export-lora; there is no adapter to merge now, and downloading a
#   base we already have locally would be slower and could drift from the
#   weights that were actually trained.
#
# * This is Qwen3-VL — `Qwen3VLForConditionalGeneration`, a vision-language
#   model — so conversion produces *two* files. The text model is what serves
#   chat; the mmproj holds the vision encoder and is required for any request
#   carrying an image. Converting without --mmproj yields a model that answers
#   text fine and silently cannot see, which is the failure worth avoiding
#   because nothing about it looks like an error.
set -euo pipefail

# The merged teacher, mounted read-only by compose.
SRC_DIR="${LLAMA_MODEL_SRC:-/teacher}"
OUT_DIR="${LLAMA_MODEL_DIR:-/models}"
QUANT="${LLAMA_QUANT:-Q4_K_M}"

NAME="${LLAMA_MODEL_NAME:-gui-owl-2b}"
# Named after the quantization so changing QUANT produces a new file rather
# than silently serving the old one.
FINAL="${OUT_DIR}/${NAME}-${QUANT,,}.gguf"
TEXT_F16="${OUT_DIR}/${NAME}-f16.gguf"
MMPROJ="${OUT_DIR}/${NAME}-mmproj-f16.gguf"

log() { printf '[convert] %s\n' "$*" >&2; }

if [[ -f "$FINAL" && -f "$MMPROJ" ]]; then
  log "already built: $FINAL"
  exit 0
fi

mkdir -p "$OUT_DIR"

if [[ ! -f "${SRC_DIR}/config.json" ]]; then
  log "FATAL: no config.json under ${SRC_DIR}"
  log "Mount the merged teacher there (see docker-compose.yml)."
  exit 1
fi

# llama.cpp's layout differs between the official images and a source build.
find_tool() {
  local name="$1" candidate
  for candidate in "/app/${name}" "/app/build/bin/${name}" "$(command -v "${name}" 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s' "$candidate"; return 0
    fi
  done
  return 1
}

find_script() {
  local name="$1" candidate
  for candidate in "/app/${name}" "/opt/llama.cpp/${name}" "/${name}"; do
    if [[ -f "$candidate" ]]; then printf '%s' "$candidate"; return 0; fi
  done
  return 1
}

convert_hf="$(find_script convert_hf_to_gguf.py)" || {
  log "FATAL: convert_hf_to_gguf.py not found in this image"
  exit 1
}

# Fail early and legibly if the image predates Qwen3-VL support. Without this
# the converter dies deep inside a model registry lookup with a message that
# does not mention the image at all.
if ! python3 - "$SRC_DIR" <<'PY'
import json, sys
config = json.load(open(f"{sys.argv[1]}/config.json"))
architectures = config.get("architectures") or []
if not any("Qwen3VL" in a for a in architectures):
    print(f"[convert] unexpected architecture: {architectures}", file=sys.stderr)
    sys.exit(1)
PY
then
  log "FATAL: ${SRC_DIR} is not a Qwen3-VL checkpoint"
  exit 1
fi

# 1. Text model.
if [[ ! -f "$TEXT_F16" ]]; then
  log "converting text model to f16"
  python3 "$convert_hf" "$SRC_DIR" --outfile "$TEXT_F16" --outtype f16
else
  log "text f16 present"
fi

# 2. Vision projector.
#
# A separate invocation because the converter emits one file per call. This is
# the step that makes the model able to see; skipping it is what turns a
# vision model into a text-only one without any error.
if [[ ! -f "$MMPROJ" ]]; then
  log "converting vision projector to f16"
  python3 "$convert_hf" "$SRC_DIR" --outfile "$MMPROJ" --outtype f16 --mmproj
else
  log "mmproj present"
fi

# 3. Quantize the text model.
#
# The projector is left at f16 on purpose: it is a fraction of the total size,
# and quantizing a vision encoder costs visible accuracy on exactly the
# screen-understanding task this model exists for.
if [[ ! -f "$FINAL" ]]; then
  quantize="$(find_tool llama-quantize)" || {
    log "FATAL: llama-quantize not found in this image"
    exit 1
  }
  log "quantizing text model to ${QUANT}"
  "$quantize" "$TEXT_F16" "${FINAL}.partial" "$QUANT"
  # Rename only on success. A crash mid-quantize would otherwise leave a
  # truncated file at the final path, which the skip-if-exists check above
  # would then treat as a completed build forever.
  mv "${FINAL}.partial" "$FINAL"
fi

# The f16 text intermediate is several GB and is not needed to serve. Kept only
# when asked, for re-quantizing without re-converting.
if [[ "${LLAMA_KEEP_INTERMEDIATES:-0}" != "1" ]]; then
  rm -f "$TEXT_F16"
fi

log "built ${FINAL}"
log "vision  ${MMPROJ}"
