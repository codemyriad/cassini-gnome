# RPM spec for Fedora and openSUSE. Build it with build-aux/rpm/build.sh,
# which makes the source tarball from the working tree.
Name:           cassini-viewer
Version:        0.2.0
Release:        1%{?dist}
Summary:        Record meetings and read along with their transcripts
License:        GPL-3.0-or-later
URL:            https://github.com/codemyriad/cassini-gnome
Source0:        %{name}-%{version}.tar.xz
BuildArch:      noarch

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
%if 0%{?suse_version}
BuildRequires:  AppStream
BuildRequires:  glib2-tools
BuildRequires:  hicolor-icon-theme
BuildRequires:  typelib-1_0-Gst-1_0
BuildRequires:  gstreamer-plugins-base
BuildRequires:  gstreamer-plugins-good
%else
BuildRequires:  appstream
BuildRequires:  gobject-introspection
BuildRequires:  gstreamer1
BuildRequires:  gstreamer1-plugins-base
BuildRequires:  gstreamer1-plugins-good
%endif

Requires:       python3 >= 3.11
Requires:       python3-gobject
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

%prep
%autosetup -p1

%build
%meson
%meson_build

%install
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

%files -f %{name}.lang
%license COPYING
%doc README.md NEWS
%{_bindir}/%{name}
%{_bindir}/cassini-browser-host
%{_bindir}/cassini-browser-control-host
%{_datadir}/%{name}/*
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
