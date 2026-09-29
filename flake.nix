{
  description = "q-core: the personal-services API, MCP server and database";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
      hashes = import ./nix/wheelhouse-hashes.nix;
    in
    {
      packages = forAll (system: pkgs: rec {
        q-core = pkgs.callPackage ./nix/package.nix { wheelhouseHash = hashes.${system}; };
        default = q-core;
      });

      nixosModules.q-core = import ./nix/module.nix self;
      nixosModules.default = self.nixosModules.q-core;

      checks = forAll (system: pkgs: {
        package = self.packages.${system}.q-core;
        vm = import ./nix/test.nix { inherit pkgs self; };
      });

      devShells = forAll (system: pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.python313 pkgs.uv pkgs.ffmpeg-headless ]; };
      });
    };
}
