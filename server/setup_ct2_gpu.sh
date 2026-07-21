#!/usr/bin/env bash
# Enable CUDA (GPU) support for CTranslate2 / faster-whisper on the NVIDIA DGX Spark
# (GB10, sm_121, aarch64, CUDA 13).
#
# Why this is needed:
#   The PyPI `ctranslate2` wheel for aarch64 is CPU-only. This script installs a
#   community-built CUDA-enabled libctranslate2 (v4.6.0, built for CUDA 13 / sm_121)
#   and rebuilds the Python bindings against it with a matching C++ ABI.
#
# Prerequisites (already handled by `uv sync`):
#   - .venv with torch (bundles cuDNN 9) and ctranslate2==4.6.0 installed
#   - system CUDA 13 toolkit at /usr/local/cuda (provides libcublas.so.13)
#   - g++, cmake, git, curl
#
# Idempotent: safe to re-run (e.g. after recreating the venv).
set -euo pipefail

# This bundle is flat (script sits directly in server/), so ROOT_DIR is the
# script's own directory — the same dir uv creates .venv in.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV="$ROOT_DIR/.venv"
PY="$VENV/bin/python"
SP="$($PY -c 'import site; print(site.getsitepackages()[0])')"
CACHE="$ROOT_DIR/.cache"
VENDOR="$ROOT_DIR/vendor/ct2"

CT2_VERSION="4.6.0"
CT2_TARBALL_URL="https://github.com/assix/ctranslate2-aarch64-cuda13-binaries/releases/download/v4.6.0-cuda13-aarch64/ctranslate2-dgxspark-aarch64-cuda13.tar.gz"
OPENBLAS_DEB="libopenblas0-pthread"
CUDART_DIR="/usr/local/cuda/targets/sbsa-linux/lib"

echo "[setup] site-packages: $SP"
mkdir -p "$CACHE"

# 0. Preflight: the CTranslate2 bindings are compiled from source, so the venv's
#    Python must ship development headers (Python.h). uv-managed interpreters include
#    them; a venv built from a bare system python often does not.
PY_INC="$($PY -c 'import sysconfig; print(sysconfig.get_path("include"))')"
if [ ! -f "$PY_INC/Python.h" ]; then
  cat >&2 <<EOF
[setup] ERROR: Python.h not found under: $PY_INC
        The venv's Python has no development headers, so the CTranslate2 bindings
        cannot be compiled.

        Fix: recreate the venv with a uv-managed Python (which includes headers):

            rm -rf .venv
            uv python install 3.12
            uv sync
            bash scripts/setup_ct2_gpu.sh

        (The project pins python-preference = "only-managed", so a fresh
        'uv sync' after removing .venv will use a managed interpreter.)
EOF
  exit 1
fi

# 1. Ensure build tooling in the venv.
echo "[setup] installing patchelf/pybind11/setuptools/wheel into venv"
uv pip install patchelf pybind11 setuptools wheel >/dev/null

# 2. Download + extract the prebuilt CUDA libctranslate2.
if [ ! -f "$CACHE/ct2extract/ctranslate2/lib/libctranslate2.so.${CT2_VERSION}" ]; then
  echo "[setup] downloading prebuilt CUDA libctranslate2 ${CT2_VERSION}"
  curl -sL "$CT2_TARBALL_URL" -o "$CACHE/ct2.tar.gz"
  rm -rf "$CACHE/ct2extract" && mkdir -p "$CACHE/ct2extract"
  tar xzf "$CACHE/ct2.tar.gz" -C "$CACHE/ct2extract"
fi

# 3. Fetch libopenblas.so.0 (standard symbols) without root, by extracting the .deb.
if [ ! -f "$CACHE/openblas/libopenblas.so.0" ]; then
  echo "[setup] fetching $OPENBLAS_DEB"
  rm -rf "$CACHE/debs" && mkdir -p "$CACHE/debs"
  ( cd "$CACHE/debs" && apt-get download "$OPENBLAS_DEB" && for d in *.deb; do dpkg-deb -x "$d" extracted; done )
  mkdir -p "$CACHE/openblas"
  cp "$CACHE"/debs/extracted/usr/lib/aarch64-linux-gnu/openblas-pthread/libopenblas.so.0 "$CACHE/openblas/"
fi

# 4. Assemble a stable vendor dir and patch its RUNPATH to find cuDNN/cuBLAS/OpenBLAS.
echo "[setup] assembling vendor dir at $VENDOR"
rm -rf "$ROOT_DIR/vendor" && mkdir -p "$ROOT_DIR/vendor"
cp -r "$CACHE/ct2extract/ctranslate2" "$VENDOR"
cp "$CACHE/openblas/libopenblas.so.0" "$VENDOR/lib/"

# cuDNN 9 and cuBLAS 13 ship inside the torch wheel; their exact subdir differs between
# CUDA 12 (nvidia/cublas/lib) and CUDA 13 (nvidia/cu13/lib) wheels, so discover them.
CUDNN="$(dirname "$(find "$SP/nvidia" -name 'libcudnn.so.9' | head -1)")"
CUBLAS="$(dirname "$(find "$SP/nvidia" -name 'libcublas.so.13' | head -1)")"
echo "[setup] cuDNN dir:  ${CUDNN:-<none>}"
echo "[setup] cuBLAS dir: ${CUBLAS:-<none>}"
"$VENV/bin/patchelf" --set-rpath "\$ORIGIN:$CUDNN:$CUBLAS:$CUDART_DIR" \
  "$VENDOR/lib/libctranslate2.so.${CT2_VERSION}"

# 5. Build the Python bindings against the CUDA lib (default gcc => new C++11 ABI,
#    matching the prebuilt lib), then install the extension into the venv.
if [ ! -d "$CACHE/CTranslate2" ]; then
  echo "[setup] cloning CTranslate2 v${CT2_VERSION} (python bindings)"
  git clone --branch "v${CT2_VERSION}" --depth 1 https://github.com/OpenNMT/CTranslate2 "$CACHE/CTranslate2"
fi

echo "[setup] building ctranslate2 python bindings"
(
  cd "$CACHE/CTranslate2/python"
  rm -rf build ctranslate2/_ext*.so
  CTRANSLATE2_ROOT="$VENDOR" LDFLAGS="-Wl,-rpath,$VENDOR/lib" \
    "$PY" setup.py build_ext --inplace >/dev/null
)
cp "$CACHE"/CTranslate2/python/ctranslate2/_ext*.so \
   "$SP/ctranslate2/_ext.cpython-312-aarch64-linux-gnu.so"

# 6. Verify.
echo "[setup] verifying CUDA support"
"$PY" - <<'PYEOF'
import ctranslate2
n = ctranslate2.get_cuda_device_count()
print(f"  ctranslate2 {ctranslate2.__version__}  cuda_devices={n}")
print(f"  cuda compute types: {sorted(ctranslate2.get_supported_compute_types('cuda'))}")
assert n >= 1, "CUDA device not visible to CTranslate2"
print("[setup] OK: faster-whisper GPU is enabled.")
PYEOF
