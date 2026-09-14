#!/usr/bin/env bash
set -euo pipefail
# Use a dedicated local server. The systemd unit also enforces memory/device limits.
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_MAX_QUEUE=8
export OLLAMA_KEEP_ALIVE=0 OLLAMA_NO_CLOUD=1 OLLAMA_VULKAN=0
export CUDA_VISIBLE_DEVICES=-1 HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1
export OMP_NUM_THREADS=6 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
exec "${HR_OLLAMA_BINARY:-ollama}" serve
