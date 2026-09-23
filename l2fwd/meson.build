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


add_project_arguments('-mavx512f', '-mavx512dq', language: 'c')
pktgen_conf = configuration_data()

# set up some global vars for compiler, platform, configuration, etc.
cc = meson.get_compiler('c')

add_project_arguments('-march=native', language: 'c')

add_project_arguments('-DALLOW_EXPERIMENTAL_API', language: 'c')
add_project_arguments('-D_GNU_SOURCE', language: 'c')
if get_option('nbprobe')
	add_project_arguments('-DNB_PROBE', language: 'c')
endif
if get_option('nbptw')
	add_project_arguments('-DNB_PTW', language: 'c')
endif

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

# -march that actually wins. Before 2026-09-23 project `-march=native` lost
# twice: nix cc-wrapper (NIX_ENFORCE_NO_NATIVE=1) strips it, and DPDK 21.11
# pkg-config Cflags end `-march=nehalem`, which meson places AFTER project
# args. Net: nehalem ISA + tune, plus explicit -mavx512f/-mavx512dq only (no
# BMI2, FMA, LZCNT, MOVBE, AVX-512 BW/VL). Target c_args land after dependency
# args, so arch_args wins. -Dmarch=dpdk = old behaviour, no -march appended.
# docs/REFLECT_PATH.md s6.
arch_args = get_option('march') == 'dpdk' ? [] : ['-march=' + get_option('march')]

subdir('libsashstore')

l2fwd = executable('l2fwd',
		sources,
		c_args: [ '-Wno-error=unused-parameter' ] + arch_args,
		install: true,
		include_directories: libsashstore_inc,
		link_with: libsashstore,
		dependencies: [ dpdk ])
