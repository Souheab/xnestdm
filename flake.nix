{
  description = "Host-native nested X11 display manager embedded with Xephyr";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      mkXnestdm =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "xnestdm";
          version = "0.1.0";
          pyproject = true;
          src = self;

          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [
            pamela
            pyside6
          ];

          nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
          buildInputs = [ pkgs.qt6.qtbase ];
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
          doCheck = true;
          preCheck = ''
            export QT_QPA_PLATFORM=offscreen
          '';
          pythonImportsCheck = [ "xnestdm" ];

          makeWrapperArgs = [
            "--unset PYTHONHOME"
            "--unset PYTHONPATH"
            "--unset PYTHONSTARTUP"
            "--unset QML2_IMPORT_PATH"
            "--unset QML_IMPORT_PATH"
            "--unset QT_PLUGIN_PATH"
            "--unset QT_QPA_PLATFORM_PLUGIN_PATH"
            "--unset QT_STYLE_OVERRIDE"
            "\${qtWrapperArgs[@]}"
            "--set QT_QPA_PLATFORM xcb"
            "--set XNESTDM_XEPHYR ${pkgs.xorg-server}/bin/Xephyr"
          ];

          meta = {
            description = "Run host X11 sessions in embedded Xephyr";
            license = pkgs.lib.licenses.mit;
            mainProgram = "xnestdm";
            platforms = pkgs.lib.platforms.linux;
          };
        };
    in
    {
      packages = forAllSystems (system: {
        xnestdm = mkXnestdm nixpkgs.legacyPackages.${system};
        default = self.packages.${system}.xnestdm;
      });

      apps = forAllSystems (system: {
        xnestdm = {
          type = "app";
          program = "${self.packages.${system}.xnestdm}/bin/xnestdm";
          meta.description = "Run host X11 sessions in embedded Xephyr";
        };
        default = self.apps.${system}.xnestdm;
      });

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          moduleConfig =
            (nixpkgs.lib.nixosSystem {
              inherit system;
              modules = [
                self.nixosModules.default
                {
                  programs.xnestdm.enable = true;
                  system.stateVersion = "26.05";
                }
              ];
            }).config;
          hasHelperWrapper = builtins.hasAttr "xnestdm-helper" moduleConfig.security.wrappers;
          hasGuiWrapper = builtins.hasAttr "xnestdm" moduleConfig.security.wrappers;
        in
        {
          xnestdm = self.packages.${system}.xnestdm;
          module =
            assert hasHelperWrapper;
            assert moduleConfig.security.wrappers."xnestdm-helper".setuid;
            assert !hasGuiWrapper;
            pkgs.runCommand "xnestdm-module-check" { } ''
              touch $out
            '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (pythonPackages: [
                pythonPackages.pamela
                pythonPackages.pytest
                pythonPackages.pyside6
              ]))
              pkgs.nixfmt
              pkgs.ruff
              pkgs.xorg-server
            ];
            QT_QPA_PLATFORM = "xcb";
          };
        }
      );

      nixosModules.default = import ./nixos/module.nix { inherit self; };
    };
}
