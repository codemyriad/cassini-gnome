# SPDX-License-Identifier: GPL-3.0-or-later
desktop_conf = configuration_data()
desktop_conf.set('APP_ID', application_id)
desktop_conf.set('APP_NAME', application_name)
desktop_file = i18n.merge_file(
        input: configure_file(
          input: 'io.codemyriad.CassiniTalk.desktop.in',
          output: 'io.codemyriad.CassiniTalk.desktop.in.in',
          configuration: desktop_conf,
        ),
       output: application_id + '.desktop',
         type: 'desktop',
       po_dir: '../po',
      install: true,
  install_dir: get_option('datadir') / 'applications',
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

metainfo_conf = configuration_data()
metainfo_conf.set('APP_ID', application_id)
metainfo_conf.set('LEGACY_APP_ID', legacy_application_id)
metainfo_conf.set('APP_NAME', application_name)
appstream_file = i18n.merge_file(
        input: configure_file(
          input: 'io.codemyriad.CassiniTalk.metainfo.xml.in',
          output: 'io.codemyriad.CassiniTalk.metainfo.xml.in.in',
          configuration: metainfo_conf,
        ),
       output: application_id + '.metainfo.xml',
       po_dir: '../po',
      install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('Validate appstream file', appstreamcli,
    args: ['validate', '--no-net', '--explain', appstream_file])
endif

install_data('io.codemyriad.CassiniTalk.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test('Validate schema file', compile_schemas,
    args: ['--strict', '--dry-run', meson.current_source_dir()])
endif

install_data('icons/hicolor/scalable/apps/io.codemyriad.CassiniTalk.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps',
  rename: application_id + '.svg',
)
install_data('icons/hicolor/symbolic/apps/io.codemyriad.CassiniTalk-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps',
  rename: application_id + '-symbolic.svg',
)

service_conf = configuration_data()
service_conf.set('APP_ID', application_id)
service_conf.set('BINDIR', get_option('prefix') / get_option('bindir'))
configure_file(
  input: 'io.codemyriad.CassiniTalk.service.in',
  output: application_id + '.service',
  configuration: service_conf,
  install_dir: get_option('datadir') / 'dbus-1' / 'services',
)

install_man('cassini-talk.1', 'cassini-viewer.1')
