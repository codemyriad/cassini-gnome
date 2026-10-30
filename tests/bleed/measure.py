#!/usr/bin/env python3
"""Measure a cassini.words.v1 transcript of the bleed fixture.

python3 measure.py TRANSCRIPT.json [--turns] [--echo RECORDING_FOLDER]

Ground truth is the script in source/tts.py: each source turn has a speaker ("me" or "them") and a time span,
found from the source WAVs (non-silent runs separated by >= 0.5 s). Every transcript word is assigned to the
source turn whose span contains its midpoint (a word in the bleed tail, up to DELAY + 0.2 s after the turn,
counts for that turn). Reports, per transcript speaker label:
  - words on the right speaker, words that echo the other side (bleed), and words outside any turn;
  - the number of displayed turns (consecutive words with the same speaker merged), versus 8 in the script;
  - "echo words": words labelled "me" during a turn spoken by "them", which is exactly what bleed produces.

--echo RECORDING_FOLDER reads that meeting's two tracks and adds:
  - lag and level of each "me" word that repeats a "them" word (same normalised text, -200..800 ms);
  - the D38.1 rule applied to the transcript (text match, 0-600 ms, mic 6 dB quieter, plus the
    "sandwich" rule), and what the D38.3 gate would see after the flagged words are skipped.
"""
import json, math, os, statistics, struct, sys, unicodedata, wave

HERE = os.path.dirname(os.path.abspath(__file__))
RATE, TAIL_MS = 16000, 500


def spans(name):
    with wave.open(os.path.join(HERE, "source", name)) as w:
        data = w.readframes(w.getnframes())
    s = struct.unpack(f"<{len(data) // 2}h", data)
    hop = RATE // 100  # 10 ms
    loud = [max(map(abs, s[i:i + hop])) > 300 for i in range(0, len(s), hop)]
    out, start, quiet = [], None, 0
    for i, on in enumerate(loud + [False] * 60):
        if on:
            start = i if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= 50:
                out.append((start * 10, (i - quiet + 1) * 10))
                start, quiet = None, 0
    return out


def samples(path):
    with wave.open(path) as w:
        data = w.readframes(w.getnframes())
    return struct.unpack(f"<{len(data) // 2}h", data)


def level(s, start_ms, end_ms):
    seg = s[max(0, int(start_ms * RATE / 1000)):int(end_ms * RATE / 1000)] or [0]
    return 20 * math.log10(max(1e-9, math.sqrt(sum(x * x for x in seg) / len(seg))) / 32768)


def norm(text):
    """D38.1: NFKC, casefold, punctuation stripped at both ends."""
    t = unicodedata.normalize("NFKC", text).casefold()
    while t and unicodedata.category(t[0]).startswith("P"):
        t = t[1:]
    while t and unicodedata.category(t[-1]).startswith("P"):
        t = t[:-1]
    return t


def d38_echo(items, mic, other):
    """Indexes of microphone words that D38.1 marks as echo. Rule 1, then the sandwich rule."""
    mine = [i for i, w in enumerate(items) if w["speaker"] == "me"]
    theirs = [w for w in items if w["speaker"] != "me"]
    echo = set()
    for i in mine:
        w = items[i]
        for o in theirs:
            if norm(o["text"]) == norm(w["text"]) and 0 <= w["startMs"] - o["startMs"] <= 600 \
                    and level(mic, w["startMs"], w["endMs"]) <= level(other, o["startMs"], o["endMs"]) - 6:
                echo.add(i)
                break
    sandwich = set()
    for k, i in enumerate(mine):
        if i in echo:
            continue
        before = next((j for j in reversed(mine[:k]) if j in echo), None)
        after = next((j for j in mine[k + 1:] if j in echo), None)
        if before is None or after is None or items[after]["startMs"] - items[before]["endMs"] >= 1000:
            continue
        w = items[i]
        if level(mic, w["startMs"], w["endMs"]) <= level(other, w["startMs"] - 600, w["endMs"]) - 6:
            sandwich.add(i)
    return echo, sandwich


def echo_report(items, folder, turn_of):
    mic = samples(os.path.join(folder, "track-microphone.wav"))
    other = samples(os.path.join(folder, "track-system.wav"))
    mine = [w for w in items if w["speaker"] == "me"]
    lags, levels, unmatched = [], [], 0
    for o in (w for w in items if w["speaker"] != "me"):
        c = [w for w in mine if norm(w["text"]) == norm(o["text"]) and -200 <= w["startMs"] - o["startMs"] <= 800]
        if not c:
            unmatched += 1
            continue
        w = min(c, key=lambda w: abs(w["startMs"] - o["startMs"] - 300))
        lags.append(w["startMs"] - o["startMs"])
        levels.append(level(mic, w["startMs"], w["endMs"]) - level(other, o["startMs"], o["endMs"]))
    if lags:
        print(f"  repeats by text: {len(lags)} (others' words without one: {unmatched}); "
              f"lag {min(lags)}..{max(lags)} ms, median {statistics.median(lags):.0f}; "
              f"level {min(levels):.1f}..{max(levels):.1f} dB, median {statistics.median(levels):.1f}")
    else:
        print(f"  repeats by text: none (others' words: {unmatched})")
    echo, sandwich = d38_echo(items, mic, other)
    flagged = echo | sandwich
    truth_echo = [i for i, w in enumerate(items) if w["speaker"] == "me" and turn_of(w) == "them"]
    own = [i for i, w in enumerate(items) if w["speaker"] == "me" and turn_of(w) == "me"]
    kept = [w for i, w in enumerate(items) if i not in flagged]
    turns = sum(1 for k, w in enumerate(kept) if k == 0 or kept[k - 1]["speaker"] != w["speaker"])
    left = [items[i]["text"] for i in truth_echo if i not in flagged]
    print(f"  D38.1: {len(echo)} flagged by text + {len(sandwich)} by sandwich; "
          f"echo words left {len(left)} of {len(truth_echo)}, own words flagged "
          f"{sum(1 for i in own if i in flagged)} of {len(own)}; displayed turns after {turns}")
    if left:
        print(f"    left: {' '.join(left)}")


def main():
    path = sys.argv[1]
    items = json.load(open(path))["items"]
    truth = sorted([(a, b, "me") for a, b in spans("me.wav")] + [(a, b, "them") for a, b in spans("them.wav")])
    def turn_of(word):
        mid = (word["startMs"] + word["endMs"]) / 2
        for a, b, who in truth:
            if a <= mid <= b + TAIL_MS:
                return who
        return None
    stats = {}
    for w in items:
        label = "me" if w["speaker"] == "me" else "them"
        who = turn_of(w)
        key = "right" if who == label else ("outside" if who is None else "echo")
        stats.setdefault(w["speaker"], {"right": 0, "echo": 0, "outside": 0})[key] += 1
    turns = []
    for w in items:
        if not turns or turns[-1][0] != w["speaker"]:
            turns.append([w["speaker"], w["startMs"], []])
        turns[-1][2].append(w["text"])
    print(f"{os.path.basename(path)}: {len(items)} words, "
          f"{len(turns)} displayed turns (script: {len(truth)} turns)")
    for speaker, s in stats.items():
        print(f"  {speaker:>8}: {s['right']} right, {s['echo']} echo, {s['outside']} outside")
    if "--turns" in sys.argv:
        for speaker, start, words in turns:
            print(f"  {start / 1000:6.2f}s {speaker:>8}: {' '.join(words)}")
    if "--echo" in sys.argv:
        echo_report(items, sys.argv[sys.argv.index("--echo") + 1], turn_of)


main()
