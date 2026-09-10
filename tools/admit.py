#!/usr/bin/env python3
"""Ask llama-manager's admission rule whether lilbee may load its models now.

The gap this closes is one-directional. llama-manager reads MemAvailable, which
on this APU is the only number that sees model weights at all — they live in the
GPU translation table, invisible to cgroup accounting. So when lilbee is already
holding memory, llama-manager notices and refuses. The other way round nobody
was checking: lilbee loads its embedder, reranker and vision model without
asking anyone, and on 2026-09-09 that met a chat model in pi and ended in an OOM.

This runs under llama-manager's own admission lock, so the answer cannot be
racing a load it is about to start, and applies llama-manager's own arithmetic
with llama-manager's own numbers:

    MemAvailable >= ram_reserve_gib + admission_margin_gib + required

It only reports. Nothing here starts, stops or evicts anything — the decision
what to do with a "no" belongs to the caller.

  admit.py --gib 8.5            → exit 0 go ahead · 3 not now · 0 no coordinator
  admit.py --gib 8.5 --wait 10  → seconds to wait for the lock (default 10)
"""

import argparse
import fcntl
import json
import os
import sys

GIB = 1024 ** 3
CONFIG = os.path.expanduser("~/.config/llama-manager/config.toml")


def read_meminfo_override():
    """OMARAG_ADMIT_AVAILABLE_GIB, so the test can state the memory it means.

    Only read when it is set, and nothing in OMA ever sets it.
    """
    raw = os.environ.get("OMARAG_ADMIT_AVAILABLE_GIB")
    return int(float(raw) * GIB) if raw else None


def standalone_config(reserve):
    """The shape llama-manager's config has, for a machine without it.

    The reserve is a fraction of RAM rather than a fixed number, because the
    same plugin runs on a 16 GiB laptop and a 64 GiB desktop; never below 2 GiB,
    under which a desktop session is in trouble anyway. The lock goes in the
    runtime directory, so it dies with the session instead of outliving a crash.
    """
    total = 0
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    keep = (max(2.0, total / GIB * 0.06) if str(reserve) == "auto"
            else float(reserve))
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return {
        "ram_reserve_gib": keep,
        "admission_margin_gib": 0.5,
        "admission_lock": os.path.join(runtime, "omarag-admission.lock"),
        "state_dir": "",
        "services": {},
    }


def mem_available():
    forced = read_meminfo_override()
    if forced is not None:
        return forced
    with open("/proc/meminfo") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if key == "MemAvailable":
                return int(rest.split()[0]) * 1024
    return 0


def holders(config):
    """What llama-manager currently has loaded, so a refusal can name it.

    The state file alone will not do: it keeps the last model a service was
    asked for long after the weights are gone — measured, chat.json naming
    ornith-bautechnik-32k while its unit was inactive. llama-manager decides
    the same question from the unit, so this does too, and only when there is
    a refusal to explain.
    """
    import subprocess
    state_dir = os.path.expanduser(str(config.get("state_dir", "")))
    out = []
    for name, service in sorted((config.get("services") or {}).items()):
        unit = str(service.get("backend_unit") or "")
        if not unit:
            continue
        try:
            done = subprocess.run(["systemctl", "--user", "is-active", unit],
                                  capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.stdout.strip() not in ("active", "activating"):
            continue
        model = service.get("model_id")
        try:
            with open(os.path.join(state_dir, name + ".json")) as handle:
                state = json.load(handle)
            model = state.get("active_model") or state.get("model") or model
        except (OSError, ValueError):
            pass
        out.append("%s (%s)" % (name, model) if model else name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gib", type=float, required=True,
                    help="what the caller is about to load")
    ap.add_argument("--wait", type=float, default=10.0,
                    help="seconds to wait for the admission lock")
    ap.add_argument("--config", default=CONFIG,
                    help="llama-manager's config; the default is where it lives")
    ap.add_argument("--reserve-gib", default="auto",
                    help="what to keep free when there is no coordinator to ask")
    args = ap.parse_args()

    try:
        import tomllib
        with open(args.config, "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, ImportError, ValueError):
        # No coordinator on this machine — the common case for anyone but its
        # author. The rule does not change, only where its numbers come from:
        # OMA's own reserve and OMA's own lock, so two shells, or this plugin
        # beside another copy of itself, still take turns.
        config = standalone_config(args.reserve_gib)

    lock_path = os.path.expanduser(str(config.get("admission_lock", "")))
    reserve = float(config.get("ram_reserve_gib", 0))
    margin = float(config.get("admission_margin_gib", 0))
    needed = (reserve + margin + args.gib) * GIB

    handle = None
    locked = False
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        handle = open(lock_path, "a+")
        # Non-blocking with our own deadline rather than a blocking flock: this
        # is called from the shell process's child, and a lock held through a
        # long model load must not turn into an unbounded wait.
        import time
        deadline = time.monotonic() + args.wait
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
    except OSError:
        handle = None

    available = mem_available()
    ok = available >= needed

    # A lock we could not take means llama-manager is loading something right
    # now. Whatever it is, it is not accounted for in MemAvailable yet, so the
    # honest answer is "not now" rather than a reading taken mid-load.
    if not locked and handle is not None:
        ok = False

    # Only on a refusal: naming what is in the way costs a handful of systemctl
    # calls, and the answer "go ahead" does not need them.
    where = "" if ok else ", ".join(holders(config))
    print("%s available=%.1f needed=%.1f reserve=%.1f margin=%.1f want=%.1f%s%s" % (
        "ok" if ok else "short",
        available / GIB, needed / GIB, reserve, margin, args.gib,
        "" if locked else " lock=busy",
        (" holding: " + where) if where else ""))

    if handle is not None:
        if locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
