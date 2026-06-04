{ stdenv, lib, fetchurl, meson, ninja, pkg-config
, dpdk, libbsd
}:

stdenv.mkDerivation rec {
  pname = "l2fwd-maglev";
  version = "0.1.0";

  src = ./.;

  nativeBuildInputs = [ meson ninja pkg-config ];

  buildInputs = [ dpdk libbsd ];

  RTE_SDK = dpdk;
}
