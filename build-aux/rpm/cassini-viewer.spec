# RPM spec for Fedora and openSUSE. Build it with build-aux/rpm/build.sh,
# which makes the source tarball from the working tree and puts Source1-5
# (from .whistle-cache) next to it.
Name:           cassini-viewer
Version:        0.2.0
Release:        1%{?dist}
Summary:        Record meetings and read along with their transcripts
License:        GPL-3.0-or-later AND Apache-2.0 AND MIT
URL:            https://github.com/codemyriad/cassini-gnome
Source0:        %{name}-%{version}.tar.xz
# The offline writing-down bundle, pinned with its sha256 in whistle/pins.json
# (keep these URLs equal to it; tests/test_packaging_pins.py checks). The
# build never downloads: meson runs whistle/fetch.py --offline, which checks
# every sha256 and stops on a mismatch.
# Whistle weights (Apache-2.0)
Source1:        https://huggingface.co/Cactus-Compute/whistle/resolve/ca5287601bef25af26dcf2e1b2bdc0843a7c19e5/whistle.cact
# Needle 3.3.0 engine wheel, for libneedle3.so (Apache-2.0)
Source2:        https://huggingface.co/Cactus-Compute/needle3/resolve/93e88ede867e4311c39151285e74df513e4b835b/python/cactus_needle-3.3.0-py3-none-manylinux2014_x86_64.whl
# cactus-needle 3.2.0 Python wrapper (Apache-2.0)
Source3:        https://files.pythonhosted.org/packages/e7/13/5f3a1c20b6643e83cd3f55c866217a0d539c343aaaf7de5253ce08ac16bf/cactus_needle-3.2.0-py3-none-any.whl
# Silero VAD (MIT)
Source4:        https://raw.githubusercontent.com/snakers4/silero-vad/7e30209a3e901f9842f81b225f3e93d8199902b1/src/silero_vad/data/silero_vad.onnx
# ONNX Runtime 1.31.0 (MIT), the PyPI wheel for this distribution's Python,
# installed unpacked in %%{_libdir}/%%{name}/python instead of depending on
# the distribution's onnxruntime (whose sympy dependency can pull in
# hundreds of MB, TeX Live included, as weak dependencies). Only the
# recognition child loads it.
%if "%{?python3_version_nodots}" == "311"
Source5:        https://files.pythonhosted.org/packages/a6/72/6c57163b63b5343853d7f0619c4f424a6e53ee762d7263667ff004bfede1/onnxruntime-1.31.0-cp311-cp311-manylinux_2_28_x86_64.whl
%endif
%if "%{?python3_version_nodots}" == "312"
Source5:        https://files.pythonhosted.org/packages/6c/44/1e9e762b95b7da0a8424913a1ed7c38cdaf88624a3c41ddba24ebac88bc9/onnxruntime-1.31.0-cp312-cp312-manylinux_2_28_x86_64.whl
%endif
%if "%{?python3_version_nodots}" == "313"
Source5:        https://files.pythonhosted.org/packages/0d/ac/67ebbaab4b3083f2a6b27ee6c4aa400c7f8d6c72b5499aac7e4cd6ba74f5/onnxruntime-1.31.0-cp313-cp313-manylinux_2_28_x86_64.whl
%endif
%if "%{?python3_version_nodots}" == "314"
Source5:        https://files.pythonhosted.org/packages/30/2e/5c6ec7e26a097e97ee70f2dee68b8ca4d9d26701f2f33c3f8ab585cb89fe/onnxruntime-1.31.0-cp314-cp314-manylinux_2_28_x86_64.whl
%endif
# The engine is a pinned glibc x86_64 binary.
ExclusiveArch:  x86_64

