# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared test 9 (D55): the protocol's tone-burst folders through GNOME's real consumers.

browser-protocol/conformance/placement is the one golden both apps read. Its
make.py writes three sessions (and, with a mutant name, the same sessions with
one offset moved); its placement.py is the oracle, the shared onset detector
and the checker, used here as they are, never re-implemented. Each leg runs
GNOME's production path and records where every burst lands:

- placement: cassini_talk.placement.frame on every placement.json case;
- rec16: the 16 kHz recognizer input of the jobs the write-down (TranscriptionQueue)
  hands its speech child (speech_pipeline.audio_chunks), placed on the session
  timeline by the diarization rejoin, which is where GNOME lays that input;
- wordMs: recognise_segment's word time for a segment that starts at the burst's
  local 16 kHz sample 16 t of that input, with a recognizer word at 0 s in it;
- mix48: the unpublished export mix before the limiter (export_audio.mixed_pcm
  of exporting.assumed_zero); mix48publication: the publication mixdown
  (mixdown.mixed of session_spans);
- playback: FolderPlayer's real GStreamer output, at the track's own rate, or
  "refused" for every track of a meeting FolderPlayer.can_play refuses (D19);
- offsetsAssumedZero: writingDown.offsetsAssumedZero as the write-down stored it
  in meeting.json, and recovery.offsetsAssumedZero of the exported file;
- opus48: the published Cassini file (finalize), decoded at 48 kHz.

