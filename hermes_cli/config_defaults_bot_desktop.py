"""Default configuration for Bot Desktop and its profile-scoped handoff viewer."""

BOT_DESKTOP_DEFAULTS = {
    # Headless Xfce screen per profile on the gateway host, streamed to Hermes Desktop where a human can
    # watch, take over (logins, 2FA, CAPTCHAs) and hand back. `hermes computer-use screen`.
    "geometry": "1440x900",
    # Opt-in: start the screen automatically the first time computer_use needs a display on a headless
    # host. Off by default so installing TigerVNC for other reasons never yields a screen nobody asked
    # for; Hermes Desktop's Screen pane offers Start and this toggle.
    "auto_start": False,
    # Refuse to start below this much free memory (MB), measured on the host or its container cgroup,
    # whichever is tighter. Xvnc + Xfce idle at ~220 MB and a takeover's browser adds 0.5-1 GB, so a
    # screen with one page runs past 1 GB; the kernel OOM killer picks its victim by score, so on a
    # small instance the loser is the dashboard or the gateway rather than the desktop. 0 disables the
    # check.
    "min_free_memory_mb": 1536,
    # Stop a screen nobody has used (no computer_use action, browser spawn, viewer or takeover) for this
    # long; it restarts on the next use. Idle Xvnc + Xfce hold ~220 MB, an abandoned browser far more.
    # 0 keeps screens up until stopped.
    "idle_stop_minutes": 30,
    # Where the screen (and with it computer_use and the bot's browser) runs. auto: follow the terminal backend
    # (inside a docker / ssh / streaming-plugin sandbox, on the gateway host for a local terminal; a backend that
    # cannot host a screen, e.g. modal/daytona/vercel, REFUSES). terminal: always inside the terminal backend.
    # gateway: always on the gateway host, even beside a sandbox terminal (explicit opt-in). ssh: on the
    # bot_desktop.ssh host whatever terminal.backend is; host + user required (incomplete refuses), key empty =
    # ssh client defaults, one login + display per agent, nothing from the gateway's home is copied there.
    # Sandbox images need the desktop stack: nousresearch/hermes-sandbox:desktop.
    "placement": "auto",
    "ssh": {"host": "", "user": "", "port": 22, "key": "", "display": 20},
    # Browser handoff is disabled until a browser-facing URL is configured. The viewer server should be
    # reachable only through an authenticated LAN/VPN reverse proxy.
    "handoff": {
        "public_url": "",  # Browser-facing base URL for this profile; empty disables handoff links.
        "bind_host": "127.0.0.1",  # Interface for `hermes computer-use handoff-serve`.
        "port": 8650,
        "allowed_clients": [],  # Source IPs/CIDRs; empty permits loopback only.
        "allowed_origins": [],  # Exact browser Origins; empty requires same-origin.
        "novnc_dir": "/usr/share/novnc",  # noVNC static install root; Debian/Ubuntu package path.
        "link_ttl_seconds": 600,  # Time to first open; link tokens are single-use.
        "session_max_seconds": 1800,  # Default upper bound for a viewer session.
    },
}