# The app checks every file of the bundle against its pinned sha256, and of
# the private ONNX Runtime against its wheel's RECORD, before it loads
# anything, so nothing in them may be stripped or split into debuginfo; the
# rest of the package is Python.
%global debug_package %{nil}
%global __brp_strip %{nil}
%global __brp_strip_comment_note %{nil}
%global __brp_strip_static_archive %{nil}
%global __brp_strip_lto %{nil}
# libneedle3.so has the SONAME libneedle.so, and ONNX Runtime ships
# libonnxruntime_providers_shared.so; both are private to the app.
%global __provides_exclude_from ^(%{_datadir}/%{name}/whistle|%{_libdir}/%{name})/.*$
# The private dist-info is outside site-packages, so the Python dependency
# generator skips it anyway; never let it read the METADATA, which lists
# packages the app does not use (protobuf, flatbuffers, sympy extras).
%global __requires_exclude_from ^%{_libdir}/%{name}/python/onnxruntime-[^/]*\\.dist-info/.*$

BuildRequires:  meson >= 1.0
BuildRequires:  python3 >= 3.11
BuildRequires:  desktop-file-utils
BuildRequires:  gettext
BuildRequires:  python3-pytest
# The browser-protocol conformance runners used by the test suite
BuildRequires:  python3-jsonschema
BuildRequires:  python3-rfc3339-validator
# glib-compile-resources and glib-compile-schemas
BuildRequires:  glib2-devel
# The player, recorder and library monitor tests need PyGObject and the
# GStreamer elements; without them %%check skips those tests silently.
BuildRequires:  python3-gobject
# The CPU-count bridge of the writing-down bundle (whistle/cpu_limit.c)
BuildRequires:  gcc
# The speech pipeline tests (tests/test_speech_pipeline.py)
BuildRequires:  python3dist(numpy)
%if 0%{?suse_version}
# %%python3_version, which picks Source5 and the Python ABI
BuildRequires:  python-rpm-macros
BuildRequires:  AppStream
BuildRequires:  glib2-tools
BuildRequires:  hicolor-icon-theme
BuildRequires:  typelib-1_0-Gst-1_0
BuildRequires:  gstreamer-plugins-base
BuildRequires:  gstreamer-plugins-good
%else
BuildRequires:  python3-rpm-macros
BuildRequires:  appstream
BuildRequires:  gobject-introspection
BuildRequires:  gstreamer1
BuildRequires:  gstreamer1-plugins-base
BuildRequires:  gstreamer1-plugins-good
%endif

Requires:       python3 >= 3.11
# The private ONNX Runtime is built for this Python's ABI.
Requires:       python(abi) = %{python3_version}
Requires:       python3-gobject
# Writing meetings down offline. Both Fedora and openSUSE provide this name;
# on Tumbleweed only the primary Python flavour does, which is what we want.
Requires:       python3dist(numpy)
%if 0%{?suse_version}
# hicolor-icon-theme comes with GTK; openSUSE's rpmlint rejects requiring it.
Requires:       typelib-1_0-Gtk-4_0 >= 4.18
Requires:       typelib-1_0-Adw-1 >= 1.8
Requires:       typelib-1_0-Gst-1_0
Requires:       gstreamer-plugins-base
Requires:       gstreamer-plugins-good
Recommends:     gstreamer-plugin-pipewire
%else
Requires:       hicolor-icon-theme
Requires:       gtk4 >= 4.18
Requires:       libadwaita >= 1.8
# GTK's typelib needs cairo-1.0.typelib, which only this package ships;
# without it Gtk fails to import on a minimal install.
Requires:       gobject-introspection
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Recommends:     pipewire-gstreamer
%endif

%description
Cassini plays meeting recordings in the Cassini portable meeting format:
ordinary Ogg Opus files that carry their own word-by-word transcript,
speaker names and summary.

Cassini also records meetings: the microphone and desktop audio through a
PulseAudio-compatible sound server such as PipeWire, and, with its companion
extension for Chromium-based browsers, each audio source of a WebRTC meeting
on its own track. The extension's native messaging hosts are included; they
are registered per user, never system-wide.

It highlights the spoken word as the recording plays, lets you click any
word to play from there, see participants, search the transcript, read the
summary, verify that the audio matches its transcript, and export the
transcript as text or WebVTT subtitles.

