# SPDX-License-Identifier: GPL-3.0-or-later
# Separate spike integration: parent packaging manifests are coordinator-owned.
project('cassini-whistle-spike', version: '0.1', meson_version: '>= 1.1.0')
py = import('python').find_installation('python3')
bundle = get_option('bundle')
if not bundle.startswith('/')
  error('Pass an absolute -Dbundle= path produced by fetch.py')
endif
run_command(py, files('verify.py'), bundle, check: true)
foreach name : ['whistle.cact', 'silero_vad.onnx', 'LICENSE-Silero-MIT.txt', 'libneedle3.so', 'wrapper.whl', 'libcassini_whistle_cpu.so', 'pins.json', 'manifest.json', 'NOTICE.md', 'LICENSE-Apache-2.0.txt', 'cpu_limit.c']
  install_data(bundle / name, install_dir: get_option('datadir') / 'cassini-viewer' / 'whistle')
endforeach
install_data('../src/cassini_viewer/opus_input.py', '../src/cassini_viewer/speech_pipeline.py', '../src/cassini_viewer/fir-48k-16k.json', '../src/cassini_viewer/whistle_worker.py', '../src/cassini_viewer/whistle_client.py',
  install_dir: get_option('datadir') / 'cassini-viewer' / 'cassini_viewer')
