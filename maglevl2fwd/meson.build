project('l2fwd-maglev', 'C',
    version: '0.1.0',

	license: 'BSD',
	default_options: [
		'buildtype=release',
		'default_library=static',
		'warning_level=3',
		'werror=true',
		'debug=true',
    'b_lto=true',
	],
	meson_version: '>= 0.47.1'
)

pktgen_conf = configuration_data()

# set up some global vars for compiler, platform, configuration, etc.
cc = meson.get_compiler('c')

add_project_arguments('-march=native', language: 'c')

add_project_arguments('-DALLOW_EXPERIMENTAL_API', language: 'c')
add_project_arguments('-D_GNU_SOURCE', language: 'c')

# enable extra warnings and disable any unwanted warnings
warning_flags = [
	'-Wno-pedantic',
	'-Wno-format-truncation',
	'-Wno-error=unused-parameter',
	'-Wno-error=restrict',
	'-Wno-error=format',
]
foreach arg: warning_flags
	if cc.has_argument(arg)
		add_project_arguments(arg, language: 'c')
	endif
endforeach

dpdk = dependency('libdpdk', required: true)
# message('prefix: ' + get_option('prefix') + ' libdir: ' + get_option('libdir'))

sources = files(
	'main.c'
)

subdir('libsashstore')

l2fwd = executable('l2fwd',
		sources,
		c_args: [ '-Wno-error=unused-parameter' ],
		install: true,
		include_directories: libsashstore_inc,
		link_with: libsashstore,
		dependencies: [ dpdk ])
