# SPDX-License-Identifier: GPL-3.0-or-later
# The offline writing-down bundle, enabled with -Dwhistle=enabled.
# fetch.py never downloads here (--offline): the pinned artifacts must already
# be in the cache directory (-Dwhistle_cache, relative to the source root),
# put there by build-aux/whistle-cache.sh or by the package's own sources.
# Packages must not strip or move these files: the app checks every hash in
# manifest.json before it loads anything. The same goes for the private ONNX
# Runtime (the pinned wheel for this Python, checked against its RECORD).
if host_machine.system() != 'linux' or host_machine.cpu_family() != 'x86_64'
  error('-Dwhistle=enabled needs a Linux x86_64 host; the pinned engine exists only for glibc x86_64. Build with -Dwhistle=disabled elsewhere.')
endif
add_languages('c', native: false, required: true)
whistle_cc = meson.get_compiler('c')
if whistle_cc.get_define('__GLIBC__', prefix: '#include <features.h>') == ''
  error('-Dwhistle=enabled needs glibc; the pinned engine does not run on this C library.')
endif

whistle_cache = get_option('whistle_cache')
if not whistle_cache.startswith('/')
  whistle_cache = meson.project_source_root() / whistle_cache
endif
whistle_bundle = meson.current_build_dir() / 'bundle'
whistle_python = python.find_installation('python3')
message('Preparing the Whistle bundle from ' + whistle_cache)
# Run at configure time with check: false, so a missing or corrupt artifact
# fails with fetch.py's own message (which names the URL and sha256).
whistle_fetch = run_command(whistle_python, files('fetch.py'), whistle_bundle,
  '--cache', whistle_cache, '--offline',
  env: {'CC': ' '.join(whistle_cc.cmd_array())},
  check: false,
)
if whistle_fetch.returncode() != 0
  error(whistle_fetch.stderr().strip())
endif
whistle_verify = run_command(whistle_python, files('verify.py'), whistle_bundle, check: false)
if whistle_verify.returncode() != 0
  error('The Whistle bundle failed verification: ' + whistle_verify.stderr().strip())
endif

# Exactly the files fetch.py wrote and verify.py checked (configure-time
# output, so install the directory rather than build targets).
install_subdir(whistle_bundle, install_dir: pkgdatadir / 'whistle', strip_directory: true,
  install_mode: 'rw-r--r--')

# ONNX Runtime: the pinned PyPI wheel for the Python the launcher runs with,
# unpacked without its model tools, instead of the distribution's package
# (which can pull in sympy, and with it hundreds of MB). Only the recognition
# child puts this directory on sys.path.
onnxruntime_abi = 'cp' + whistle_python.language_version().replace('.', '')
onnxruntime_build = meson.current_build_dir() / 'onnxruntime'
message('Preparing ONNX Runtime for ' + onnxruntime_abi)
onnxruntime_fetch = run_command(whistle_python, files('fetch.py'), onnxruntime_build,
  '--onnxruntime', onnxruntime_abi, '--cache', whistle_cache, '--offline',
  check: false,
)
if onnxruntime_fetch.returncode() != 0
  error(onnxruntime_fetch.stderr().strip())
endif
onnxruntime_verify = run_command(whistle_python, files('verify.py'), '--onnxruntime', onnxruntime_build, check: false)
if onnxruntime_verify.returncode() != 0
  error('The private ONNX Runtime failed verification: ' + onnxruntime_verify.stderr().strip())
endif
install_subdir(onnxruntime_build, install_dir: onnxruntime_dir, strip_directory: true,
  install_mode: 'rw-r--r--')
