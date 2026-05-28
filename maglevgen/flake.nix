{
  description = "ixy.rs traffic generator with flow randomization";

  inputs = {
    mars-std.url = "github:mars-research/mars-std";
  };

  outputs = { self, mars-std, ... }: let
    supportedSystems = [ "x86_64-linux" ];
  in mars-std.lib.eachSystem supportedSystems (system: let
    nightlyVersion = "2025-01-10";

    pkgs = mars-std.legacyPackages.${system};
    pinnedRust = pkgs.rust-bin.nightly.${nightlyVersion}.default.override {
      extensions = [ "rust-src" "rust-analyzer-preview" ];
      targets = [ "x86_64-unknown-linux-gnu" ];
    };
    rustPlatform = pkgs.makeRustPlatform {
      rustc = pinnedRust;
      cargo = pinnedRust;
    };
  in rec {
    defaultPackage = legacyPackages.maglevgen;
    legacyPackages.maglevgen = import ./default.nix {
      inherit rustPlatform;
      lib = pkgs.lib;
    };

    devShell = pkgs.mkShell {
      nativeBuildInputs = [
        pinnedRust
      ] ++ (with pkgs; [
        gnumake

	(dpdk.override { withExamples = [ "all" ]; })
      ]);
    };
  });
}
