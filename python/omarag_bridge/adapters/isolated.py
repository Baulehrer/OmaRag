from __future__ import annotations

import asyncio
import inspect
import json
import multiprocessing
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from importlib import metadata
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models.domain import BookMetadata, CapabilitySet, Citation, EvidenceMode, SearchHit
from ..models.errors import OmaRagError
from ..runtime import configure_process_environment, release_native_memory
from .base import HaikuAdapter

_QUERY_OPERATIONS = {"warm", "search", "ask", "analyze", "citation_details"}
_CALLBACK_NAMES = {"segment_guard", "before_segment", "on_segment", "on_phase", "segment_sizer"}


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    memory_high: int
    memory_max: int
    memory_swap_max: int
    tasks_max: int = 96


def _haiku_version() -> str | None:
    for distribution in ("haiku-rag", "haiku-rag-slim", "haiku.rag"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return None


def _version_pair(version: str | None) -> tuple[int, int]:
    try:
        major, minor, *_ = (version or "").split(".")
        return int(major), int(minor)
    except (TypeError, ValueError):
        return 0, 0


def _remote_error(payload: dict[str, Any]) -> OmaRagError:
    error = OmaRagError(
        str(payload.get("message", "Worker operation failed")),
        details=payload.get("details"),
    )
    error.code = str(payload.get("code", "WORKER_FAILED"))
    error.status_code = int(payload.get("status_code", 500))
    error.retryable = bool(payload.get("retryable", True))
    return error


def _error_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "message": getattr(exc, "message", str(exc)),
        "details": getattr(exc, "details", {}),
        "code": getattr(exc, "code", "WORKER_FAILED"),
        "status_code": getattr(exc, "status_code", 500),
        "retryable": getattr(exc, "retryable", True),
    }


def _set_parent_death_signal() -> None:
    if os.name != "posix":
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        libc.prctl(1, signal.SIGTERM, 0, 0, 0)
    except (AttributeError, OSError):
        pass


def _memory_usage() -> int:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key in {"VmRSS", "VmSwap"}:
                values[key] = int(raw.strip().split()[0]) * 1024
        return values.get("VmRSS", 0) + values.get("VmSwap", 0)
    except (OSError, ValueError, IndexError):
        return 0


def _memory_watchdog(limit: int, stop: threading.Event) -> None:
    while not stop.wait(0.25):
        if limit > 0 and _memory_usage() > limit:
            os._exit(137)


def _current_cgroup() -> Path | None:
    try:
        entry = next(
            line
            for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
            if line.startswith("0::")
        )
        return Path("/sys/fs/cgroup") / entry.split("::", 1)[1].lstrip("/")
    except (OSError, StopIteration, ValueError):
        return None


def _delegated_root_cgroup() -> Path | None:
    current = _current_cgroup()
    if current is not None and current.name == "oracle-api":
        return current.parent
    return current


def _isolate_api_cgroup(limits: WorkerLimits) -> None:
    """Move the daemon into a no-swap sibling of the heavy worker cgroups."""
    root = _delegated_root_cgroup()
    if root is None or root.name == "oracle-api":
        return
    try:
        target = root / "oracle-api"
        target.mkdir(exist_ok=True)
        # cgroup v2 forbids enabling domain controllers while the parent
        # contains a process. Move the API first, then delegate memory/pids to
        # its sibling worker cgroups and apply the API-specific ceilings.
        (target / "cgroup.procs").write_text(str(os.getpid()), encoding="ascii")
        subtree = root / "cgroup.subtree_control"
        if subtree.exists():
            subtree.write_text("+memory +pids", encoding="ascii")
        for name, value in (
            ("memory.high", limits.memory_high),
            ("memory.max", limits.memory_max),
            ("memory.swap.max", limits.memory_swap_max),
            ("pids.max", limits.tasks_max),
        ):
            setting = target / name
            if setting.exists():
                setting.write_text(str(value), encoding="ascii")
    except OSError:
        # Development shells and rootless containers may not delegate cgroups.
        pass


