# Functional tests for libsashstore hashtables.
# Builds dramblast.c UNMODIFIED, without DPDK, using the shims in shims/.
#
#   make        build and run
#   make run    same
#   make link-O0  demonstrate finding C5 (inline without static breaks linking)

LIB  := ../l2fwd/libsashstore
CC   ?= gcc
# icelake-server: AVX-512 + SSE4.2 CRC32 + cmpxchg16b, matching l2fwd meson option
# `march` default. Not native: nix gcc wrapper strips -march=native
# (NIX_ENFORCE_NO_NATIVE), which lost crc32 and failed the build in nix shell.
MARCH ?= icelake-server
CFLAGS := -O2 -g -march=$(MARCH) -Wall -Wextra -Wno-unused-parameter \
          -I$(LIB) -Ishims
SRCS := test_dramblast.c $(LIB)/dramblast.c $(LIB)/conshash.c $(LIB)/hash.c \
        $(LIB)/backing.c

.PHONY: all run clean check-c5
all: run

test_dramblast: $(SRCS) $(LIB)/dramblast.h Makefile
	$(CC) $(CFLAGS) -o $@ $(SRCS)

run: test_dramblast
	./test_dramblast

# Finding C5: dramblast.c marks five helpers `inline` with no `static` and no
# extern declaration, so no external definition is ever emitted. At -O1..-O3
# they are always inlined and it links by luck; at -O0, and at any -O level
# once a sanitizer inhibits inlining, the link fails.
check-c5:
	@echo "--- -O0 (expected: undefined references) ---"
	-@$(CC) -O0 -march=native -I$(LIB) -Ishims -o /dev/null $(SRCS) 2>&1 \
	  | grep -o "undefined reference to .[a-z_0-9]*." | sort | uniq -c
	@echo "--- -O2 with -fsanitize=address,undefined (expected: same) ---"
	-@$(CC) -O2 -march=native -fsanitize=address,undefined -I$(LIB) -Ishims \
	  -o /dev/null $(SRCS) 2>&1 \
	  | grep -o "undefined reference to .[a-z_0-9]*." | sort | uniq -c

clean:
	rm -f test_dramblast
