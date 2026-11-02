"""Independent PCM_STALE_AFTER oracle (SPEC.md "Audio transport liveness"); never production code.

`closed(frames, now)` says whether the app has closed the recording's audio
transport by monotonic time `now` (ms). Every audio-transport frame counts as
activity; control-connection frames do not. The watchdog is armed only while an
open remote span (declared by `track`, not yet ended by `trackEnd` or `stop`)
has delivered PCM (`audio`), and the deadline is `now - lastFrame >= 45000`.
"""
PCM_STALE_AFTER = 45000
AUDIO_TRANSPORT = {'track', 'audio', 'sources', 'trackEnd', 'stop'}


def expired(now, last):
    return now - last >= PCM_STALE_AFTER


def closed(frames, now):
    open_spans, delivered, last = set(), set(), None

    def armed():
        return bool(open_spans & delivered)

    for frame in frames:
        if frame['atMs'] > now:
            break
        if frame['frame'] not in AUDIO_TRANSPORT:
            continue  # control frames (ping/pong/meetings/status/...) never count
        if last is not None and armed() and expired(frame['atMs'], last):
            return True  # a frame at or after the deadline arrives too late
        last, kind = frame['atMs'], frame['frame']
        if kind == 'track':
            open_spans.add(frame['id'])
        elif kind == 'audio' and frame['id'] in open_spans:
            delivered.add(frame['id'])
        elif kind == 'trackEnd':
            open_spans.discard(frame['id'])
        elif kind == 'stop':
            open_spans.clear()
    return last is not None and armed() and expired(now, last)
