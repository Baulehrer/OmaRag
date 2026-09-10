#!/usr/bin/env python3
"""Watch memory the way the kernel does, and say when OMA should give it back.

Two numbers, because they answer different questions.

MemAvailable is predictive: it says whether a load would fit. It is the only
figure that sees model weights on a shared-memory APU at all — they live in the
GPU translation table, where cgroup accounting cannot follow. Measured here:
9.19 GiB of weights, while the cgroup reported 0.68 GiB and a 3 GiB limit never
fired.

/proc/pressure/memory is reactive: it says whether anything is *stalling* on
memory right now. With swap configured that matters more than free bytes — the
machine does not get killed, it crawls, and MemAvailable can look calm while
everything thrashes. systemd-oomd watches the same signal and starts killing
cgroups at 50 % over 20 seconds; the point of this is to be finished giving
memory back long before that, so nothing has to be killed at all.

Prints one line whenever the verdict changes, plus a heartbeat, so the caller
can react without polling anything itself:

    state=ok available=23.4 total=62.0 some10=0.0 full10=0.0 reserve=4.0

  ok        room to work
  tight     give up what is idle
  critical  stop what is running as well

Nothing here loads, stops or kills anything. It only reports.
"""

import argparse
import os
import sys
import time

GIB = 1024 ** 3


def meminfo():
    values = {}
    with open("/proc/meminfo") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                values[key] = int(rest.split()[0]) * 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def pressure():
    """some/full avg10 from PSI, or (0, 0) on a kernel built without it."""
    try:
        with open("/proc/pressure/memory") as handle:
            out = {}
            for line in handle:
                kind, _, rest = line.partition(" ")
                for field in rest.split():
                    name, _, value = field.partition("=")
                    if name == "avg10":
                        out[kind] = float(value)
            return out.get("some", 0.0), out.get("full", 0.0)
    except (OSError, ValueError):
        return 0.0, 0.0


def auto_reserve(total):
    """What to keep free when nobody said.

    A fraction rather than a fixed number, because the same plugin runs on a
    16 GiB laptop and a 64 GiB desktop. Never less than 2 GiB: below that a
    desktop session is already in trouble.
    """
    return max(2.0 * GIB, total * 0.06)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reserve-gib", default="auto",
                    help="keep this much free; 'auto' is 6 %% of RAM, min 2 GiB")
    ap.add_argument("--interval", type=float, default=5.0)
    # Well under systemd-oomd's 50 % over 20 s, so OMA has finished releasing
    # before anything starts choosing a cgroup to kill.
    ap.add_argument("--ease", type=float, default=10.0,
                    help="some avg10 above this means: give up what is idle")
    ap.add_argument("--stand-down", type=float, default=25.0,
                    help="some avg10 above this means: stop running work too")
    ap.add_argument("--ease-seconds", type=float, default=5.0)
    ap.add_argument("--stand-down-seconds", type=float, default=10.0)
    ap.add_argument("--heartbeat", type=float, default=60.0)
    ap.add_argument("--once", action="store_true", help="one line, then exit")
    args = ap.parse_args()

    total, _ = meminfo()
    reserve = (auto_reserve(total) if args.reserve_gib == "auto"
               else float(args.reserve_gib) * GIB)

    state = "ok"
    over_ease = 0.0
    over_stand = 0.0
    last_said = 0.0

    while True:
        total, available = meminfo()
        some, full = pressure()
        now = time.monotonic()

        # Pressure has to persist: a single burst is a file copy, not a
        # shortage, and releasing a model for it would be worse than the burst.
        over_ease = over_ease + args.interval if some >= args.ease else 0.0
        over_stand = over_stand + args.interval if some >= args.stand_down else 0.0

        if over_stand >= args.stand_down_seconds or available < reserve / 2:
            verdict = "critical"
        elif over_ease >= args.ease_seconds or available < reserve:
            verdict = "tight"
        else:
            verdict = "ok"

        # Repeated while there is trouble, not only when the verdict turns:
        # the reader may have started listening after the change, and a state
        # it never heard about is a state it cannot act on.
        if (verdict != state or verdict != "ok"
                or (now - last_said) >= args.heartbeat or args.once):
            print("state=%s available=%.1f total=%.1f some10=%.1f full10=%.1f "
                  "reserve=%.1f" % (verdict, available / GIB, total / GIB,
                                    some, full, reserve / GIB), flush=True)
            last_said = now
        state = verdict
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
