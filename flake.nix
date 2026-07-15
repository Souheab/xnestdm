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
      mkUserdesk =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "userdesk";
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
          pythonImportsCheck = [ "userdesk" ];

          makeWrapperArgs = [
            "\${qtWrapperArgs[@]}"
            "--set QT_QPA_PLATFORM xcb"
            "--set USERDESK_XEPHYR ${pkgs.xorg-server}/bin/Xephyr"
          ];

          meta = {
            description = "Run host X11 sessions in embedded Xephyr";
            license = pkgs.lib.licenses.mit;
            mainProgram = "userdesk";
            platforms = pkgs.lib.platforms.linux;
          };
        };
    in
    {
      packages = forAllSystems (system: {
        userdesk = mkUserdesk nixpkgs.legacyPackages.${system};
        default = self.packages.${system}.userdesk;
      });

      apps = forAllSystems (system: {
        userdesk = {
          type = "app";
          program = "${self.packages.${system}.userdesk}/bin/userdesk";
          meta.description = "Run host X11 sessions in embedded Xephyr";
        };
        default = self.apps.${system}.userdesk;
      });

      checks = forAllSystems (system: {
        userdesk = self.packages.${system}.userdesk;
      });

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

      nixosModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.programs.userdesk;
          sessionData = config.services.displayManager.sessionData;
          package = self.packages.${pkgs.stdenv.hostPlatform.system}.userdesk.overrideAttrs (previous: {
            makeWrapperArgs =
              (previous.makeWrapperArgs or [ ])
              ++ lib.optionals (sessionData ? desktops) [
                "--set USERDESK_XSESSION_DIRS ${sessionData.desktops}/share/xsessions"
              ]
              ++ lib.optionals (sessionData ? wrapper) [
                "--set USERDESK_XSESSION_WRAPPER ${sessionData.wrapper}"
              ];
          });
        in
        {
          options.programs.userdesk.enable = lib.mkEnableOption "Userdesk";

          config = lib.mkIf cfg.enable {
            environment.systemPackages = [
              package
            ];
            security.pam.services.userdesk = {
              startSession = true;
              setLoginUid = false;
            };
          };
        };
    };
}
