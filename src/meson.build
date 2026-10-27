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
  'cassini_viewer/recorder.py',
  'cassini_viewer/recording_session.py',
  'cassini_viewer/browser_host.py',
  'cassini_viewer/session_window.py',
  'cassini_viewer/recording_window.py',
  'cassini_viewer/portable.py',
  'cassini_viewer/summary.py',
  'cassini_viewer/transcript.py',
  'cassini_viewer/window.py',
  install_dir: moduledir,
)

# pytest is optional: the Flatpak SDK, for one, does not ship it.
pytest = python.find_installation('python3', modules: ['pytest'], required: false)
if pytest.found()
  test('pytest',
    pytest,
    args: ['-m', 'pytest', '-q', meson.project_source_root() / 'tests'],
    env: ['PYTHONPATH=' + meson.current_source_dir()],
    workdir: meson.project_source_root(),
  )
endif

configure_file(
  input: 'cassini-browser-host.in',
  output: 'cassini-browser-host',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)
install_subdir('../browser-extension',
  install_dir: pkgdatadir,
  exclude_directories: ['tests', '__pycache__'],
  exclude_files: ['native_host.py'],
)
