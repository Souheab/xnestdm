# xnestdm

xnestdm is a small nested X11 display manager. It embeds a Xephyr X server in
a normal Qt window, discovers the X sessions supplied by the host, and starts
the selected session as the current user or as a PAM-authenticated local user.
The Qt login page and session toolbar use the platform's default Qt style.

xnestdm does not bundle a desktop environment. Standard host X session entries
are discovered from `xsessions` directories under `XDG_DATA_HOME` and
`XDG_DATA_DIRS`, with `/usr/local/share/xsessions` and `/usr/share/xsessions` as
fallbacks. The NixOS module also exposes the sessions configured through
`services.displayManager` and uses the host's X session wrapper.

If the host exposes no session entries, the **User X session** fallback tries
the selected user's executable `~/.xsession`, then `~/.xinitrc`, followed by a
system X session script under `/etc/X11`.

## Requirements

- Linux with an X11 desktop, or a Wayland desktop with working XWayland
- Nix with flakes enabled
- At least one host X11 session entry or a usable user/system X session script
- A usable host PAM service and local/NSS-visible accounts when switching users
- The NixOS module, or `sudo` for standalone alternate-user sessions

This is not a sandbox. A nested session uses the selected user's normal home,
configuration, devices, network, runtime services, and host permissions.
Xephyr is started with local access control disabled (`-ac`) but with TCP
listening disabled.

## Build and run

Build without privileges:

```console
nix build
```

Run without privileges to start a host session as the current user:

```console
nix run .
```

In this mode, username and password controls are disabled. To enable login as
another user, preserve the outer X connection and the host session catalog:

```console
sudo --preserve-env=DISPLAY,XAUTHORITY,XDG_DATA_DIRS nix run .
```

Before Qt is loaded, this starts a small privileged helper and permanently
drops the GUI process back to the original `sudo` user. The current-user button
therefore refers to that user rather than root. When the NixOS module is
enabled, its installed `xnestdm` command already knows the configured host
session directory and wrapper, so only the outer X credentials need to survive
`sudo`.

If `XAUTHORITY` is normally unset but the display cookie is stored in
`~/.Xauthority`, provide it explicitly before `sudo`:

```console
XAUTHORITY="$HOME/.Xauthority" sudo --preserve-env=DISPLAY,XAUTHORITY,XDG_DATA_DIRS nix run .
```

xnestdm forces Qt's `xcb` backend. On a Wayland desktop, `DISPLAY` must point to
XWayland. Native Wayland sessions are not listed or supported inside Xephyr.

When login as another user is enabled, xnestdm uses the `xnestdm` PAM service
when `/etc/pam.d/xnestdm` exists and otherwise falls back to `login`. Override
this for a host-specific policy with:

```console
sudo --preserve-env=DISPLAY,XAUTHORITY,XDG_DATA_DIRS nix run . -- --pam-service SERVICE
```

The standalone `login` fallback performs PAM authentication, account checks,
and credential setup, but intentionally skips that service's session hooks.
Traditional `login` policies commonly require `pam_loginuid`, which cannot
replace the audit login ID inherited through `sudo`. Enabling the NixOS module
provides the dedicated `xnestdm` policy and full PAM open/close session hooks.

Add `--verbose` after `--` to include Xephyr and nested-session diagnostics on
standard error.

### Clipboard sharing

During a nested session, open the settings cog in the top toolbar and enable
**Share clipboard with guest** to copy and paste plain text between the host
and guest. The option is off on first use and remembers later changes. Enabling
it sends the host's current clipboard text to the guest; disabling it stops
future synchronization without clearing either clipboard.

Clipboard sharing is bidirectional and deliberately opt-in because guest
applications can read host clipboard text while it is enabled. Images, files,
rich text, custom clipboard formats, and the X11 primary selection are not
shared.

### Session discovery overrides

`XNESTDM_XSESSION_DIRS` accepts a colon-separated list of additional host
`xsessions` directories. `XNESTDM_XSESSION_WRAPPER` accepts an optional host
session wrapper command that is placed before the selected session command.
These are primarily integration hooks for display-manager configuration; they
do not add desktop packages to xnestdm.

## NixOS module

The flake exports a module that installs xnestdm, connects it to the host's
configured X session catalog and wrapper, and creates a dedicated PAM service:

```nix
{
  inputs.xnestdm.url = "path:/path/to/xnestdm";

  outputs = { nixpkgs, xnestdm, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      modules = [
        xnestdm.nixosModules.default
        ({ ... }: { programs.xnestdm.enable = true; })
      ];
    };
  };
}
```

Apply the system configuration once (as with any NixOS system change):

```console
sudo nixos-rebuild switch --flake /path/to/config#my-host
```

After that, start `xnestdm` as your normal user. Both the current-user path and
password-authenticated local-user logins work without `sudo`:

```console
xnestdm
```

The module installs the package and PAM policy, connects the configured host X
sessions, and creates `/run/wrappers/bin/xnestdm-helper` as a setuid-root NixOS
security wrapper. The public `xnestdm` command, its Qt GUI, and Xephyr always
run as the invoking user. The helper contains no Qt code and is used only for
PAM authentication, starting the authenticated user's desktop with that user's
credentials, supervising it, and closing the PAM session at logout.

The GUI and helper communicate over a private socket for the lifetime of the
application. If the helper is unavailable, current-user sessions still work
and the username/password controls are disabled. The privileged helper ignores
caller-supplied Python, PAM-service, session-wrapper, QML, and Qt plugin
overrides; the module always uses its dedicated `xnestdm` PAM service. The
module does not install a desktop environment.

## Troubleshooting

- An empty catalog becomes **User X session**. If that also fails, make the
  desired desktop available through a standard X session `.desktop` entry or a
  user X session script.
- A session entry with a missing `TryExec` target is intentionally hidden.
- If alternate-user controls are disabled after enabling the module, apply the
  configuration with `nixos-rebuild switch` and check that
  `/run/wrappers/bin/xnestdm-helper` exists.
- Desktop startup, D-Bus, profile loading, and logout behavior come from the
  host's session entry and wrapper. xnestdm only launches and supervises them.
- **End Session** sends the nested session process group a graceful termination
  request, then forces cleanup if it does not exit. Logging out inside the
  desktop is still the preferred desktop-specific path.

## Development and checks

```console
nix develop
pytest
nix flake check
```

Automated tests mock PAM and child processes. Real current-user and alternate-
user logins remain manual checks because they intentionally use the host's PAM
policy, user database, and graphical sessions.
