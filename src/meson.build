# SPDX-License-Identifier: GPL-3.0-or-later
viewer_resources = gnome.compile_resources('cassini-viewer',
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
conf.set('bindir', get_option('prefix') / get_option('bindir'))

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
  'cassini_viewer/screenshot.py',
  'cassini_viewer/player.py',
  'cassini_viewer/recorder.py',
  'cassini_viewer/recording_session.py',
  'cassini_viewer/browser_host.py',
  'cassini_viewer/browser_control_host.py',
  'cassini_viewer/browser_service.py',
  'cassini_viewer/browser_window.py',
  'cassini_viewer/browser_notifications.py',
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
  exclude_files: ['native_host.py', 'control_host.py', 'install-host.py'],
)

# The portable contract gate needs only the Python standard library.
protocol_python = python.find_installation('python3')
test('browser-protocol-integrity', protocol_python,
  args: [meson.project_source_root() / 'browser-protocol/verify.py', '--pinned', meson.project_source_root() / 'browser-extension'],
)
protocol_tests = python.find_installation('python3', modules: ['jsonschema', 'rfc3339_validator'], required: false)
if protocol_tests.found()
test('browser-protocol-conformance', protocol_tests,
  args: [meson.project_source_root() / 'browser-protocol/conformance/run.py',
         meson.project_source_root() / 'tests/conformance_host.py'],
  env: ['CASSINI_CONFORMANCE_DIR=' + meson.current_build_dir() / 'conformance-recordings'],
  timeout: 120,
)
test('browser-control-conformance', protocol_tests,
  args: [meson.project_source_root() / 'browser-protocol/conformance/control/run.py',
         meson.project_source_root() / 'browser-extension/control_host.py'],
  timeout: 120,
)
endif

configure_file(
  input: '../browser-extension/install-host.py',
  output: 'install-host.py',
  configuration: conf,
  install: true,
  install_dir: pkgdatadir / 'browser-extension',
  install_mode: 'r-xr-xr-x',
)

# Audio and GUI integration is opt-in and always uses the private harness.
if get_option('recording_tests')
foreach scenario : ['browser', 'native', 'gtk']
  scenario_args = ['/usr/bin/python3', 'tests/recording/' + scenario + '.py']
  if scenario == 'browser'
    scenario_args = ['node', 'tests/recording/browser.mjs']
  elif scenario == 'gtk'
    scenario_args += ['--build', meson.project_build_root()]
  endif
  test('recording-' + scenario,
    find_program('../tests/recording/run.sh'),
    args: scenario_args,
    suite: 'recording',
    depends: viewer_resources,
    timeout: 300,
    workdir: meson.project_source_root(),
  )
endforeach
endif

configure_file(
  input: 'cassini-browser-control-host.in',
  output: 'cassini-browser-control-host',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)

if get_option('recording_tests')
  test('recording-app-control', find_program('../tests/recording/run.sh'),
    args: ['node', 'tests/recording/app-control.mjs'],
    env: ['CASSINI_TEST_BUILD=' + meson.project_build_root()],
    suite: 'recording', depends: viewer_resources,
    timeout: 300, workdir: meson.project_source_root(),
  )
endif
