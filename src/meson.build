# SPDX-License-Identifier: GPL-3.0-or-later
gnome.compile_resources('cassini-viewer',
  'cassini-viewer.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

conf = configuration_data()
conf.set('PYTHON', python.find_installation('python3').full_path())
conf.set('VERSION', meson.project_version())
conf.set('APPLICATION_ID', application_id)
conf.set('PROFILE', get_option('profile'))
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'cassini-viewer.in',
  output: 'cassini-viewer',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)

install_data(
  'cassini_viewer/__init__.py',
  'cassini_viewer/colors.py',
  'cassini_viewer/main.py',
  'cassini_viewer/player.py',
  'cassini_viewer/portable.py',
  'cassini_viewer/summary.py',
  'cassini_viewer/transcript.py',
  'cassini_viewer/window.py',
  install_dir: moduledir,
)

test('pytest',
  python.find_installation('python3', modules: ['pytest'], required: false),
  args: ['-m', 'pytest', '-q', meson.project_source_root() / 'tests'],
  env: ['PYTHONPATH=' + meson.current_source_dir()],
  workdir: meson.project_source_root(),
)
