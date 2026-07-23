{
  description = "Navidrome Music Collector — Soulseek-powered music downloader";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };

        python = pkgs.python312;
        pythonEnv = python.withPackages (ps: with ps; [
          mutagen
          requests
          pyacoustid
          click
          # chromaprint binary for acoustid fingerprinting
          chromaprint
        ]);

        navidrome-collector = python.pkgs.buildPythonPackage {
          pname = "navidrome-collector";
          version = "0.1.0";
          src = ./.;
          pyproject = true;

          nativeBuildInputs = with pkgs; [
            python.pkgs.setuptools
            python.pkgs.wheel
          ];

          propagatedBuildInputs = with python.pkgs; [
            mutagen
            requests
            pyacoustid
            click
          ];

          # Tests need chromaprint/slskd — skip for nix build
          doCheck = false;

          meta = {
            description = "Soulseek-powered music collector for Navidrome";
            homepage = "https://git.827482.xyz/xvantz/navidrome-collector";
            license = pkgs.lib.licenses.mit;
            maintainers = with pkgs.lib.maintainers; [ xvantz ];
          };
        };
      in
      {
        packages = {
          default = navidrome-collector;
          navidrome-collector = navidrome-collector;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.chromaprint
          ];
          shellHook = ''
            echo "navidrome-collector dev shell"
            echo "  mutagen  requests  pyacoustid  click"
            echo ""
          '';
        };
      }
    ) // {
      # NixOS module (cross-platform, not per-system)
      nixosModules.default = import ./nixos-module.nix self;
      nixosModules.navidrome-collector = import ./nixos-module.nix self;
    };
}
