#!/usr/bin/env python3
"""Build the cross-track bleed fixture (D37, review #14).

python3 make.py [OUT]   (default: this folder). Standard library only, deterministic.

Reads source/me.wav (Daniel, "Me") and source/them.wav (Samantha and Karen, "The others"):
16 kHz mono PCM16, 44.0 s, the same length, turns strictly alternating, made by
source/tts.py with macOS `say`. Writes two ready-to-scan Library meetings:

  Recording-b1eed000-0000-4000-8000-000000000001  clean control
      track-microphone.wav = me
      track-system.wav     = them
  Recording-b1eed000-0000-4000-8000-000000000002  bleed
      track-microphone.wav = me + them delayed DELAY_MS, scaled by GAIN_DB
      track-system.wav     = them

The bleed models laptop speakers: the others come out of the speakers and reach the
microphone a little later and quieter. Nothing of "me" goes into the system track.
"""
import json, os, struct, sys, wave

HERE = os.path.dirname(os.path.abspath(__file__))
RATE = 16000
DELAY_MS = 300
GAIN_DB = -12.0
CASES = [
    ("b1eed000-0000-4000-8000-000000000001", "Bleed fixture: clean", False),
    ("b1eed000-0000-4000-8000-000000000002", "Bleed fixture: others in mic, 300 ms, -12 dB", True),
]


def read(name):
    with wave.open(os.path.join(HERE, "source", name)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE), name
        data = w.readframes(w.getnframes())
    return list(struct.unpack(f"<{len(data) // 2}h", data))


def write(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def write_meeting(out, uuid, title, created_at, mic, system):
    """Write one completed Library meeting with a microphone and a system track."""
    frames = len(mic)
    duration_ms = frames * 1000 // RATE
    folder = os.path.join(out, f"Recording-{uuid}")
    os.makedirs(folder, exist_ok=True)
    write(os.path.join(folder, "track-microphone.wav"), mic)
    write(os.path.join(folder, "track-system.wav"), system)
    track = {"sampleRate": RATE, "channels": 1, "frames": frames, "ended": True,
             "startOffsetMs": 0, "endOffsetMs": duration_ms}
    session = {
        "version": 1, "id": uuid, "source": "macos", "title": title,
        "createdAt": created_at,
        "state": "completed",
        "tracks": [
            {"id": "microphone", "file": "track-microphone.wav", "kind": "microphone",
             "label": "Your microphone", "labelSource": "device", **track},
            {"id": "system", "file": "track-system.wav", "kind": "system",
             "label": "The others", "labelSource": "device", **track},
        ],
    }
    with open(os.path.join(folder, "session.json"), "w") as f:
        json.dump(session, f, indent=2)
        f.write("\n")


def main():
    out = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else HERE
    me, them = read("me.wav"), read("them.wav")
    assert len(me) == len(them)
    frames = len(me)
    delay = RATE * DELAY_MS // 1000
    gain = 10 ** (GAIN_DB / 20)
    leaked = [0] * delay + them[:frames - delay]
    mixed = [round(a + gain * b) for a, b in zip(me, leaked)]
    assert max(map(abs, mixed)) < 32767, "mix clips; lower the source level"
    for uuid, title, bleed in CASES:
        created_at = "2026-10-30T09:00:00Z" if not bleed else "2026-10-30T09:05:00Z"
        write_meeting(out, uuid, title, created_at, mixed if bleed else me, them)
    print(f"{frames} frames, {frames / RATE:.1f} s; bleed delay {delay} frames, gain {gain:.4f}")


if __name__ == "__main__":
    main()
