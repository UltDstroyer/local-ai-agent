{
  description = "ctx-agent: isolated local AI task runner for NixOS";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      each = f: nixpkgs.lib.genAttrs systems
        (system: f (import nixpkgs { inherit system; }));
    in {
      packages = each (pkgs: {
        default = pkgs.callPackage ./nix/package.nix { };
      });
      nixosModules.default = import ./nix/module.nix;
      nixosModules.ctx-agent = self.nixosModules.default;
    };
}