GNOME measures every burst exactly where D55 puts it (delta 0 on every leg),
so beyond placement.py's tiers every leg must equal the oracle exactly. The
protocol's app mutants (bursts.json "mutants") are measured
the same way and must fail exactly the legs they name. The clean results go to
$CASSINI_PLACEMENT_RESULTS/gnome.json (else a temporary file), for
`placement.py compare` against Mac's.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "browser-protocol/conformance/placement"
A, B, C = (f"09000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oracle = _module("protocol_placement", GOLDEN / "placement.py")
make = _module("protocol_placement_make", GOLDEN / "make.py")
BURSTS = json.loads((GOLDEN / "bursts.json").read_text())
EXPECTED = oracle.expected(BURSTS)
TRACKS = {f"{s['id']}/{t['id']}": t for s in BURSTS["sessions"] for t in s["tracks"]}
MUTANTS = [mutant["name"] for mutant in BURSTS["mutants"]]
# Legs GNOME lands exactly on the oracle. opus48 too: libopus is deterministic for one
# build and input, and the decoded square wave reaches half its peak on the onset sample.
EXACT = ("rec16", "wordMs", "mix48", "mix48publication", "playback", "opus48")


def materialize(root, mutant=None):
    """make.py's folders (or a mutant's) as hidden Library sessions of `root`, the layout finalize publishes from."""
    from cassini_talk.library_v2 import journal

    root.mkdir(parents=True, exist_ok=True)
    layout = journal.Layout(root)
    layout.ensure()
    with contextlib.redirect_stdout(io.StringIO()):
        make.make(layout.sessions, mutant)
    return layout


def digest(folder):
    """The bytes make.py wrote for one session folder."""
    value = hashlib.sha256()
    for path in sorted(folder.iterdir()):
        value.update(path.name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return value.hexdigest()


def wait_for(predicate, seconds=60):
    """Dispatch the default main context until `predicate` holds; the timeout only bounds it (E1)."""
    context = GLib.MainContext.default()
    expired = []

    def expire():
        expired.append(True)
        return GLib.SOURCE_REMOVE

    timer = GLib.timeout_add_seconds(seconds, expire)
    try:
        while not predicate() and not expired:
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(timer)
    assert predicate(), "GStreamer did not reach the expected state"


def played(tracks, rate):
    """FolderPlayer's real mixed output for `tracks`, as mono S16 at `rate`."""
    from cassini_talk.folder_player import FolderPlayer

    sink = Gst.ElementFactory.make("appsink", None)
    sink.set_property(
        "caps", Gst.Caps.from_string(f"audio/x-raw,format=S16LE,layout=interleaved,rate={rate},channels=1")
    )
    sink.set_property("emit-signals", True)
    sink.set_property("sync", False)
    chunks, errors = [], []

    def received(src):
        buffer = src.emit("pull-sample").get_buffer()
        chunks.append(buffer.extract_dup(0, buffer.get_size()))
        return Gst.FlowReturn.OK

    sink.connect("new-sample", received)
    player = FolderPlayer(audio_sink=sink)
    player.connect("error", lambda _player, message: errors.append(message))
    try:
        player.load_tracks(tracks)
        wait_for(lambda: player._prerolled or errors)
        assert not errors
        player.play()
        wait_for(lambda: player._at_end or errors)
        assert not errors
        return np.frombuffer(b"".join(chunks), dtype="<i2")
    finally:
        player.shutdown()


def found(stream, rate, expected):
    """The shared detector at each expected position (placement.py onset)."""
    return [oracle.onset(stream, rate, at) for at in expected]


class OneWord:
    """A recognizer that hears one word at the very start of its segment."""

    def transcribe(self, samples, **_options):
        return dict(text="burst", language="en", words=[dict(word="burst", start=0.0, end=0.1)])


class Session:
    """Every leg of one golden session folder, measured through GNOME's production paths."""

    def __init__(self, layout, meeting, scratch):
        self.layout, self.meeting, self.scratch = layout, meeting, scratch
        self.keys = [f"{meeting.id}/{track.id}" for track in meeting.tracks]
        self.legs = {leg: {} for leg in ("rec16", "wordMs", "mix48", "mix48publication", "playback", "opus48")}
        self.flags = {}

    def writing_down(self):
        """The write-down's own jobs and the flag it stored, through TranscriptionQueue.

        The Library writes a recording down once it is published (the queue
        holds a hidden session's intent until then), from its session folder.
        """
        from cassini_talk.meeting_store import read_metadata
        from cassini_talk.transcription import TranscriptionQueue

        jobs, done = [], threading.Event()

        class Client:
            """The speech child's seat: it keeps the jobs the queue prepared and hears no words."""

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def close(self):
                pass

            def process_tracks(self, tracks, _notify):
                jobs.extend(tracks)
                return dict(segments=0, diagnostics=[])

        queue = TranscriptionQueue(
            lambda _directory: None,
            lambda _directory: done.set(),
            bundle=self.scratch / "bundle",
            root=self.layout.root,
            client_factory=Client,
        )
        try:
            assert queue.enqueue(self.meeting.directory)
            assert done.wait(60), "the write-down did not finish"
        finally:
            queue.close()
        metadata = read_metadata(self.meeting.directory)
        assert metadata["transcript"]["status"] == "done", metadata
        writing = metadata["recordingDetails"]["writingDown"]
        self.flags["transcript"] = writing.get("offsetsAssumedZero", False)
        return jobs

    def speech(self):
        """rec16 and wordMs from the write-down's jobs."""
        from cassini_talk.diarization_audio import rejoin
        from cassini_talk.speech_pipeline import audio_chunks, placed_offset, recognise_segment

        jobs = self.writing_down()
        live = [job for job in jobs if not job.get("silent")]
        assert len(live) == len(jobs) == len(self.keys), "the golden's tracks are audible (not D56-silent)"
        with rejoin(live, scratch=self.scratch / "rejoin") as (stream, _frames, _sha):
            session = np.frombuffer(stream.read(), dtype="<f4")
        for job in live:
            key = f"{self.meeting.id}/{job['captureTrackId']}"
            self.legs["rec16"][key] = found(session, 16000, EXPECTED[key]["rec16"]["frames"])
            local = np.concatenate(list(audio_chunks(job)))
            # Where the speech child places this job's words (an unknown offset at an assumed 0).
            offset = placed_offset(job, [])
            words = []
            # Pinned inputs: the segment starts at the burst's local sample, never at a measured onset.
            for start in (16 * ms for ms in TRACKS[key]["burstsMs"]):
                segment = dict(startSample=start, endSample=start + 4000, reason="silence")
                _value, (word,), _language, _sha = recognise_segment(
                    OneWord(), None, segment, offset, samples=local[start : start + 4000]
                )
                words.append(word["startMs"])
            self.legs["wordMs"][key] = words

    def mixes(self):
        from cassini_talk.export_audio import mixed_pcm
        from cassini_talk.exporting import assumed_zero
        from cassini_talk.library_v2 import mixdown

        export = np.concatenate(list(mixed_pcm(assumed_zero(self.meeting.tracks))))
        manifest = json.loads((self.meeting.directory / "session.json").read_text())
        timeline = mixdown.session_spans(self.meeting.directory, manifest)
        publication = np.concatenate(list(mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans))))
        for key in self.keys:
            expected = EXPECTED[key]["mix48"]["frames"]
            self.legs["mix48"][key] = found(export, 48000, expected)
            self.legs["mix48publication"][key] = found(publication, 48000, expected)

    def playback(self):
        from cassini_talk.folder_player import FolderPlayer

        meeting = self.meeting
        if not FolderPlayer.can_play(meeting):
            # D19: the Library never loads such a folder, so none of its tracks plays.
            player = FolderPlayer(audio_sink=Gst.ElementFactory.make("fakesink", None))
            try:
                with pytest.raises(ValueError, match="unknown start offset"):
                    player.load_tracks(meeting.tracks)
            finally:
                player.shutdown()
            self.legs["playback"].update(dict.fromkeys(self.keys, "refused"))
            return
        for rate in sorted({track.sample_rate for track in meeting.tracks}):
            output = played(meeting.tracks, rate)
            for key, track in zip(self.keys, meeting.tracks, strict=True):
                if track.sample_rate == rate:
                    offset = TRACKS[key]["startOffsetMs"] or 0
                    at = [oracle.frame(offset, rate) + ms * rate // 1000 for ms in TRACKS[key]["burstsMs"]]
                    self.legs["playback"][key] = found(output, rate, at)

    def exported(self):
        """The recovery flag the unpublished export writes (portable_writer)."""
        from cassini_talk import portable
        from cassini_talk.exporting import export_meeting

        path = export_meeting(self.meeting, self.scratch / "export")
        extension = portable.read_meeting(str(path)).manifest.get("x", {})
        self.flags["export"] = extension.get("gocassini.com", {}).get("recovery", {}).get("offsetsAssumedZero", False)

    def published(self):
        from cassini_talk.library_v2 import finalize
        from cassini_talk.track_source import OpusDecoder

        result = finalize.finalize(self.layout.root, self.meeting.id, zone="UTC")
        # track_source opens only confined tracks/ files.
        folder = self.scratch / "decode" / "tracks"
        folder.mkdir(parents=True)
        shutil.copyfile(self.layout.root / result.name, folder / "published.opus")
        decoder = OpusDecoder(folder / "published.opus")
        try:
            decoder.seek(0)
            data = decoder.read(decoder.info.samples)
            channels = decoder.channels
        finally:
            decoder.close()
        decoded = np.frombuffer(data, dtype="<i2").reshape(-1, channels).astype(np.float64).mean(axis=1)
        for key in self.keys:
            self.legs["opus48"][key] = found(decoded, 48000, EXPECTED[key]["opus48"]["frames"])

    def run(self):
        self.mixes()
        self.playback()
        self.exported()
        self.published()
        self.speech()
        return self


def measure(tmp, mutant=None, cache=None):
    """placement.py's results file for make.py's folders (or a mutant's), every leg measured.

    `cache` maps a session folder's bytes to its measured legs: a mutant moves
    one session, and its other folders are byte for byte the ones measured before.
    The folders and everything written from them are removed once measured: the
    suite's /tmp must keep finalize's free-space reserve.
    """
    try:
        return _measure(tmp, mutant, cache)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _measure(tmp, mutant, cache):
    from cassini_talk.library import LibraryScanner
    from cassini_talk.placement import frame

    layout = materialize(tmp / "library", mutant)
    if mutant is None:
        # The folders are the golden's own bytes on this platform too (materialized.sha256).
        for line in (GOLDEN / "materialized.sha256").read_text().splitlines():
            value, name = line.split("  ", 1)
            assert hashlib.sha256((layout.sessions / name).read_bytes()).hexdigest() == value, name
    cache = {} if cache is None else cache
    legs = {leg: {} for leg in ("rec16", "wordMs", "mix48", "mix48publication", "playback", "opus48")}
    flags = {}
    for meeting in sorted(LibraryScanner(layout.sessions).scan().meetings, key=lambda m: m.id):
        key = digest(meeting.directory)
        if key not in cache:
            cache[key] = Session(layout, meeting, tmp / "scratch" / meeting.id).run()
        session = cache[key]
        for leg, values in session.legs.items():
            legs[leg].update(values)
        flags[meeting.id] = dict(session.flags)
    literals = json.loads((GOLDEN / "placement.json").read_text())["cases"]
    legs["placement"] = [[c["ms"], c["rate"], frame(c["ms"], c["rate"])] for c in literals]
    legs["offsetsAssumedZero"] = flags
    return dict(
        vector=BURSTS["vector"],
        protocolCommit=(ROOT / "browser-protocol/PINNED_COMMIT").read_text().strip(),
        app="gnome",
        legs=legs,
    )


def test_placement_rounds_the_binary_double_half_away_from_zero_as_swift_does():
    import math

    from cassini_talk.placement import frame, word_ms

    # The largest double below 0.5 stays 0; floor(x + 0.5) rounds the sum up to 1.
    below = 0.49999999999999994
    assert below * 1000 / 1000 == below and math.floor(below + 0.5) == 1
    assert frame(below, 1000) == 0
    # Ties go away from zero (round() would give 2 and -2), on both signs.
    assert (frame(2.5, 1000), frame(-2.5, 1000), frame(-2.4, 1000)) == (3, -3, -2)
    # The arithmetic is the double's, in Swift's order: ms * rate, then / 1000.
    assert frame(1234.5104, 48000) == 59256 and frame(1234.5104 + 1 / 48, 48000) == 59257
    for bad in (math.inf, math.nan):
        with pytest.raises(ValueError):
            frame(bad, 16000)
    # A word's session ms floors the session sample: B.system's first burst is at 528000.
    assert word_ms(31999.96875, 16000) == 33000 and word_ms(31999.96875, 15999) == 32999


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """The clean results and each app mutant's, sharing the measurement of unchanged folders."""
    tmp = tmp_path_factory.mktemp("placement")
    cache = {}
    clean = measure(tmp / "golden", cache=cache)
    folders = len(cache)
    mutants = {name: measure(tmp / name, name, cache) for name in MUTANTS}
    # Each mutant moves one offset in one session: one more folder measured per mutant.
    assert len(cache) == folders + len(MUTANTS)
    return clean, mutants


def test_every_burst_lands_where_d55_says_in_every_gnome_consumer(measured, tmp_path):
    results, _mutants = measured
    target = os.environ.get("CASSINI_PLACEMENT_RESULTS")
    path = Path(target) / "gnome.json" if target else tmp_path / "gnome.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=1) + "\n")
    assert oracle.problems(oracle.strict(path.read_text())) == []
    legs = results["legs"]
    assert legs["offsetsAssumedZero"] == {
        A: dict(transcript=False, export=False),
        B: dict(transcript=False, export=False),
        C: dict(transcript=True, export=True),
    }
    # D19: a meeting with an unknown offset is refused whole, its known-offset microphone too.
    assert legs["playback"][f"{C}/microphone"] == legs["playback"][f"{C}/system"] == "refused"


def test_gnome_lands_every_burst_on_the_oracle_sample(measured):
    # GNOME's FIR, its 16 -> 48 kHz mix resamplers, folder playback at the track's rate
    # and the Opus round trip all keep a burst's onset on its sample, so beyond
    # placement.py's tiers (20 ms for a resampled 16 kHz track or Opus) every leg is exact.
    legs = measured[0]["legs"]
    for leg in EXACT:
        for key, entry in EXPECTED.items():
            want = entry["mix48" if leg == "mix48publication" else leg]
            if want == "refused":
                assert legs[leg][key] == "refused", (leg, key)
                continue
            assert legs[leg][key] == want.get("frames", want.get("values")), (leg, key)


@pytest.mark.parametrize("name", MUTANTS)
def test_each_app_mutant_fails_exactly_the_legs_it_names(measured, name):
    assert oracle.mutant_problems(name, measured[1][name]) == []
