# Userdesk

Userdesk is a PySide6 application that embeds a Xephyr X server in a normal Qt
window and starts an XFCE session inside it as the current user or as a
PAM-authenticated local user.
The Qt login page and session toolbar use the platform's default Qt style.

## Requirements

- Linux with an X11 desktop, or a Wayland desktop with working XWayland
- Nix with flakes enabled
- A usable host PAM service and local/NSS-visible accounts when switching users
- Optional `sudo` access for authenticating and switching to another user

This is not a sandbox. The nested session uses the selected user's normal home,
configuration, devices, network, and host permissions. Xephyr is started with
local access control disabled (`-ac`) but with TCP listening disabled.

## Build and run

Build without privileges:

```console
nix build
```

Run without privileges to start XFCE as your current user:

```console
nix run .
```

In this mode, the username and password controls are disabled. To enable login
as another user, run while preserving the outer X connection:

```console
sudo --preserve-env=DISPLAY,XAUTHORITY nix run .
```

The current-user button still refers to the original `sudo` invoker in this
mode, rather than to root.

If `XAUTHORITY` is normally unset but your display cookie is stored in
`~/.Xauthority`, provide it explicitly before `sudo`:

```console
XAUTHORITY="$HOME/.Xauthority" sudo --preserve-env=DISPLAY,XAUTHORITY nix run .
```

Userdesk forces Qt's `xcb` backend. On a Wayland desktop, `DISPLAY` must point to
XWayland; native Wayland embedding is not supported.

When login as another user is enabled, the application uses the `userdesk` PAM
service when `/etc/pam.d/userdesk` exists and otherwise falls back to `login`.
Override this for a host-specific policy with:

```console
sudo --preserve-env=DISPLAY,XAUTHORITY nix run . -- --pam-service SERVICE
```

The standalone `login` fallback performs PAM authentication, account checks,
and credential setup, but intentionally skips that service's session hooks.
Traditional `login` policies commonly require `pam_loginuid`, which cannot
replace the audit login ID inherited through `sudo`. Enabling the NixOS module
provides the dedicated `userdesk` policy and full PAM open/close session hooks.

Add `--verbose` after `--` to include Xephyr/XFCE diagnostics on standard error.

## NixOS module

The flake exports a module that installs the package and creates a dedicated PAM
service using the machine's normal PAM account and session rules:

```nix
{
  inputs.userdesk.url = "path:/path/to/userdesk";

  outputs = { nixpkgs, userdesk, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      modules = [
        userdesk.nixosModules.default
        ({ ... }: { programs.userdesk.enable = true; })
      ];
    };
  };
}
```

The module does not install a setuid GUI. Start the installed `userdesk` command
normally for a current-user session, or through `sudo` to enable switching users.

## Development and checks

```console
nix develop
pytest
nix flake check
```

Automated tests mock PAM and child processes. A real login remains a manual test
because it intentionally uses the host's PAM policy and user database.
