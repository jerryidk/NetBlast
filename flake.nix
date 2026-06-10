{
  description = "A simple project";

  inputs = {
    mars-std.url = "github:mars-research/mars-std";
  };

  outputs = { self, mars-std, ... }: let
    # System types to support.
    supportedSystems = [ "x86_64-linux" ];
  in mars-std.lib.eachSystem supportedSystems (system: let
    pkgs = mars-std.legacyPackages.${system};
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
  });
}
