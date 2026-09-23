{
  description = "A simple project";

  inputs = {
    mars-std.url = "github:mars-research/mars-std";
    # Tools only. Never builds l2fwd: DPDK, gcc stay on mars-std so binary under
    # test byte-identical whichever shell ran it. mars-std perf 5.17 / pcm 202112
    # predate Emerald Rapids (family 6 model 207) -- no event JSON, hence raw
    # encodings in harness.sh. Newer perf has names, but names = new instrument:
    # re-validate against harness.sh validate known answers before trusting.
    tools.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs = { self, mars-std, tools, ... }: let
    # System types to support.
    supportedSystems = [ "x86_64-linux" ];
  in mars-std.lib.eachSystem supportedSystems (system: let
    pkgs = mars-std.legacyPackages.${system};
    tpkgs = tools.legacyPackages.${system};
  in rec {
    defaultPackage = packages.l2fwd-maglev;
    packages.l2fwd-maglev = pkgs.callPackage ./package.nix { };

    devShell = pkgs.mkShell {
      # Inherit the build dependencies of your C project
      inputsFrom = [ defaultPackage ];

      # Add extra tools specifically for your development environment
      packages = [
        pkgs.python3
        pkgs.python3Packages.matplotlib
      ];
    };

    # Profiling shell: `nix develop .#profile`. Build shell deps plus tools.
    # perf: PEBS load latency (perf mem), c2c, Intel PT + ptwrite, arch LBR.
    # xed: perf script --xed disassembly of PT traces (perf decodes PT itself).
    # pcm: uncore cross-check of raw IMC encodings. pahole: struct layout.
    # llvm: llvm-mca throughput bound on compute kernels. bpftrace: uprobe
    # COUNTS only (alloc inventory), never timing -- trap costs ~us.
    devShells.profile = pkgs.mkShell {
      inputsFrom = [ defaultPackage ];
      packages = [
        pkgs.python3
        pkgs.python3Packages.matplotlib
        tpkgs.perf
        tpkgs.xed
        tpkgs.pcm
        tpkgs.pahole
        tpkgs.llvmPackages.llvm
        tpkgs.bpftrace
        tpkgs.babeltrace2
        tpkgs.likwid
      ];
    };
  });
}
