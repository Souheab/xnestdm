{
  description = "PySide6 host for an embedded Xephyr XFCE session";

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
        let
          runtimePackages = with pkgs; [
            adwaita-icon-theme
            bash
            coreutils
            dbus
            garcon
            gnome-themes-extra
            hicolor-icon-theme
            shared-mime-info
            tango-icon-theme
            thunar
            xdg-user-dirs
            xfce4-appfinder
            xfce4-icon-theme
            xfce4-notifyd
            xfce4-panel
            xfce4-power-manager
            xfce4-session
            xfce4-settings
            xfce4-taskmanager
            xfce4-terminal
            xfconf
            xfdesktop
            xfwm4
            xfwm4-themes
            xorg-server
            xmodmap
            xrdb
          ];
        in
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
            "--set USERDESK_DBUS_RUN_SESSION ${pkgs.dbus}/bin/dbus-run-session"
            "--set USERDESK_SESSION_ENTRY $out/bin/userdesk-session-entry"
            "--set USERDESK_SHELL ${pkgs.bash}/bin/sh"
            "--set USERDESK_XFCE_XINITRC ${pkgs.xfce4-session.xinitrc}"
            "--set USERDESK_XFCE_LOGOUT ${pkgs.xfce4-session}/bin/xfce4-session-logout"
            "--prefix PATH : ${pkgs.lib.makeBinPath runtimePackages}"
            "--prefix XDG_DATA_DIRS : ${pkgs.lib.makeSearchPath "share" runtimePackages}"
            "--prefix XDG_CONFIG_DIRS : ${pkgs.lib.makeSearchPath "etc/xdg" runtimePackages}"
          ];

          meta = {
            description = "Run a local user's XFCE session in embedded Xephyr";
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
          meta.description = "Run a local user's XFCE session in embedded Xephyr";
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
              pkgs.xfce4-session
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
        in
        {
          options.programs.userdesk.enable = lib.mkEnableOption "Userdesk";

          config = lib.mkIf cfg.enable {
            environment.systemPackages = [
              self.packages.${pkgs.stdenv.hostPlatform.system}.userdesk
            ];
            security.pam.services.userdesk = {
              startSession = true;
              setLoginUid = false;
            };
          };
        };
    };
}
