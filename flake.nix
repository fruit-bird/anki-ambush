{
  description = "Anki Ambush: an Anki add-on that ambushes you with due cards";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # Adds pkgs.ankiAddons.anki-ambush
      overlays.default = final: prev: {
        ankiAddons = (prev.ankiAddons or { }) // {
          anki-ambush = final.callPackage ./nix/package.nix { };
        };
      };

      packages = forAllSystems (pkgs: rec {
        anki-ambush = pkgs.callPackage ./nix/package.nix { };
        default = anki-ambush;
        # Anki wrapped with just this add-on, for trying it out: nix run .#anki
        anki = pkgs.anki.withAddons [ anki-ambush ];
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
