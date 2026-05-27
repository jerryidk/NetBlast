libsashstore_sources = [ 'conshash.c', 'hash.c', 'hashmap.c', 'maglev.c', 'packettool.c', 'sashstore.c' ]

libsashstore_inc = include_directories('./.')

libsashstore = library('sashstore',
    sources: libsashstore_sources,
    include_directories: libsashstore_inc,
    dependencies: dpdk,
    override_options: [
        'b_lto=false',
    ]
)