It writes recorded meetings down on this computer, without a network
connection: the package includes the Whistle speech recognition weights and
Needle engine (Apache-2.0), the Silero voice activity detector (MIT) and,
for the recognition process only, ONNX Runtime (MIT), and runs them with
NumPy on the CPU. Nothing is downloaded and no telemetry is sent.

%prep
%autosetup -p1
mkdir -p .whistle-cache
cp -p %{SOURCE1} .whistle-cache/whistle.cact
cp -p %{SOURCE2} .whistle-cache/engine.whl
cp -p %{SOURCE3} .whistle-cache/wrapper.whl
cp -p %{SOURCE4} .whistle-cache/silero_vad.onnx
%{!?SOURCE5:echo "No pinned ONNX Runtime wheel for Python %{python3_version}; see whistle/pins.json" >&2; exit 1}
cp -p %{SOURCE5} .whistle-cache/

%build
%meson -Dwhistle=enabled -Dwhistle_cache=.whistle-cache
%meson_build

%install
# openSUSE strips in its own brp scripts; see debug_package above.
export NO_BRP_STRIP_DEBUG=true
%meson_install
# Meson installs the launchers 0555; packaging policy wants 0755.
chmod 0755 %{buildroot}%{_bindir}/%{name} %{buildroot}%{_bindir}/cassini-browser-host \
  %{buildroot}%{_bindir}/cassini-browser-control-host \
  %{buildroot}%{_datadir}/%{name}/browser-extension/install-host.py
# po/LINGUAS may list no languages yet; an empty list is fine.
%find_lang %{name} || : > %{name}.lang
# rpm rejects an empty -f list, so the package's data directory goes in it.
echo "%%dir %{_datadir}/%{name}" >> %{name}.lang

%check
%meson_test
# Catches anything that changed the installed bundle, such as stripping.
python3 whistle/verify.py %{buildroot}%{_datadir}/%{name}/whistle
python3 whistle/verify.py --onnxruntime %{buildroot}%{_libdir}/%{name}/python

%files -f %{name}.lang
%license COPYING
%license whistle/NOTICE.md whistle/LICENSE-Apache-2.0.txt whistle/LICENSE-Silero-MIT.txt
%license whistle/LICENSE-ONNXRuntime-MIT.txt
%doc README.md NEWS
%{_bindir}/%{name}
%{_bindir}/cassini-browser-host
%{_bindir}/cassini-browser-control-host
%{_datadir}/%{name}/*
%{_libdir}/%{name}
%{_datadir}/applications/com.gocassini.Viewer.desktop
%{_datadir}/metainfo/com.gocassini.Viewer.metainfo.xml
%{_datadir}/glib-2.0/schemas/com.gocassini.Viewer.gschema.xml
%{_datadir}/icons/hicolor/scalable/apps/com.gocassini.Viewer.svg
%{_datadir}/icons/hicolor/symbolic/apps/com.gocassini.Viewer-symbolic.svg
%dir %{_datadir}/dbus-1
%dir %{_datadir}/dbus-1/services
%{_datadir}/dbus-1/services/com.gocassini.Viewer.service
%{_mandir}/man1/%{name}.1*

%changelog
* Tue Oct 27 2026 Silvio Tomatis <silviot@gmail.com> - 0.2.0-1
- Record meetings: microphone and desktop audio, and browser meetings
  per source through the companion extension and its native hosts.
- Name missing libraries and GStreamer plugins at startup instead of failing.
- Package for Fedora, openSUSE, Arch Linux and Alpine as well as Debian.

* Thu Oct 15 2026 Silvio Tomatis <silviot@gmail.com> - 0.1.1-1
- Fix transcript-click crashes and GTK label sizing warnings.
- Remove participant hiding and port inline interjection grouping.
- Preserve interjections in plain-text exports.

* Thu Oct 15 2026 Silvio Tomatis <silviot@gmail.com> - 0.1.0-1
- First release.