def _reclaim_inactive_file_cache() -> None:
    """Discard worker file cache without swapping the small API process."""
    cgroup = _delegated_root_cgroup()
    if cgroup is None:
        return
    try:
        for child in cgroup.glob("oracle-worker-*"):
            if (child / "cgroup.procs").read_text(encoding="ascii").strip():
                return
        stats = {
            key: int(value)
            for key, value in (
                line.split(maxsplit=1)
                for line in (cgroup / "memory.stat").read_text(encoding="ascii").splitlines()
            )
        }
        file_cache = stats.get("file", 0)
        if file_cache < 16 * 1024**2:
            return
        # A plain memory.reclaim may swap anonymous API pages. Linux cgroup v2's
        # nested key keeps this strictly to page cache.
        (cgroup / "memory.reclaim").write_text(f"{file_cache} swappiness=0", encoding="ascii")
    except (OSError, ValueError):
        # Process exit remains the hard guarantee on kernels/containers which
        # do not expose delegated cache reclaim.
        pass


def _ollama_targets(database: Path, operation: str, fallback_url: str) -> set[tuple[str, str]]:
    try:
        import yaml

        config_path = database.parent.parent / "haiku.rag.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        base_url = str(
            config.get("providers", {}).get("ollama", {}).get("base_url") or fallback_url
        ).rstrip("/")
        sections = ["embeddings", "reranking"]
        if operation in {"ask", "analyze"}:
            sections.append("qa")
        targets: set[tuple[str, str]] = set()
        for section in sections:
            model = config.get(section, {}).get("model", {})
            if isinstance(model, dict) and model.get("provider") == "ollama" and model.get("name"):
                targets.add((base_url, str(model["name"])))
        return targets
    except (AttributeError, OSError, TypeError, ValueError):
        return set()


