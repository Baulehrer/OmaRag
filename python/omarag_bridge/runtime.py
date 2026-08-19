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
    # Disable library telemetry at the earliest import boundary. These flags
    # carry no user content and do not interfere with explicitly consented
    # artifact downloads.
    # torch builds its inductor compiler command line without quoting paths, so
    # an installation under a directory containing a space fails to link
    # ("ld: cannot find -ltorch"), and a machine without a C++ toolchain fails
    # the same way.  Both turn every document conversion into a hard error.
    # Falling back to eager execution costs a little CPU speed and keeps
    # indexing working everywhere.
    "TORCHDYNAMO_SUPPRESS_ERRORS": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "OLLAMA_NO_CLOUD": "1",
}


def configure_process_environment(
    cache_dir: Path | None = None,
    *,
    offline_models: bool = False,
) -> None:
    """Apply lean defaults before native/model libraries are imported."""
    for name, value in _LEAN_ENVIRONMENT.items():
        os.environ.setdefault(name, value)
    for name in ("NO_PROXY", "no_proxy"):
        values = [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]
        known = {item.casefold() for item in values}
        for local in ("localhost", "127.0.0.1", "::1"):
            if local.casefold() not in known:
                values.append(local)
        os.environ[name] = ",".join(values)
    os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
    if cache_dir is not None:
        os.environ.setdefault("HF_HOME", str(cache_dir / "huggingface"))
    if offline_models:
        # Heavy query/import workers must consume only the artifacts admitted
        # by the parent preflight. The parent process deliberately stays online
        # so a separately confirmed model-install operation can still run.
        os.environ["HF_HUB_OFFLINE"] = "1"
        # Content-bearing worker transports may target loopback or an
        # explicitly trusted endpoint, but must never be silently rerouted by
        # process-wide proxy variables. Artifact downloads run in the parent.
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(name, None)


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
