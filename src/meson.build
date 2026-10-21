conf = configuration_data()
conf.set('PYTHON', python.full_path())
conf.set('PKGDATADIR', pkgdatadir)

configure_file(
  input: 'jungbackup.in',
  output: 'jungbackup',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

install_subdir('jungbackup',
  install_dir: pkgdatadir,
  exclude_directories: ['__pycache__'],
)
