{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.xnestdm;
  sessionData = config.services.displayManager.sessionData;
  package = self.packages.${pkgs.stdenv.hostPlatform.system}.xnestdm.overrideAttrs (previous: {
    makeWrapperArgs =
      (previous.makeWrapperArgs or [ ])
      ++ lib.optionals (sessionData ? desktops) [
        "--set XNESTDM_XSESSION_DIRS ${sessionData.desktops}/share/xsessions"
      ]
      ++ lib.optionals (sessionData ? wrapper) [
        "--set XNESTDM_XSESSION_WRAPPER ${sessionData.wrapper}"
      ];
  });
in
{
  options.programs.xnestdm.enable = lib.mkEnableOption "xnestdm";

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.security.enableWrappers;
        message = "programs.xnestdm requires security.enableWrappers";
      }
    ];

    environment.systemPackages = [ package ];

    # xnestdm authenticates the selected account before using these privileges.
    # NixOS places this wrapper first in interactive users' PATH.
    security.wrappers.xnestdm = {
      source = lib.getExe package;
      owner = "root";
      group = "root";
      setuid = true;
    };

    security.pam.services.xnestdm = {
      startSession = true;
      # A nested login inherits the launcher's audit session, whose login UID
      # cannot be replaced even though the wrapper has an effective UID of 0.
      setLoginUid = false;
    };
  };
}
