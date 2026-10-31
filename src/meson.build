# SPDX-License-Identifier: GPL-3.0-or-later
talk_resources = gnome.compile_resources('cassini-talk',
  'cassini-talk.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

# The launcher's shebang points at this interpreter, so it must be the one
# that has PyGObject: a python3 from a virtualenv usually does not. Missing gi
# is only a warning because packagers often build without runtime deps.
py = python.find_installation('python3')
if not py.language_version().version_compare('>= 3.11')
  warning('Python @0@ found; cassini-talk needs Python 3.11 or newer at run time.'.format(py.language_version()))
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

launcher = configure_file(
  input: 'cassini-talk.in',
  output: 'cassini-talk',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)

talk_sources = files(
  'cassini_talk/__init__.py',
  'cassini_talk/colors.py',
  'cassini_talk/main.py',
  'cassini_talk/identity.py',
  'cassini_talk/main_loop_watchdog.py',
  'cassini_talk/installed_build.py',
  'cassini_talk/build_refresh.py',
  'cassini_talk/library_window.py',
  'cassini_talk/meeting_trash.py',
  'cassini_talk/trash_cleanup.py',
  'cassini_talk/model_manifest.py',
  'cassini_talk/model_download.py',
  'cassini_talk/zstd_stream.py',
  'cassini_talk/search_index.py',
  'cassini_talk/search_page.py',
  'cassini_talk/meeting_page.py',
  'cassini_talk/speaker_merges.py',
  'cassini_talk/speaker_edits.py',
  'cassini_talk/speakers_dialog.py',
  'cassini_talk/meeting_text.py',
  'cassini_talk/meeting_store.py',
  'cassini_talk/meeting_rename.py',
  'cassini_talk/rename_dialog.py',
  'cassini_talk/transcription.py',
  'cassini_talk/transcript_publish.py',
  'cassini_talk/speech_models.py',
  'cassini_talk/models.json',
  'cassini_talk/voices_logic.py',
  'cassini_talk/campplus_model.py',
  'cassini_talk/campplus.py',
  'cassini_talk/voice_models.py',
  'cassini_talk/voice_backfill.py',
  'cassini_talk/browser_connection.py',
  'cassini_talk/browser_status.py',
  'cassini_talk/voice_store.py',
  'cassini_talk/voice_board.py',
  'cassini_talk/saved_voices_page.py',
  'cassini_talk/worker_voices.py',
  'cassini_talk/diarization.py',
  'cassini_talk/diarization_abi.py',
  'cassini_talk/diarization_audio.py',
  'cassini_talk/diarization_models.py',
  'cassini_talk/worker_diarization.py',
  'cassini_talk/nemotron.py',
  'cassini_talk/nemotron_model.py',
  'cassini_talk/parakeet.py',
  'cassini_talk/parakeet_abi.py',
  'cassini_talk/parakeet_model.py',
  'cassini_talk/parakeet_client.py',
  'cassini_talk/parakeet_worker.py',
  'cassini_talk/sherpa_runtime.py',
  'cassini_talk/sherpa-pins.json',
  'cassini_talk/whistle_client.py',
  'cassini_talk/whistle_worker.py',
  'cassini_talk/speech_pipeline.py',
  'cassini_talk/opus_input.py',
  'cassini_talk/fir-48k-16k.json',
  'cassini_talk/inline_line.py',
  'cassini_talk/open_meeting.py',
  'cassini_talk/export_model.py',
  'cassini_talk/export_audio.py',
  'cassini_talk/portable_context.py',
  'cassini_talk/portable_writer.py',
  'cassini_talk/exporting.py',
  'cassini_talk/export_dialog.py',
  'cassini_talk/received.py',

  'cassini_talk/capture_guard.py',
  'cassini_talk/background_recording.py',
  'cassini_talk/windowless.py',
  'cassini_talk/status_notifier.py',
  'cassini_talk/capture_sources.py',
  'cassini_talk/call_lifecycle.py',
  'cassini_talk/session_events.py',
  'cassini_talk/what_is_recorded.py',
  'cassini_talk/strings.py',
  'cassini_talk/strings.json',
  'cassini_talk/library.py',
  'cassini_talk/library_v2_index.py',
  'cassini_talk/library_publication.py',
  'cassini_talk/publication_state.py',
  'cassini_talk/library_recovery.py',
  'cassini_talk/library_edits.py',
  'cassini_talk/library_files.py',
  'cassini_talk/track_source.py',
  'cassini_talk/library_schema.py',
  'cassini_talk/meeting.schema.json',
  'cassini_talk/library_monitor.py',
  'cassini_talk/capture_activity.py',
  'cassini_talk/meeting_state.py',
  'cassini_talk/ux_formats.py',
  'cassini_talk/screenshot.py',
  'cassini_talk/player.py',
  'cassini_talk/folder_player.py',
  'cassini_talk/recorder.py',
  'cassini_talk/native_recorder.py',
  'cassini_talk/spectro_analysis.py',
  'cassini_talk/spectro_source.py',
  'cassini_talk/spectro_transport.py',
  'cassini_talk/spectro_view_model.py',
  'cassini_talk/spectro_views.py',
  'cassini_talk/spectrogram_strip.py',
  'cassini_talk/recording_page.py',
  'cassini_talk/recording_comfort.py',
  'cassini_talk/recording_details.py',
  'cassini_talk/capture_details.py',
  'cassini_talk/capture_facts.py',
  'cassini_talk/capture_details_dialog.py',
  'cassini_talk/global_shortcut.py',
  'cassini_talk/settings_dialog.py',
  'cassini_talk/library_move.py',
  'cassini_talk/library_move_pair.py',
  'cassini_talk/library_location.py',
  'cassini_talk/library_relocator.py',
  'cassini_talk/record_first.py',
  'cassini_talk/recording_session.py',
  'cassini_talk/browser_host.py',
  'cassini_talk/browser_audio_proxy.py',
  'cassini_talk/browser_capture.py',
  'cassini_talk/browser_watch.py',
  'cassini_talk/native_tap.py',
  'cassini_talk/native_timing.py',
  'cassini_talk/capture_clock.py',
  'cassini_talk/rotating_wav.py',
  'cassini_talk/mapped_audio.py',
  'cassini_talk/output_mute.py',
  'cassini_talk/browser_control_host.py',
  'cassini_talk/browser_service.py',
  'cassini_talk/companion.py',
  'cassini_talk/registration.py',
  'cassini_talk/companion_bundle.py',
  'cassini_talk/companion_local_dialog.py',
  'cassini_talk/flatpak_bridge.py',
  'cassini_talk/flatpak-native-host.sh',
  'cassini_talk/companion_setup.py',
  'cassini_talk/browser_notifications.py',
  'cassini_talk/portable.py',
  'cassini_talk/runtime.py',
  'cassini_talk/summary.py',
  'cassini_talk/transcript.py',
  'cassini_talk/window.py',
)
install_data(talk_sources, install_dir: moduledir)

# S-12 Library v2 (step 3a: not wired into the app yet).
install_data(
  'cassini_talk/library_v2/__init__.py',
  'cassini_talk/library_v2/edit.py',
  'cassini_talk/library_v2/finalize.py',
  'cassini_talk/library_v2/fsutil.py',
  'cassini_talk/library_v2/journal.py',
  'cassini_talk/library_v2/mixdown.py',
  'cassini_talk/library_v2/names.py',
  'cassini_talk/library_v2/ogg.py',
  'cassini_talk/library_v2/publish.py',
  'cassini_talk/library_v2/xschema.py',
  'cassini_talk/library_v2/x-schema.json',
  install_dir: moduledir / 'library_v2',
)

# Where the recognition child finds the app's private ONNX Runtime, if any
# (whistle_worker.onnxruntime_dir); a source run has no paths.json.
runtime_paths = configure_file(
  input: 'cassini_talk/paths.json.in',
  output: 'paths.json',
  configuration: {'ONNXRUNTIME_DIR': onnxruntime_dir, 'SHERPA_DIR': sherpa_dir, 'BINDIR': get_option('prefix') / get_option('bindir'), 'STORE_LISTING_PUBLIC': get_option('store_listing_public').to_string('true', 'false'), 'PROFILE': get_option('profile')},
  install: true,
  install_dir: moduledir,
)

# Content identity changes even for a same-version patch. Ninja tracks every
# runtime source, generated launcher/resource, and bundled companion input.
identity_script = files('../build-aux/build-identity.py')
extension_inputs = run_command(py, identity_script,
  '--list-extension', meson.project_source_root() / 'browser-extension', check: true).stdout().strip().split('\n')
identity_inputs = talk_sources + [launcher, runtime_paths, talk_resources] + files(extension_inputs)
if get_option('whistle').enabled()
  identity_inputs += files('../whistle/pins.json', '../whistle/cpu_limit.c')
endif
custom_target('installed-build-identity',
  input: identity_inputs,
  output: 'build-info.json',
  command: [py, identity_script, '--source', meson.project_source_root(),
    '--build', meson.project_build_root(), '--version', meson.project_version(),
    '--profile', get_option('profile'), '--application', application_id,
    '--output', '@OUTPUT@', '@INPUT@'],
  build_by_default: true,
  install: true,
  install_dir: pkgdatadir,
)

# pytest is optional: the Flatpak SDK, for one, does not ship it.
pytest = python.find_installation('python3', modules: ['pytest'], required: false)
if pytest.found()
  test('pytest',
    pytest,
    args: ['-m', 'pytest', '-q', '--durations=20', meson.project_source_root() / 'tests'],
    env: ['PYTHONPATH=' + meson.current_source_dir(), 'CASSINI_TEST_TIMINGS=1'],
    workdir: meson.project_source_root(),
    # The suite re-runs both conformance suites as subprocesses, alongside
    # Meson's own conformance tests: 11-21 s in a package build container,
    # over Meson's 30 s default on a loaded builder. Library v2 adds a 60 min
    # alignment golden, a scaled long meeting and a SIGKILL crash matrix.
    timeout: 240,
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
    depends: talk_resources,
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
    suite: 'recording', depends: talk_resources,
    timeout: 300, workdir: meson.project_source_root(),
  )
endif
