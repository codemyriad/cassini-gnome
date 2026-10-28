# SPDX-License-Identifier: GPL-3.0-or-later
project('cassini-viewer',
          version: '0.2.0',
    meson_version: '>= 1.0.0',
  default_options: [ 'warning_level=2', 'werror=false', ],
)

fs = import('fs')
i18n = import('i18n')
gnome = import('gnome')
python = import('python')

base_id = 'com.gocassini.Viewer'
if get_option('profile') == 'development'
  application_id = base_id + '.Devel'
else
  application_id = base_id
endif

pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'cassini_viewer'

# Runtime requirements, checked by gi.require_version() at startup:
# GTK >= 4.18, libadwaita >= 1.8, GStreamer >= 1.20 with the Opus plugin.

subdir('data')
subdir('src')
subdir('po')
if get_option('whistle').enabled()
  subdir('whistle')
endif

# Distribution packages refresh these caches with triggers, so the tools are
# often absent from build environments; skip a cache whose tool is missing
# instead of failing a plain "meson install".
gnome.post_install(
     glib_compile_schemas: true,
    gtk_update_icon_cache: find_program('gtk4-update-icon-cache', 'gtk-update-icon-cache', required: false).found(),
  update_desktop_database: find_program('update-desktop-database', required: false).found(),
)
