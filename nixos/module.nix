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
      ]
      ++ [
        "--set XNESTDM_HELPER /run/wrappers/bin/xnestdm-helper"
        "--set XNESTDM_PAM_SERVICE xnestdm"
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

    # Only the non-Qt PAM/session helper receives privileges. The GUI and
    # embedded Xephyr server always run as the invoking user.
    security.wrappers.xnestdm-helper = {
      source = lib.getExe' package "xnestdm-helper";
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
