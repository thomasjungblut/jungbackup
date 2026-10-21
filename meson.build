project('jungbackup',
  version: '0.1.0',
  meson_version: '>= 1.0.0',
  license: 'Apache-2.0',
)

app_id = 'io.jungblut.JungBackup'
python = import('python').find_installation('python3')
pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()

subdir('data')
subdir('src')
