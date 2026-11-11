# D82: ten slowest main CI test-carrying steps

Ten successful push runs on main, 2026-11-10 22:46 through 2026-11-11 04:04 UTC.
Duration is the step execution interval from GitHub job timestamps, excluding runner queue time.
Package and Source steps include dependency installation and build as well as their real test suites.

| Job / step | Median seconds | Range | Runs |
|---|---:|---:|---:|
| Debian package / Build .deb (ubuntu:26.04): Run build-aux/deb/build.sh "ubuntu:26.04" | 610 | 461–716 | 10 |
| RPM and Arch packages / Build .rpm (registry.opensuse.org/opensuse/tumbleweed:latest): Run build-aux/rpm/build.sh "registry.opensuse.org/opensuse/tumbleweed:latest" | 596 | 488–637 | 10 |
| Debian package / Build .deb (debian:testing): Run build-aux/deb/build.sh "debian:testing" | 583.5 | 466–607 | 10 |
| RPM and Arch packages / Build Arch package: Run build-aux/arch/build.sh | 525.5 | 388–540 | 10 |
| RPM and Arch packages / Build .rpm (fedora:44): Run build-aux/rpm/build.sh "fedora:44" | 520 | 416–846 | 10 |
| Source / Build and test on fedora:latest: Run build-aux/check-distros.sh "fedora:latest" | 409 | 293–447 | 10 |
| Boards / current-window: Render the real current window in private Xvfb | 255 | 234–453 | 10 |
| Recording / capture: Python tests with private data on the temporary-file filesystem | 254.5 | 229–547 | 10 |
| Source / Build and test on alpine:edge: Run build-aux/check-distros.sh "alpine:edge" | 244.5 | 203–258 | 10 |
| Recording / capture: Library concurrent browser capture and native hidden controls | 217.5 | 216–227 | 10 |

## Source evidence

- [main 870b2e6 run 42956864798](https://github.com/codemyriad/cassini-gnome/actions/runs/42956864798)
- [main 6c2d1a2 run 42953680492](https://github.com/codemyriad/cassini-gnome/actions/runs/42953680492)
- [main 2cc9309 run 42949196862](https://github.com/codemyriad/cassini-gnome/actions/runs/42949196862)
- [main e92d0a3 run 42944277350](https://github.com/codemyriad/cassini-gnome/actions/runs/42944277350)
- [main edcc2f2 run 42939395812](https://github.com/codemyriad/cassini-gnome/actions/runs/42939395812)
- [main 1a45e8b run 42934117848](https://github.com/codemyriad/cassini-gnome/actions/runs/42934117848)
- [main 16dd648 run 42917879940](https://github.com/codemyriad/cassini-gnome/actions/runs/42917879940)
- [main 40e386b run 42917861610](https://github.com/codemyriad/cassini-gnome/actions/runs/42917861610)
- [main a72acf0 run 42917842447](https://github.com/codemyriad/cassini-gnome/actions/runs/42917842447)
- [main 98b1555 run 42917600366](https://github.com/codemyriad/cassini-gnome/actions/runs/42917600366)

## Chosen change: track-limit fixture setup

Recording's full pytest step is the second slowest test-focused step, at a
254.5 s median (229–547 s, ten runs). Profiling found repeated creation of
62–63 ended tracks: every add/end durably republishes the growing manifest.
The setup now uses the existing `RecordingSession.change([])` transaction.
The real add/end APIs, all WAVs, and final durable checkpoint remain. The
operation under test, including the late 64th track, runs after setup commits.
The helper also checks committed ended-track metadata and every WAV header.

Three alternating before/after pairs, same seven node IDs, pinned Ubuntu 26.04
image, network disabled, 1 GiB container limit, task-owned disk scratch:

| Pair | Before seconds | After seconds |
|---|---:|---:|
| 1 | 54.37 | 7.87 |
| 2 | 38.26 | 9.96 |
| 3 | 43.71 | 9.63 |

Median: **43.71 s → 9.63 s (78% less)**. These are observations, not test pass
conditions. Do not extrapolate this host's saving to the whole CI step: durable
I/O cost differs between hosts. No samples, tracks, source-limit cases, failure
modes, audio assertions or intermediate publication checks are removed. Cases
that test per-track wire ACK durability are unchanged.

The complete pair JUnit files and logs are retained at
`/mnt/ai/cassini/dev/evidence/gnome-d82-main-steps-20261111/`.
