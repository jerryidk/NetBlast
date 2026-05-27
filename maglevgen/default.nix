{ lib, rustPlatform }:

with builtins;

let
  badDirectories = [ "target" ".direnv" ];
in rustPlatform.buildRustPackage {
  name = "maglevgen";
  version = "0.1.0";

  src = lib.cleanSourceWith {
    filter = name: type: !(type == "directory" && elem name badDirectories);
    src = lib.cleanSourceWith {
      filter = lib.cleanSourceFilter;
      src = ./.;
    };
  };
  cargoSha256 = "sha256-KDe3TrnTd7LE5OHxYh7s7SxhdCJnMHzYqlOWlgQ+dvI=";
  doCheck = false;
}