def _unload_ollama_targets(targets: set[tuple[str, str]]) -> None:
    for base_url, model in targets:
        payload = json.dumps({"model": model, "keep_alive": 0}).encode()
        request = urllib.request.Request(
            f"{base_url}/api/generate",
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except (OSError, urllib.error.URLError):
            # Ollama residency cleanup must never turn a completed answer into
            # an API failure; the server's own keep_alive remains the fallback.
            pass


class _ChildCallbacks:
    def __init__(self, connection: Connection, enabled: set[str]) -> None:
        self.connection = connection
        self.enabled = enabled
        self.sequence = 0

    def _call(self, name: str, *args: Any) -> Any:
        self.sequence += 1
        callback_id = self.sequence
        self.connection.send({"type": "callback", "id": callback_id, "name": name, "args": args})
        response = self.connection.recv()
        if response.get("type") != "callback_result" or response.get("id") != callback_id:
            raise RuntimeError("Invalid callback response from Oracle daemon")
        if response.get("error"):
            raise _remote_error(response["error"])
        return response.get("result")

    async def before_segment(self, start: int, end: int, pages: int) -> bool:
        return bool(self._call("before_segment", start, end, pages))

    async def on_segment(self, segment: dict[str, Any]) -> None:
        self._call("on_segment", segment)

    async def on_phase(self, phase: str, start: int, end: int, pages: int) -> None:
        self._call("on_phase", phase, start, end, pages)

    def segment_sizer(self, preferred: int, scanned: bool) -> int:
        return int(self._call("segment_sizer", preferred, scanned))

    @asynccontextmanager
    async def segment_guard(self):
        guard_id = str(uuid4())
        self._call("segment_guard_enter", guard_id)
        try:
            yield
        finally:
            self._call("segment_guard_exit", guard_id)

    def options(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if "before_segment" in self.enabled:
            result["before_segment"] = self.before_segment
        if "on_segment" in self.enabled:
            result["on_segment"] = self.on_segment
        if "on_phase" in self.enabled:
            result["on_phase"] = self.on_phase
        if "segment_sizer" in self.enabled:
            result["segment_sizer"] = self.segment_sizer
        if "segment_guard" in self.enabled:
            result["segment_guard"] = self.segment_guard
        return result


async def _execute_request(adapter: Any, connection: Connection, request: dict[str, Any]) -> Any:
    callbacks = _ChildCallbacks(connection, set(request.get("callbacks", [])))
    kwargs = dict(request.get("kwargs", {}))
    kwargs.update(callbacks.options())
    operation = getattr(adapter, str(request["operation"]))
    result = operation(*request.get("args", ()), **kwargs)
    return await result if inspect.isawaitable(result) else result


def _worker_main(
    connection: Connection,
    idle_seconds: float,
    memory_max: int,
) -> None:
    configure_process_environment()
    _set_parent_death_signal()
    stop = threading.Event()
    watchdog = threading.Thread(
        target=_memory_watchdog,
        args=(memory_max, stop),
        name="oracle-memory-guard",
        daemon=True,
    )
    watchdog.start()
    try:
        # This is the intentional heavy import boundary.
        from .haiku_v070 import VanillaHaikuAdapter

        adapter = VanillaHaikuAdapter()
        while True:
            if idle_seconds > 0 and not connection.poll(idle_seconds):
                return
            try:
                request = connection.recv()
            except EOFError:
                return
            if request.get("type") == "shutdown":
                return
            try:
                result = asyncio.run(_execute_request(adapter, connection, request))
                connection.send({"type": "result", "result": result})
            except BaseException as exc:
                connection.send({"type": "error", "error": _error_payload(exc)})
            finally:
                release_native_memory()
            if idle_seconds <= 0:
                return
    finally:
        stop.set()
        connection.close()


class _WorkerCgroup:
    """Best-effort cgroup-v2 child limit; watchdog remains the portable fallback."""

    def __init__(self, pid: int, limits: WorkerLimits) -> None:
        self.path: Path | None = None
        try:
            parent = _delegated_root_cgroup()
            if parent is None:
                return
            target = parent / f"oracle-worker-{pid}"
            target.mkdir()
            for name, value in (
                ("memory.high", limits.memory_high),
                ("memory.max", limits.memory_max),
                ("memory.swap.max", limits.memory_swap_max),
                ("pids.max", limits.tasks_max),
            ):
                setting = target / name
                if setting.exists():
                    setting.write_text(str(value), encoding="ascii")
            (target / "cgroup.procs").write_text(str(pid), encoding="ascii")
            self.path = target
        except (OSError, StopIteration, ValueError):
            self.path = None

    def close(self) -> None:
        if self.path is not None:
            for _ in range(6):
                try:
                    self.path.rmdir()
                    return
                except FileNotFoundError:
                    return
                except OSError:
                    # A just-exited multithreaded worker can remain attached
                    # for a few scheduler ticks after Process.join().
                    time.sleep(0.05)


@dataclass(slots=True)
class _WorkerHandle:
    process: multiprocessing.Process
    connection: Connection
    cgroup: _WorkerCgroup

    @property
    def alive(self) -> bool:
        return self.process.is_alive()

    def terminate(self) -> None:
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=3)
        if self.process.is_alive() and hasattr(self.process, "kill"):
            self.process.kill()
        self.process.join(timeout=2)
        self.connection.close()
        self.cgroup.close()
        _reclaim_inactive_file_cache()


class IsolatedHaikuAdapter(HaikuAdapter):
    """Keep Haiku and all model libraries outside the long-lived API process."""

    name = "haiku-vanilla-isolated"

    def __init__(
        self,
        *,
        api_limits: WorkerLimits,
        import_limits: WorkerLimits,
        query_limits: WorkerLimits,
        utility_limits: WorkerLimits,
        query_idle_seconds: float = 30.0,
        ollama_url: str = "http://127.0.0.1:11434",
        unload_ollama_models: bool = True,
    ) -> None:
        _isolate_api_cgroup(api_limits)
        self.version = _haiku_version()
        self._available = self.version is not None
        supports_images = self._available and _version_pair(self.version) >= (0, 72)
        self.capabilities = CapabilitySet(
            streaming_chat=False,
            question_images=supports_images,
            analysis_images=supports_images,
            visual_grounding=supports_images,
            evaluation=True,
        )
        self.import_limits = import_limits
        self.query_limits = query_limits
        self.utility_limits = utility_limits
        self.query_idle_seconds = query_idle_seconds
        self.ollama_url = ollama_url
        self.unload_ollama_models = unload_ollama_models
        self._context = multiprocessing.get_context("spawn")
        self._query_worker: _WorkerHandle | None = None
        self._query_reaper: asyncio.Task[None] | None = None
        self._query_ollama_targets: set[tuple[str, str]] = set()
        self._query_lock = asyncio.Lock()
        self._residency_policy: Callable[[], float] = lambda: self.query_idle_seconds
        self._query_expires_at = 0.0

    @property
    def available(self) -> bool:
        return self._available

    @property
    def query_worker_state(self) -> str:
        return "ready" if self._query_worker is not None and self._query_worker.alive else "idle"

    @property
    def worker_expires_in_seconds(self) -> float:
        return max(0.0, self._query_expires_at - time.monotonic())

    def set_residency_policy(self, policy: Callable[[], float]) -> None:
        self._residency_policy = policy

    def _spawn(self, limits: WorkerLimits, idle_seconds: float) -> _WorkerHandle:
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker_main,
            args=(child, idle_seconds, limits.memory_max),
            name="omarag-haiku-worker",
        )
        process.start()
        child.close()
        return _WorkerHandle(process, parent, _WorkerCgroup(process.pid, limits))

    async def _callback(
        self,
        message: dict[str, Any],
        callbacks: dict[str, Any],
        guards: dict[str, AbstractAsyncContextManager[Any]],
    ) -> Any:
        name = str(message["name"])
        args = tuple(message.get("args", ()))
        if name == "segment_guard_enter":
            guard_id = str(args[0])
            manager = callbacks["segment_guard"]()
            await manager.__aenter__()
            guards[guard_id] = manager
            return None
        if name == "segment_guard_exit":
            manager = guards.pop(str(args[0]))
            await manager.__aexit__(None, None, None)
            return None
        callback = callbacks[name]
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    async def _receive(
        self,
        worker: _WorkerHandle,
        callbacks: dict[str, Any],
    ) -> Any:
        guards: dict[str, AbstractAsyncContextManager[Any]] = {}
        try:
            while True:
                try:
                    message = await asyncio.to_thread(worker.connection.recv)
                except EOFError as exc:
                    worker.process.join(timeout=0.2)
                    raise _remote_error(
                        {
                            "message": (
                                "Haiku worker exceeded its resource limit"
                                if worker.process.exitcode == 137
                                else f"Haiku worker exited unexpectedly ({worker.process.exitcode})"
                            ),
                            "code": "WORKER_RESOURCE_LIMIT"
                            if worker.process.exitcode == 137
                            else "WORKER_EXITED",
                            "status_code": 503,
                            "retryable": True,
                        }
                    ) from exc
                if message.get("type") == "callback":
                    try:
                        result = await self._callback(message, callbacks, guards)
                        response = {
                            "type": "callback_result",
                            "id": message["id"],
                            "result": result,
                        }
                    except BaseException as exc:
                        response = {
                            "type": "callback_result",
                            "id": message["id"],
                            "error": _error_payload(exc),
                        }
                    worker.connection.send(response)
                    continue
                if message.get("type") == "error":
                    raise _remote_error(message["error"])
                if message.get("type") == "result":
                    return message.get("result")
                raise RuntimeError("Invalid response from Haiku worker")
        finally:
            for manager in guards.values():
                with suppress(Exception):
                    await manager.__aexit__(None, None, None)

    async def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if not self.available:
            raise _remote_error(
                {
                    "message": "Haiku RAG is not installed in the active Python runtime",
                    "code": "HAIKU_UNAVAILABLE",
                    "status_code": 503,
                    "retryable": True,
                }
            )
        callbacks: dict[str, Any] = {}
        for name in _CALLBACK_NAMES:
            value = kwargs.pop(name, None)
            if value is not None:
                callbacks[name] = value
        request = {
            "type": "request",
            "operation": operation,
            "args": args,
            "kwargs": kwargs,
            "callbacks": sorted(callbacks),
        }
        if operation in _QUERY_OPERATIONS:
            async with self._query_lock:
                # A new request resets the worker's idle lifetime.
                if self._query_reaper is not None:
                    self._query_reaper.cancel()
                    self._query_reaper = None
                    self._query_expires_at = 0.0
                if self.unload_ollama_models and args:
                    self._query_ollama_targets.update(
                        _ollama_targets(Path(args[0]), operation, self.ollama_url)
                    )
                for attempt in range(2):
                    worker = self._query_worker
                    if worker is None or not worker.alive:
                        if worker is not None:
                            worker.terminate()
                        worker = self._spawn(self.query_limits, self.query_idle_seconds)
                        self._query_worker = worker
                    try:
                        worker.connection.send(request)
                        result = await self._receive(worker, callbacks)
                        self._schedule_query_reaper(worker)
                        return result
                    except asyncio.CancelledError:
                        worker.terminate()
                        self._query_worker = None
                        await self._unload_query_models()
                        raise
                    except (BrokenPipeError, ConnectionResetError, OmaRagError) as exc:
                        retryable_exit = (
                            not isinstance(exc, OmaRagError) or exc.code == "WORKER_EXITED"
                        )
                        worker.terminate()
                        self._query_worker = None
                        if attempt == 0 and retryable_exit:
                            continue
                        await self._unload_query_models()
                        raise
                raise RuntimeError("Unable to start Haiku query worker")
        if operation == "ingest":
            # Do not carry an idle reranker beside Docling's conversion peak.
            # ResourceCoordinator already prevents active chat/index overlap;
            # this additionally releases the query worker between phases.
            await self._stop_query_worker()
        limits = self.import_limits if operation == "ingest" else self.utility_limits
        worker = self._spawn(limits, 0)
        try:
            worker.connection.send(request)
            return await self._receive(worker, callbacks)
        except asyncio.CancelledError:
            worker.terminate()
            raise
        finally:
            await asyncio.to_thread(worker.process.join, 5)
            worker.terminate()

    def _schedule_query_reaper(self, worker: _WorkerHandle) -> None:
        if self._query_reaper is not None:
            self._query_reaper.cancel()
        lifetime = max(0.0, min(self.query_idle_seconds, self._residency_policy()))
        self._query_expires_at = time.monotonic() + lifetime
        self._query_reaper = asyncio.create_task(
            self._reap_query_worker(worker, lifetime), name="omarag-query-worker-reaper"
        )

    async def _reap_query_worker(self, worker: _WorkerHandle, lifetime: float) -> None:
        try:
            deadline = time.monotonic() + lifetime
            while True:
                now = time.monotonic()
                allowed = max(0.0, min(self.query_idle_seconds, self._residency_policy()))
                deadline = min(deadline, now + allowed)
                self._query_expires_at = deadline
                remaining = deadline - now
                if remaining <= 0:
                    break
                await asyncio.sleep(min(2.0, remaining))
            async with self._query_lock:
                if self._query_worker is worker:
                    worker.terminate()
                    self._query_worker = None
                    self._query_expires_at = 0.0
                    await self._unload_query_models()
        except asyncio.CancelledError:
            raise

    async def _stop_query_worker(self) -> None:
        reaper = self._query_reaper
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
        self._query_reaper = None
        self._query_expires_at = 0.0
        async with self._query_lock:
            if self._query_worker is not None:
                self._query_worker.terminate()
                self._query_worker = None
            await self._unload_query_models()

    async def _unload_query_models(self) -> None:
        targets = self._query_ollama_targets
        self._query_ollama_targets = set()
        if targets:
            await asyncio.to_thread(_unload_ollama_targets, targets)

    def _call_sync(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        worker = self._spawn(self.utility_limits, 0)
        try:
            worker.connection.send(
                {
                    "type": "request",
                    "operation": operation,
                    "args": args,
                    "kwargs": kwargs,
                    "callbacks": [],
                }
            )
            while True:
                try:
                    message = worker.connection.recv()
                except EOFError as exc:
                    worker.process.join(timeout=0.2)
                    raise _remote_error(
                        {
                            "message": f"Haiku utility worker exited ({worker.process.exitcode})",
                            "code": "WORKER_EXITED",
                            "status_code": 503,
                            "retryable": True,
                        }
                    ) from exc
                if message.get("type") == "error":
                    raise _remote_error(message["error"])
                if message.get("type") == "result":
                    return message.get("result")
        finally:
            worker.process.join(timeout=5)
            worker.terminate()

    async def shutdown(self) -> None:
        await self._stop_query_worker()

    async def ensure_database(self, database: Path) -> None:
        await self._call("ensure_database", database)

    async def warm(self, database: Path) -> None:
        await self._call("warm", database)

    async def citation_details(self, database: Path, citation: Citation) -> Citation:
        return await self._call("citation_details", database, citation)

    async def ingest(
        self,
        database: Path,
        source: str,
        *,
        parser_id: str = "auto",
        processing_profile: str = "default",
        segment_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        before_segment: Callable[[int, int, int], Awaitable[bool]] | None = None,
        generation_id: str | None = None,
        document_fingerprint: str | None = None,
        resume_segments: list[dict[str, Any]] | None = None,
        on_segment: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_phase: Callable[[str, int, int, int], Awaitable[None]] | None = None,
        segment_sizer: Callable[[int, bool], int] | None = None,
        metadata: BookMetadata | None = None,
        original_source: str | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "ingest",
            database,
            source,
            parser_id=parser_id,
            processing_profile=processing_profile,
            segment_guard=segment_guard,
            before_segment=before_segment,
            generation_id=generation_id,
            document_fingerprint=document_fingerprint,
            resume_segments=resume_segments,
            on_segment=on_segment,
            on_phase=on_phase,
            segment_sizer=segment_sizer,
            metadata=metadata,
            original_source=original_source,
        )

    async def delete_document(self, database: Path, document_id: str) -> bool:
        return bool(await self._call("delete_document", database, document_id))

    async def search(
        self,
        database: Path,
        query: str,
        limit: int,
        *,
        document_filter: str | None = None,
        search_type: str = "hybrid",
    ) -> list[SearchHit]:
        return await self._call(
            "search",
            database,
            query,
            limit,
            document_filter=document_filter,
            search_type=search_type,
        )

    async def ask(
        self,
        database: Path,
        question: str,
        images: list[str] | None = None,
        *,
        document_filter: str | None = None,
        evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    ) -> tuple[str, list[Citation]]:
        return await self._call(
            "ask",
            database,
            question,
            images,
            document_filter=document_filter,
            evidence_mode=evidence_mode,
        )

    async def analyze(
        self,
        database: Path,
        question: str,
        images: list[str] | None = None,
        *,
        document_filter: str | None = None,
        evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    ) -> tuple[str, list[Citation]]:
        return await self._call(
            "analyze",
            database,
            question,
            images,
            document_filter=document_filter,
            evidence_mode=evidence_mode,
        )

    async def update_document_metadata(
        self, database: Path, document_ids: list[str], metadata: dict[str, Any]
    ) -> None:
        await self._call("update_document_metadata", database, document_ids, metadata)

    def validate_config(self, content: str) -> None:
        self._call_sync("validate_config", content)
