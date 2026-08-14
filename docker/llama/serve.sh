#!/usr/bin/env bash
# Start llama-server on the converted model, using the GPU when there is one.
#
# The GPU decision is made twice, at two different levels, because neither is
# sufficient alone:
#
#   * Which image and whether the container can see a GPU at all is a compose
#     concern, decided by scripts/llama_up.sh before anything starts. Compose
#     cannot probe hardware, and a `gpus: all` reservation is a hard error on a
#     machine without one.
#   * How many layers to offload is a runtime concern, decided here. A CUDA
#     image can still end up on a host whose driver is missing or mismatched,
#     and that must degrade to CPU rather than crash-loop.
set -euo pipefail

MODEL_DIR="${LLAMA_MODEL_DIR:-/models}"
QUANT="${LLAMA_QUANT:-Q4_K_M}"
MODEL="${MODEL_DIR}/gui-owl-1.5-2b-chat-${QUANT,,}.gguf"
PORT="${LLAMA_PORT:-8080}"
CTX="${LLAMA_CTX_SIZE:-8192}"
PARALLEL="${LLAMA_PARALLEL:-2}"
ALIAS="${LLAMA_MODEL_ALIAS:-gui-owl-1.5-2b-chat}"

log() { printf '[serve] %s\n' "$*" >&2; }

if [[ ! -f "$MODEL" ]]; then
  log "FATAL: $MODEL does not exist. The converter should have produced it."
  exit 1
fi

find_tool() {
  local name="$1" candidate
  for candidate in "/app/${name}" "/app/build/bin/${name}" "$(command -v "${name}" 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

server="$(find_tool llama-server)" || {
  log "FATAL: llama-server not found in this image"
  exit 1
}

# A GPU is usable only if the runtime is present *and* a device answers.
# nvidia-smi existing proves nothing: it is in the CUDA image regardless.
gpu_layers=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  gpu_layers="${LLAMA_GPU_LAYERS:-999}"
  log "GPU detected: $(nvidia-smi -L 2>/dev/null | head -1)"
  log "offloading ${gpu_layers} layers"
else
  log "no usable GPU; running on CPU"
fi

args=(
  --model "$MODEL"
  --alias "$ALIAS"
  --host 0.0.0.0
  --port "$PORT"
  --ctx-size "$CTX"
  --parallel "$PARALLEL"
  --n-gpu-layers "$gpu_layers"
)

# Set by the converter only when it could not merge the adapter ahead of time.
if [[ -f "${MODEL_DIR}/.runtime-lora" ]]; then
  lora="$(cat "${MODEL_DIR}/.runtime-lora")"
  if [[ -f "$lora" ]]; then
    log "applying chat adapter at load time: $lora"
    args+=(--lora "$lora")
  fi
fi

if [[ "$gpu_layers" == "0" ]]; then
  # Threads matter only on CPU; llama.cpp's default is conservative in a
  # container because it reads the host's core count, not the cgroup limit.
  args+=(--threads "${LLAMA_THREADS:-$(nproc)}")
fi

log "starting llama-server on :${PORT}"
exec "$server" "${args[@]}"
