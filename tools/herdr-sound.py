#!/usr/bin/env python3
"""Lift herdr's notification sounds out of the installed herdr binary.

The user asked for herdr's sound in OMA. herdr ships it compiled in — there is
no file to point at — so this reads the binary that is already on the machine
and writes the sounds as WAV into OMA's own state directory. Nothing is
downloaded, nothing is installed, and the repository stays free of somebody
else's asset: the sound only exists on a machine that already has herdr.

WAV rather than the original MP3 because OMA plays sounds through
canberra-gtk-play / pw-play / paplay, none of which decode MP3.

Usage:  herdr-sound.py [--list] [--out DIR] [--binary PATH]
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import wave

BITRATES_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
BITRATES_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def frame_length(data, i):
    """Length of the MPEG audio frame at i, or 0 if there is no valid frame."""
    if i + 4 > len(data):
        return 0
    if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
        return 0
    version = (data[i + 1] >> 3) & 3
    layer = (data[i + 1] >> 1) & 3
    if layer != 1 or version == 1:          # Layer III only, no reserved version
        return 0
    bitrate_index = (data[i + 2] >> 4) & 0xF
    rate_index = (data[i + 2] >> 2) & 3
    if bitrate_index in (0, 15) or rate_index == 3:
        return 0
    bitrate = (BITRATES_V1 if version == 3 else BITRATES_V2)[bitrate_index] * 1000
    rate = RATES[version][rate_index]
    samples = 1152 if version == 3 else 576
    return int(samples / 8 * bitrate / rate) + ((data[i + 2] >> 1) & 1)


def longest_mpeg_run(data):
    """The longest chain of back-to-back MPEG frames in the binary.

    Found by walking rather than by a fixed offset, so a herdr update moves the
    sound without breaking this. Audio is the only place in a binary where
    hundreds of valid frame headers follow each other end to end.
    """
    best = (0, 0)
    i = 0
    limit = len(data)
    while i < limit:
        if data[i] != 0xFF:
            i += 1
            continue
        length = frame_length(data, i)
        if not length:
            i += 1
            continue
        start, count, p = i, 0, i
        while True:
            step = frame_length(data, p)
            if not step:
                break
            p += step
            count += 1
        if count > 8 and p - start > best[1] - best[0]:
            best = (start, p)
        i = max(i + 1, p) if count > 8 else i + 1
    return best


def decode(mp3):
    """MP3 bytes to 44.1 kHz stereo signed 16-bit PCM."""
    for cmd in (["mpg123", "-q", "-s", "--stereo", "-r", "44100", "-"],
                ["ffmpeg", "-v", "quiet", "-i", "pipe:0",
                 "-f", "s16le", "-ac", "2", "-ar", "44100", "pipe:1"]):
        if not shutil.which(cmd[0]):
            continue
        done = subprocess.run(cmd, input=mp3, stdout=subprocess.PIPE)
        if done.returncode == 0 and len(done.stdout) > 4096:
            return done.stdout
    sys.exit("no MP3 decoder found — install mpg123 or ffmpeg")


def split(pcm, rate=44100, floor=-45.0, gap=0.15):
    """Cut the decoded stream into the separate sounds and trim their silence.

    herdr's sounds sit in the binary as one run of frames; a gap of near-silence
    is the only thing that separates them.
    """
    frames = len(pcm) // 4
    window = rate // 100                                    # 10 ms
    threshold = (10 ** (floor / 20)) * 32768
    loud = []
    for w in range(0, frames, window):
        chunk = pcm[w * 4:(w + window) * 4]
        if not chunk:
            break
        samples = struct.unpack("<%dh" % (len(chunk) // 2), chunk)
        peak = max(abs(s) for s in samples)
        loud.append(peak > threshold)

    parts, run_start, quiet = [], None, 0
    quiet_limit = int(gap * 100)
    for w, is_loud in enumerate(loud + [False] * (quiet_limit + 1)):
        if is_loud:
            if run_start is None:
                run_start = w
            quiet = 0
        elif run_start is not None:
            quiet += 1
            if quiet > quiet_limit:
                a = max(0, run_start - 1) * window
                b = min(frames, (w - quiet + 3) * window)
                parts.append(pcm[a * 4:b * 4])
                run_start, quiet = None, 0
    return parts


def write_wav(path, pcm, rate=44100):
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=shutil.which("herdr") or "/usr/bin/herdr")
    ap.add_argument("--out", default=os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "omarchy", "omarag", "sounds"))
    ap.add_argument("--list", action="store_true",
                    help="report what was found without writing anything")
    args = ap.parse_args()

    if not os.path.isfile(args.binary):
        sys.exit("herdr not found at %s — nothing to lift" % args.binary)

    with open(args.binary, "rb") as fh:
        data = fh.read()
    start, end = longest_mpeg_run(data)
    if end - start < 4096:
        sys.exit("no embedded audio in %s" % args.binary)

    pcm = decode(data[start:end])
    parts = split(pcm)
    if not parts:
        sys.exit("audio decoded but no sound stood out of the silence")

    # Declaration order in the binary. herdr's own config names its two
    # overrides done_path and request_path in that order, and its CLI takes
    # --sound done|request — so the first sound is read as the finished one.
    # Inferred from order, not from a label in the binary.
    names = ["done", "request"] + ["extra-%d" % n for n in range(1, 9)]
    total = len(pcm) // 4 / 44100.0
    print("%s: %d bytes of MPEG at %d, %.2f s, %d sound(s)"
          % (os.path.basename(args.binary), end - start, start, total, len(parts)))

    written = []
    if not args.list:
        os.makedirs(args.out, exist_ok=True)
    for n, part in enumerate(parts):
        name = names[n] if n < len(names) else "sound-%d" % n
        seconds = len(part) // 4 / 44100.0
        path = os.path.join(args.out, "herdr-%s.wav" % name)
        if args.list:
            print("  %-8s %.2f s" % (name, seconds))
            continue
        write_wav(path, part)
        written.append(path)
        print("  %-8s %.2f s  %s" % (name, seconds, path))
    if written:
        print(written[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
