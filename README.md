# low-bandwidth

Local bandwidth monitor with both a browser dashboard and a terminal TUI for spotting which processes are using the most bandwidth right now and shutting them down quickly when you are on a constrained connection.

## What it does

- Samples live per-process network traffic on macOS with `nettop`
- Samples live per-process network traffic on Linux with `nethogs`
- Ranks the top bandwidth users over a rolling one-minute average built from short live samples
- Shows the socket ports each process is using when the platform collector can resolve them
- Lets you stop or force-stop individual processes
- Includes a few platform-specific preset actions for common bandwidth drains:
  - pause iCloud sync daemons
  - pause App Store download daemons
  - disable or re-enable background system update checks
  - open System Settings or App Store for the cases Apple only exposes in the GUI

## Run it

Recommended setup from the repo:

```bash
./scripts/bootstrap.sh
```

That runs `uv sync` and, on Linux, installs the `nethogs` system package.

If you want an installable CLI:

```bash
python3 -m pip install -e .
low-bandwidth
```

If you just want to run the web dashboard from the repo:

```bash
python3 run.py
```

Then open `http://127.0.0.1:8421`.

If you want the terminal interface instead:

```bash
python3 run.py --ui tui
```

If you want to run the whole app under `sudo` without losing the terminal settings that curses needs:

```bash
./scripts/run-with-sudo.sh --ui tui
```

If Linux collection is still behaving unexpectedly, dump a one-shot snapshot plus collector debug details:

```bash
python3 run.py --dump-snapshot
```

With the installed CLI:

```bash
low-bandwidth --ui tui
```

## TUI controls

- `j` / `k` or arrow keys: move through processes
- `t`: send `SIGTERM` to the selected process
- `x`: send `SIGKILL` to the selected process
- `a` through `c`: toggle one of the macOS restart blockers shown in the TUI header
- `h`: toggle hiding rows below `1KB` total traffic
- `q`: quit

## Notes

- macOS uses `nettop` and Linux uses `nethogs`. Other platforms still show the UI, but live collection is unsupported.
- `nettop` and `nethogs` are both sampled in short bursts, then combined into a rolling 60-second average so processes do not blink in and out immediately.
- Linux support requires `nethogs` to be installed and to have enough packet-capture privileges to observe traffic.
- Built-in presets are platform-specific. Linux intentionally hides the macOS-only iCloud/App Store actions instead of showing irrelevant controls.
- On Linux, the app will retry `nethogs` with `sudo -n` after a permission error. Run `sudo -v` first if you want that non-interactive retry to succeed.
- If you prefer to manage dependencies separately, `uv sync` still only handles the Python environment. Use `./scripts/install-linux-deps.sh` to install `nethogs` on Linux.
- The macOS blocker presets now try to disable the relevant `launchd` services before stopping matching processes, so they are meant to survive immediate restarts better than a plain kill.
- `softwareupdate --schedule on|off` may prompt for admin rights depending on how you launch the app.
