# SPDX-License-Identifier: GPL-3.0-or-later
viewer_resources = gnome.compile_resources('cassini-viewer',
  'cassini-viewer.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

# The launcher's shebang points at this interpreter, so it must be the one
# that has PyGObject: a python3 from a virtualenv usually does not. Missing gi
# is only a warning because packagers often build without runtime deps.
py = python.find_installation('python3')
if not py.language_version().version_compare('>= 3.11')
  warning('Python @0@ found; cassini-viewer needs Python 3.11 or newer at run time.'.format(py.language_version()))
endif
if not python.find_installation('python3', modules: ['gi'], required: false).found()
  warning('The python3 at @0@ cannot import gi (PyGObject), and the launcher will run with it. Install PyGObject for it, or point Meson at the system Python with a native file whose [binaries] section sets python = \'/usr/bin/python3\'.'.format(py.full_path()))
endif

# On merged-/usr systems PATH may list /usr/sbin first (Arch, Fedora), which
# would give the launcher a #!/usr/sbin/python3 shebang. Prefer /usr/bin when
# it is the same file.
python_path = py.full_path()
foreach sbin : ['/usr/sbin/', '/sbin/']
  if python_path.startswith(sbin)
    candidate = python_path.replace(sbin, '/usr/bin/')
    if fs.is_samepath(candidate, python_path)
      python_path = candidate
    endif
  endif
endforeach

conf = configuration_data()
conf.set('PYTHON', python_path)
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
  'cassini_viewer/library_window.py',
  'cassini_viewer/meeting_trash.py',
  'cassini_viewer/trash_cleanup.py',
  'cassini_viewer/model_manifest.py',
  'cassini_viewer/model_download.py',
  'cassini_viewer/zstd_stream.py',
  'cassini_viewer/search_index.py',
  'cassini_viewer/search_page.py',
  'cassini_viewer/meeting_page.py',
  'cassini_viewer/speaker_merges.py',
  'cassini_viewer/speaker_edits.py',
  'cassini_viewer/speakers_dialog.py',
  'cassini_viewer/meeting_text.py',
  'cassini_viewer/meeting_store.py',
  'cassini_viewer/meeting_rename.py',
  'cassini_viewer/rename_dialog.py',
  'cassini_viewer/transcription.py',
  'cassini_viewer/transcript_publish.py',
  'cassini_viewer/speech_models.py',
  'cassini_viewer/models.json',
  'cassini_viewer/voices_logic.py',
  'cassini_viewer/campplus_model.py',
  'cassini_viewer/campplus.py',
  'cassini_viewer/voice_models.py',
  'cassini_viewer/voice_backfill.py',
  'cassini_viewer/browser_connection.py',
  'cassini_viewer/browser_status.py',
  'cassini_viewer/voice_store.py',
  'cassini_viewer/voice_board.py',
  'cassini_viewer/saved_voices_page.py',
  'cassini_viewer/worker_voices.py',
  'cassini_viewer/diarization.py',
  'cassini_viewer/diarization_abi.py',
  'cassini_viewer/diarization_audio.py',
  'cassini_viewer/diarization_models.py',
  'cassini_viewer/worker_diarization.py',
  'cassini_viewer/nemotron.py',
  'cassini_viewer/nemotron_model.py',
  'cassini_viewer/parakeet.py',
  'cassini_viewer/parakeet_abi.py',
  'cassini_viewer/parakeet_model.py',
  'cassini_viewer/parakeet_client.py',
  'cassini_viewer/parakeet_worker.py',
  'cassini_viewer/sherpa_runtime.py',
  'cassini_viewer/sherpa-pins.json',
  'cassini_viewer/whistle_client.py',
  'cassini_viewer/whistle_worker.py',
  'cassini_viewer/speech_pipeline.py',
  'cassini_viewer/opus_input.py',
  'cassini_viewer/fir-48k-16k.json',
  'cassini_viewer/inline_line.py',
  'cassini_viewer/open_meeting.py',
  'cassini_viewer/export_model.py',
  'cassini_viewer/export_audio.py',
  'cassini_viewer/portable_context.py',
  'cassini_viewer/portable_writer.py',
  'cassini_viewer/exporting.py',
  'cassini_viewer/export_dialog.py',
  'cassini_viewer/received.py',

  'cassini_viewer/capture_guard.py',
  'cassini_viewer/background_recording.py',
  'cassini_viewer/capture_sources.py',
  'cassini_viewer/session_events.py',
  'cassini_viewer/what_is_recorded.py',
  'cassini_viewer/strings.py',
  'cassini_viewer/strings.json',
  'cassini_viewer/library.py',
  'cassini_viewer/library_files.py',
  'cassini_viewer/library_schema.py',
  'cassini_viewer/meeting.schema.json',
  'cassini_viewer/library_monitor.py',
  'cassini_viewer/capture_activity.py',
  'cassini_viewer/meeting_state.py',
  'cassini_viewer/ux_formats.py',
  'cassini_viewer/screenshot.py',
  'cassini_viewer/player.py',
  'cassini_viewer/folder_player.py',
  'cassini_viewer/recorder.py',
  'cassini_viewer/native_recorder.py',
  'cassini_viewer/spectro_analysis.py',
  'cassini_viewer/spectro_source.py',
  'cassini_viewer/recording_page.py',
  'cassini_viewer/recording_comfort.py',
  'cassini_viewer/recording_details.py',
  'cassini_viewer/capture_details.py',
  'cassini_viewer/capture_facts.py',
  'cassini_viewer/capture_details_dialog.py',
  'cassini_viewer/global_shortcut.py',
  'cassini_viewer/settings_dialog.py',
  'cassini_viewer/library_move.py',
  'cassini_viewer/library_location.py',
  'cassini_viewer/library_relocator.py',
  'cassini_viewer/record_first.py',
  'cassini_viewer/recording_session.py',
  'cassini_viewer/browser_host.py',
  'cassini_viewer/browser_audio_proxy.py',
  'cassini_viewer/browser_capture.py',
  'cassini_viewer/browser_watch.py',
  'cassini_viewer/native_tap.py',
  'cassini_viewer/native_timing.py',
  'cassini_viewer/browser_control_host.py',
  'cassini_viewer/browser_service.py',
  'cassini_viewer/companion.py',
  'cassini_viewer/flatpak_bridge.py',
  'cassini_viewer/flatpak-native-host.sh',
  'cassini_viewer/companion_setup.py',
  'cassini_viewer/browser_notifications.py',
  'cassini_viewer/portable.py',
  'cassini_viewer/runtime.py',
  'cassini_viewer/summary.py',
  'cassini_viewer/transcript.py',
  'cassini_viewer/window.py',
  install_dir: moduledir,
)

# Where the recognition child finds the app's private ONNX Runtime, if any
# (whistle_worker.onnxruntime_dir); a source run has no paths.json.
configure_file(
  input: 'cassini_viewer/paths.json.in',
  output: 'paths.json',
  configuration: {'ONNXRUNTIME_DIR': onnxruntime_dir, 'SHERPA_DIR': sherpa_dir, 'BINDIR': get_option('prefix') / get_option('bindir')},
  install: true,
  install_dir: moduledir,
)

# pytest is optional: the Flatpak SDK, for one, does not ship it.
pytest = python.find_installation('python3', modules: ['pytest'], required: false)
if pytest.found()
  test('pytest',
    pytest,
    args: ['-m', 'pytest', '-q', '--durations=20', meson.project_source_root() / 'tests'],
    env: ['PYTHONPATH=' + meson.current_source_dir()],
    workdir: meson.project_source_root(),
    # The suite re-runs both conformance suites as subprocesses, alongside
    # Meson's own conformance tests: 11-21 s in a package build container,
    # over Meson's 30 s default on a loaded builder.
    timeout: 120,
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
