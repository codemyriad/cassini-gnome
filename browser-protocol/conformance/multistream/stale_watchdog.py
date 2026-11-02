"""Independent PCM_STALE_AFTER oracle (SPEC.md "Audio transport liveness"); never production code.

`closed(frames, now)` says whether the app has closed the recording's audio
transport by monotonic time `now` (ms). Every audio-transport frame counts as
activity; control-connection frames do not. The watchdog is armed only while an
open remote span (declared by `track`, not yet ended by `trackEnd` or `stop`)
has delivered PCM (`audio`), and the deadline is `now - lastFrame >= 45000`.

`documentChanged` (D57) is the recording tab's page state and documentGeneration
as a control status reports them (pageState may be absent); like every control
frame it is not activity. navigating retires every open span and opens a
departing window: a span declared while it is open is retired at once (the old
document still runs until the next commits). The window closes on the next
present or on a new documentGeneration, never on the same one. A new
documentGeneration retires the spans declared up to the last frame that still
reported the old one; a span declared after that, even before its own
generation is reported, stays armed. A retired span never arms the watchdog.

A `track` may carry the documentGeneration the extension stamped (D61,
extension 0.2.12). The app keeps the set of generations it has seen leave:
navigating adds the current one and the generation of every open span, and a
change from a known generation adds the old one and the generation of every open
span other than the new one. A span with a generation is dormant exactly when its
generation is in that set, checked at every evaluation; the declaration-order
rules above apply only to spans without one, and a generation that is not 1 to
200 UTF-8 bytes counts as absent. A first generation still retires nothing, and
none ever leaves the set.
"""
PCM_STALE_AFTER = 45000
AUDIO_TRANSPORT = {'track', 'audio', 'sources', 'trackEnd', 'stop'}


def expired(now, last):
    return now - last >= PCM_STALE_AFTER


def valid(document):
    return isinstance(document, str) and 0 < len(document.encode()) <= 200


def closed(frames, now):
    open_spans, delivered, last = set(), set(), None
    # D57: declaration order, the current documentGeneration, how many spans
    # were declared when a frame last reported it, and the departing window.
    declared, generation, confirmed, departing = [], None, 0, False
    # D61: open spans that carry a documentGeneration, and the departed generations.
    documents, departed = {}, set()

    def dormant(id):
        return documents[id] in departed

    def armed():
        return bool(open_spans & delivered) or any(id in delivered and not dormant(id) for id in documents)

    for frame in frames:
        if frame['atMs'] > now:
            break
        if last is not None and armed() and expired(frame['atMs'], last):
            return True  # a frame at or after the deadline arrives too late
        if frame['frame'] == 'documentChanged':
            # A retired span leaves open_spans for good: its PCM never arms.
            page, document = frame.get('pageState'), frame.get('documentGeneration')
            changed = document is not None and document != generation
            if changed and generation is not None:
                open_spans.difference_update(declared[:confirmed])
                departed.add(generation)
                departed.update(g for g in documents.values() if g != document)
            if document is not None:
                generation, confirmed = document, len(declared)
            if page == 'navigating':
                if generation is not None:
                    departed.add(generation)
                departed.update(documents.values())  # every open span's document is gone
                open_spans.clear()
                departing = True
            elif page == 'present' or changed:
                departing = False  # the next document is here; the same one is not
            continue  # a control frame: never activity
        if frame['frame'] not in AUDIO_TRANSPORT:
            continue  # control frames (ping/pong/meetings/status/...) never count
        last, kind = frame['atMs'], frame['frame']
        if kind == 'track' and valid(frame.get('documentGeneration')):
            documents[frame['id']] = frame['documentGeneration']
        elif kind == 'track':
            declared.append(frame['id'])
            if not departing:
                open_spans.add(frame['id'])
        elif kind == 'audio' and frame['id'] in open_spans:
            delivered.add(frame['id'])
        elif kind == 'audio' and frame['id'] in documents:
            delivered.add(frame['id'])
        elif kind == 'trackEnd':
            open_spans.discard(frame['id'])
            documents.pop(frame['id'], None)
        elif kind == 'stop':
            open_spans.clear()
            documents.clear()
    return last is not None and armed() and expired(now, last)
