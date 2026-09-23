libsashstore_sources = [ 'conshash.c', 'hash.c', 'hashmap.c', 'maglev.c', 'packettool.c', 'sashstore.c', 'dramblast.c', 'backing.c', 'nbprobe.c']

libsashstore_inc = include_directories('./.')

libsashstore = library('sashstore',
    sources: libsashstore_sources,
    include_directories: libsashstore_inc,
    dependencies: dpdk,
    c_args: arch_args,  # after dependency -march=nehalem; see ../meson.build
    override_options: [
        'b_lto=false',
    ]
)
