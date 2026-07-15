# Userdesk

Userdesk is a PySide6 application that embeds a Xephyr X server in a normal Qt
window and starts an XFCE session inside it as a PAM-authenticated local user.
The Qt login page and session toolbar use the platform's default Qt style.

## Requirements

- Linux with an X11 desktop, or a Wayland desktop with working XWayland
- Nix with flakes enabled
- A usable host PAM service and local/NSS-visible user accounts
- `sudo` access, because authentication and switching UID/GID require root

This is not a sandbox. The nested session uses the selected user's normal home,
configuration, devices, network, and host permissions. Xephyr is started with
local access control disabled (`-ac`) but with TCP listening disabled.

## Build and run

Build without privileges:

```console
nix build
```

Run from the repository while preserving the outer X connection:

```console
sudo --preserve-env=DISPLAY,XAUTHORITY nix run .
```

If `XAUTHORITY` is normally unset but your display cookie is stored in
`~/.Xauthority`, provide it explicitly before `sudo`:

```console
XAUTHORITY="$HOME/.Xauthority" sudo --preserve-env=DISPLAY,XAUTHORITY nix run .
```

Userdesk forces Qt's `xcb` backend. On a Wayland desktop, `DISPLAY` must point to
XWayland; native Wayland embedding is not supported.

By default, the application uses the `userdesk` PAM service when
`/etc/pam.d/userdesk` exists and otherwise falls back to `login`. Override this
for a host-specific policy with:

```console
sudo --preserve-env=DISPLAY,XAUTHORITY nix run . -- --pam-service SERVICE
```

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
through `sudo` in the same way as the flake app.

## Development and checks

```console
nix develop
pytest
nix flake check
```

Automated tests mock PAM and child processes. A real login remains a manual test
because it intentionally uses the host's PAM policy and user database.
