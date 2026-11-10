# D92: recognition thread headroom

Parakeet and the Whistle default use
`min(8, max(2, (os.cpu_count() or 1) - 2))`: total logical CPUs, not physical
cores. An unavailable CPU count uses the minimum. Whistle's explicit
`CASSINI_WHISTLE_THREADS` override remains 1–32. Silero stays at 1/1 threads;
Nemotron and CAM++ stay at 2. No model, decoder, audio or golden changes.

## Live recording policy

The same count applies when writing down a saved recording with a simultaneous
live capture. The existing queue owns one recognition job; it does not create
one pool per audio track. GTK and capture remain outside the inference child.
Before creating native pools, the child lowers its scheduling priority to nice
5, preserving an inherited lower priority. A denied priority change does not
reject valid transcription. Recognition need not restart when capture starts.

This reserves requested pool headroom rather than CPU affinity: helper threads
can still exist. On hosts with fewer than four CPUs the mandated minimum of two
cannot leave two CPUs unused; lower child priority lets interactive/capture
work run first. Explicit Whistle overrides are the user's choice.

## Measurement method

The benchmark uses the real Parakeet/Whistle clients and speech pipeline on a
910.173-second synthetic English WAV (six archived Kokoro voices, five repeats,
48 kHz mono PCM16). Input SHA256:
`bc47f97107dfc3a3e9b0562c8f17719c8101f1cbfd68581b5112a6c1dc7fbc08`.
Parakeet is the pinned v2 model at `50c15b94ea9cb1c2f8dbcdcbc0f15b450169154deeb206923cb5388f60892366`.

Cases run sequentially with warmed verified model bytes, one 2-thread case and
one policy case per engine. Both use nice 5 to isolate thread-count effects.
Timing starts after child ready and includes the real VAD/planning/resampling
and recognition pipeline, excluding startup/model verification. Times are
observations, not pass conditions or statistically controlled speedup claims.

Roy reports eight logical CPUs; the existing Ubuntu container sees twelve.
For the measured cases `PYTHON_CPU_COUNT=8` makes Python match the host, without
changing production code or adding a Parakeet override. Receipts assert native
Parakeet count 2/6 and Whistle child environment 2/6. The candidate Whistle case
has no `CASSINI_WHISTLE_THREADS` override. Earlier eight-thread diagnostics are
excluded.

A GTK spinner paints throughout recognition on the main thread. On the first
progress event, GLib starts the real NativeRecorder with synthetic test inputs;
the completion event stops it. Both tracks must contain frames, have no reported
gaps, finish and drain without timeout/errors. Thread nice values are read from
`/proc`, Silero's reported 1/1 settings are checked, and the parent must not
import the inference engines. This demonstrates headroom for the measured
synthetic capture and lightweight GTK workload; it is not a test of every audio
device or a full transcript UI under load.

The networkless, read-only existing test image has a 2200 MiB memory cap and no
swap. Measurements hold the shared heavy lock and check >4000 MiB available;
no Docker build or shared QA desktop is needed. Test barriers are completion
signals/futures, not sleeps or wall-time pass bounds. Frame intervals and elapsed
inference durations are diagnostic only.

Raw scripts, word/timing arrays, events and captures:
`/mnt/ai/cassini/dev/evidence/gnome-d92-six-20261110/` on roy. Run
`python3 .../locked-launch.py` there to reproduce with its existing pinned local
bundle/model mounts. Snapshots differ only in native thread count and a native
count receipt; production policy is unchanged by instrumentation. The worker
package bootstrap received a separate isolated-interpreter regression test.

## Results (2026-11-10, one pair per engine)

| Engine | 2 threads | 6 threads | Ratio | Words | Word/timing arrays |
| --- | ---: | ---: | ---: | ---: | --- |
| Parakeet | 52.5 s | 31.2 s | 1.69× | 2,312 | Identical |
| Whistle | 142.4 s | 86.7 s | 1.64× | 2,625 | Identical |

All four cases captured two tracks with nonzero frames, no gaps/errors and no
drain timeout. Every native worker thread observed at startup had nice >=5;
the parent stayed at nice 0. GTK paint thread CPU p90 was 0.82–1.17 ms; frame
interval p90 was 16.667 ms in each case (maximum 33.334 ms in Whistle's
six-thread case). These diagnostics have no wall-clock pass threshold.

Machine-readable receipts are in `evidence/d92-recognition-threads.json`.
59 targeted policy/Parakeet/Whistle tests passed, including actual native config
arguments, logical-count boundaries/fallback, Whistle override/rejection,
priority inheritance/permission failure, and isolated direct worker bootstrap.
Source CI Ruff check/format and diff checks passed. No golden files changed.
