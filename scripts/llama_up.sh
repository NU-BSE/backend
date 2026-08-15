#!/usr/bin/env bash
# Bring up the llama.cpp cloud agent, preferring CUDA and falling back to CPU.
#
# This exists because compose cannot make the choice. A GPU reservation is a
# hard error on a host without one, so `docker compose up` cannot be written to
# try CUDA and degrade — something has to probe the host first and select a
# profile. That something is this script; the profiles themselves live in
# docker-compose.yml and can still be driven by hand.
#
#   scripts/llama_up.sh          # detect, convert if needed, serve
#   LLAMA_FORCE=cpu  scripts/llama_up.sh
#   LLAMA_FORCE=gpu  scripts/llama_up.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

log() { printf '[llama] %s\n' "$*" >&2; }

detect_profile() {
  case "${LLAMA_FORCE:-auto}" in
    cpu) log "forced CPU"; printf 'llama-cpu'; return ;;
    gpu) log "forced GPU"; printf 'llama-gpu'; return ;;
  esac

  # Three things must hold, and each fails differently:
  #   1. a driver on the host,
  #   2. a device it can actually enumerate,
  #   3. a container runtime that can pass it through.
  # Checking only for nvidia-smi is the usual mistake — it is present on plenty
  # of hosts whose driver is broken or whose Docker has no GPU runtime, and
  # compose then fails at start with a much less obvious message.
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    log "no nvidia-smi; using CPU"
    printf 'llama-cpu'
    return
  fi
  if ! nvidia-smi -L >/dev/null 2>&1; then
    log "nvidia-smi present but no device answered; using CPU"
    printf 'llama-cpu'
    return
  fi
  if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    log "GPU present but Docker has no nvidia runtime; using CPU"
    log "install nvidia-container-toolkit to use it"
    printf 'llama-cpu'
    return
  fi

  log "GPU: $(nvidia-smi -L | head -1)"
  printf 'llama-gpu'
}

profile="$(detect_profile)"
log "profile: ${profile}"

# The conversion is a one-shot that must finish before the server starts, and
# it can take a long while on a cold volume (a ~4GB download, then a merge and
# a quantize). Run it in the foreground so its progress is visible and a
# failure stops here rather than leaving the server crash-looping on a missing
# model file.
log "converting (skips anything already built)"
docker compose --profile "$profile" run --rm llama-convert

log "starting server"
docker compose --profile "$profile" up -d "$profile"

log "waiting for health"
for _ in $(seq 1 120); do
  if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
    log "ready on http://127.0.0.1:8080"
    log "point the API at it:"
    log "  LLM_UPSTREAM_URL=http://${profile}:8080/v1/chat/completions"
    log "  LLM_MODEL=${LLAMA_MODEL_ALIAS:-gui-owl-1.5-2b-chat}"
    log "  LLM_MOCK=false"
    exit 0
  fi
  sleep 5
done

log "server did not become healthy; logs follow"
docker compose --profile "$profile" logs --tail 50 "$profile"
exit 1
