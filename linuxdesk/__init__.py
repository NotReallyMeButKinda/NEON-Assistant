"""
linuxdesk -- NEON on Linux (Arch first; Hyprland, KDE Plasma, and other Wayland or X11 desktops).

The Windows-specific modules (system_control, windows, dictation, selection, clipboard, hotkeys, media,
notifications, secrets_store, startup, shortcuts, filesearch, sysinfo) keep their public functions; on Linux
they hand the work to the modules here. Everything here talks to the system through ordinary command-line
tools (wpctl, playerctl, hyprctl, wl-clipboard, grim...) via osinfo.run(), so there are no extra Python
packages, and tests can fake every command.

  apps      .desktop files: the app catalogue, launching, default apps, autostart and menu shortcuts
  system    volume, power, lock, screenshots, brightness, uptime, disk space, the trash, CPU / RAM / battery
  wm        windows: list, focus, close, minimize, fullscreen (Hyprland: hyprctl; KDE: kdotool)
  input     typing and key presses (wtype on wlroots compositors like Hyprland; ydotool elsewhere)
  clip      the clipboard and the primary selection (wl-clipboard; xclip on X11)
  media     now playing and play / pause / next through MPRIS (playerctl)
  notify    reading notifications off D-Bus, and do-not-disturb for the common notification daemons
  secrets   the Secret Service (KWallet / GNOME Keyring) through secret-tool
  files     file search through plocate
  hypr      Hyprland specifics: keybinds, window rules, reserved screen space, border colours
"""
