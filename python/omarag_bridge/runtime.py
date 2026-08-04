from __future__ import annotations

import ctypes
import gc
import os
from pathlib import Path

_LEAN_ENVIRONMENT = {
    "MALLOC_ARENA_MAX": "2",
    "MALLOC_TRIM_THRESHOLD_": "131072",
    "TOKENIZERS_PARALLELISM": "false",
    "OMP_NUM_THREADS": "4",
    "MKL_NUM_THREADS": "4",
    "OPENBLAS_NUM_THREADS": "4",
    "NUMEXPR_NUM_THREADS": "4",
    "RAYON_NUM_THREADS": "4",
}


def configure_process_environment(cache_dir: Path | None = None) -> None:
    """Apply lean defaults before native/model libraries are imported."""
    for name, value in _LEAN_ENVIRONMENT.items():
        os.environ.setdefault(name, value)
    os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
    if cache_dir is not None:
        os.environ.setdefault("HF_HOME", str(cache_dir / "huggingface"))


def release_native_memory() -> None:
    """Best-effort trimming between requests; worker exit remains the hard boundary."""
    gc.collect()
    if os.name != "posix":
        return
    try:
        allocator = ctypes.CDLL(None)
        trim = allocator.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
    except (AttributeError, OSError):
        pass
