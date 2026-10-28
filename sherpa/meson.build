# SPDX-License-Identifier: GPL-3.0-or-later
if not get_option('whistle').enabled()
  error('-Dsherpa=enabled needs -Dwhistle=enabled for bundled Silero and private Python ONNX Runtime')
endif
if host_machine.system() != 'linux' or host_machine.cpu_family() != 'x86_64'
  error('The D16 sherpa runtime is pinned only for Linux glibc x86_64')
endif
add_languages('c', native: false, required: true)
sherpa_cc = meson.get_compiler('c')
if sherpa_cc.get_define('__GLIBC__', prefix: '#include <features.h>') == ''
  error('The pinned D16 sherpa runtime needs glibc')
endif
sherpa_cache = get_option('sherpa_cache')
if not sherpa_cache.startswith('/')
  sherpa_cache = meson.project_source_root() / sherpa_cache
endif
sherpa_bundle = meson.current_build_dir() / 'bundle'
sherpa_python = python.find_installation('python3')
run_command(sherpa_python, files('fetch.py'), sherpa_bundle, '--cache', sherpa_cache, '--offline', check: true)
run_command(sherpa_python, files('fetch.py'), '--cache', sherpa_cache, '--verify', sherpa_bundle, check: true)
install_subdir(sherpa_bundle, install_dir: sherpa_dir, strip_directory: true, install_mode: 'rw-r--r--')
