# Convenience targets. The build itself is Meson; packages are built in
# containers (podman or docker) by the scripts in build-aux/.
#   make help

# Plain assignments, not ?=, so an exported PREFIX does not redirect `make run`;
# override them on the command line.
BUILDDIR = _build
PREFIX = $(CURDIR)/_install
PROFILE = development

.PHONY: help deps build test test-recording lint format run install uninstall pot \
	deb rpm rpm-opensuse arch flatpak flatpak-devel packages check-distros clean

help:
	@echo "Development:"
	@echo "  make deps            install build and runtime dependencies (sudo)"
	@echo "  make build           configure and build into $(BUILDDIR)"
	@echo "  make test            meson test (validators, conformance vectors, pytest)"
	@echo "  make test-recording  end-to-end recording tests (docker, xvfb; see tests/recording)"
	@echo "  make lint            browser-protocol integrity, ruff check and format check"
	@echo "  make run [FILE=x]    install into $(PREFIX) and run from there"
	@echo "  make pot             regenerate po/cassini-viewer.pot"
	@echo "Packages (written to dist/):"
	@echo "  make deb             .deb in an Ubuntu 26.04 container"
	@echo "  make rpm             .rpm in a Fedora container"
	@echo "  make rpm-opensuse    .rpm in an openSUSE Tumbleweed container"
	@echo "  make arch            .pkg.tar.zst in an Arch Linux container"
	@echo "  make flatpak         Flatpak bundle (flatpak-devel for the .Devel app)"
	@echo "  make packages        all of the above"
	@echo "  make check-distros   build and test on several distributions"
	@echo "System install:"
	@echo "  make build BUILDDIR=_build-system PREFIX=/usr/local PROFILE=default"
	@echo "  sudo meson install -C _build-system"

deps:
	build-aux/install-deps.sh --all

# Configure, or reconfigure when PREFIX or PROFILE changed since last time.
$(BUILDDIR)/build.ninja:
	meson setup $(BUILDDIR) --prefix="$(PREFIX)" -Dprofile=$(PROFILE)

build: $(BUILDDIR)/build.ninja
	meson configure $(BUILDDIR) --prefix="$(PREFIX)" -Dprofile=$(PROFILE)
	meson compile -C $(BUILDDIR)

test: build
	meson test -C $(BUILDDIR) --print-errorlogs

lint:
	python3 browser-protocol/verify.py --pinned browser-extension
	ruff check src tests
	ruff format --check src tests

format:
	ruff format src tests

install: build
	meson install -C $(BUILDDIR)

uninstall:
	ninja -C $(BUILDDIR) uninstall

run: install
	GSETTINGS_SCHEMA_DIR="$(PREFIX)/share/glib-2.0/schemas" \
	XDG_DATA_DIRS="$(PREFIX)/share:$${XDG_DATA_DIRS:-/usr/local/share:/usr/share}" \
		"$(PREFIX)/bin/cassini-viewer" $(FILE)

pot: $(BUILDDIR)/build.ninja
	meson compile -C $(BUILDDIR) cassini-viewer-pot

deb:
	build-aux/deb/build.sh

rpm:
	build-aux/rpm/build.sh

rpm-opensuse:
	build-aux/rpm/build.sh registry.opensuse.org/opensuse/tumbleweed:latest

arch:
	build-aux/arch/build.sh

flatpak:
	build-aux/flatpak/build.sh

flatpak-devel:
	build-aux/flatpak/build.sh --devel

packages: deb rpm rpm-opensuse arch flatpak

check-distros:
	build-aux/check-distros.sh

clean:
	rm -rf $(BUILDDIR) _build-system _install _flatpak .flatpak-builder repo

test-recording: build
	tests/recording/run.sh /usr/bin/python3 tests/recording/native.py
	tests/recording/run.sh node tests/recording/browser.mjs
	tests/recording/run.sh /usr/bin/python3 tests/recording/gtk.py --build $(BUILDDIR)
