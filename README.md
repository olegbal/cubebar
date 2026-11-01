# CubeBar

Claude Code / Codex / OpenCode (MiniMax, etc.) usage limits on a GeekMagic HelloCubic-Lite cube — like CodexBar, but on your desk.

Data comes from the [CodexBar](https://github.com/steipete/CodexBar) CLI (`codexbar usage --json`); a 240×240 image
is rendered on a Mac or Linux server and uploaded to the cube's stock firmware over its HTTP API (*Photo Album* theme).
No reflashing required.

## Commands

```bash
git clone https://github.com/olegbal/cubebar.git && cd cubebar
pip install -r requirements.txt                           # Pillow; on macOS you also need CodexBar: brew install --cask codexbar

python3 cubebar.py preview --scale 3                      # preview without the cube
python3 cubebar.py info --host 192.168.88.23              # check the connection to the cube
python3 cubebar.py push --host 192.168.88.23              # push once
python3 cubebar.py run --host 192.168.88.23               # refresh every 2 minutes
python3 cubebar.py install-service --host 192.168.88.23   # run in the background: launchd on macOS, systemd on Linux
python3 cubebar.py uninstall-service
```

Options:
- `--providers claude,codex,minimax` — filter and order;
- `--used` — show used instead of remaining;
- `--no-extra` — hide model-scoped windows such as "Fable only";
- `--mirror` — for a cube without its crystal: the firmware draws for the crystal's reflection, so the bare screen
  shows everything mirrored. `--mirror` (= `v`) flips top to bottom — for viewing from the front; `--mirror h` flips left to right;
- `--subsampling 2` — if the cube won't display a 4:4:4 JPEG;
- `install-service --print` — only print the unit/plist, install nothing.

## Proxmox (LXC)

You can hand the deployment to an agent on the Proxmox host: clone the repository on the host and give it
[deploy/PROXMOX_AGENT.md](deploy/PROXMOX_AGENT.md) — a step-by-step runbook with checks and guardrails.
Below is the same thing done by hand.

The CodexBar CLI works on Linux without a GUI. Browser cookies can't be read there, so Claude and Codex
authenticate through their own CLIs (OAuth), and MiniMax via an API key. Sign in on the server separately —
don't copy tokens from your Mac: both CLIs rotate refresh tokens, and one of the machines will get logged out.

1. A container on the Proxmox host (Debian 12 or 13, ~512 MB RAM), on the same network as the cube:

   ```bash
   pveam update && pveam available | grep debian
   pveam download local <debian-template>.tar.zst
   pct create 120 local:vztmpl/<debian-template>.tar.zst --hostname cubebar \
     --cores 1 --memory 512 --rootfs local-lvm:4 --unprivileged 1 --features nesting=1 \
     --net0 name=eth0,bridge=vmbr0,ip=dhcp --onboot 1
   pct start 120
   ```

2. Clone the project into the container (on the Proxmox host):

   ```bash
   pct exec 120 -- bash -c 'apt-get update && apt-get install -y git && git clone https://github.com/olegbal/cubebar.git /root/cubebar'
   ```

3. Inside the container (`pct enter 120`) — install the CodexBar CLI (static musl build, sha256-verified),
   Codex CLI, Claude Code, Python + Pillow, and the DejaVu font:

   ```bash
   TZ_NAME=Asia/Tbilisi bash /root/cubebar/deploy/install-lxc.sh
   ```

4. Sign in, check, start:

   ```bash
   claude                        # /login → open the link in a browser → /exit
   codex login --device-auth
   codexbar usage
   python3 /opt/cubebar/cubebar.py install-service --host 192.168.88.23 --mirror
   journalctl -u cubebar -f
   ```

Reserve the cube's IP in your router's DHCP, otherwise it may change after a reboot.

Updating: `cd /root/cubebar && git pull && bash deploy/install-lxc.sh && systemctl restart cubebar`.

## What's on the screen

- For each provider — its limit windows: `5h` (session), `7d` (week), and model-scoped windows.
- Percent remaining (green > 50%, yellow > 20%, red otherwise) and a countdown to the reset.
- A white tick on the bar marks where you "should" be at the current pace (pace from CodexBar): bar past the tick means you have headroom.
- Top right — time of the last update: if it's stale, the service is down or CodexBar isn't responding.
- `sign in required` — the provider's CLI isn't signed in on this machine.

As many providers are shown as are enabled in CodexBar (`codexbar config providers`).
Two fit in the regular layout; with more, rows switch to compact (label, bar, countdown,
and percent on one line), which fits 3–4 providers on the screen. Pages alternate on every refresh
only if even that doesn't fit. Unlimited windows (e.g. the MiniMax Plus weekly one) are hidden.

### Balance providers (Claude API)

The Claude API credit is a balance, not a limit, so it is shown in dollars: the header carries
`$86.34 / $100` and the row shows the expiry (`Nov 28`) instead of a percentage. The bar still
shows the fraction left.

The data comes from CodexBar's `claudeplatform` provider, which reads the Claude Console session.
On Linux the `sessionKey` cookie has to be pasted by hand (browser import is macOS-only):

```bash
codexbar config enable --provider claudeplatform
codexbar config set-source --provider claudeplatform --source web
printf '%s' 'sessionKey=...' | codexbar config set-cookie --provider claudeplatform --stdin
```

Get the cookie from DevTools → Application → Cookies → `platform.claude.com` → `sessionKey`.
It is a session cookie: when it expires the cube shows a sign-in error until a fresh one is pasted.

## How it talks to the cube

- `POST /doUpload?dir=/image/` (field `file`) — upload a JPEG
- `GET /set?theme=3` — Photo Album theme
- `GET /set?img=/image/<file>` — show a file
- `GET /delete?file=/image/<file>` — delete a file

Two files (`cubebar-a.jpg` / `cubebar-b.jpg`) are used in turn; your own photos in the album are left alone.
Each takes ~24 KB of the cube's flash — keep `free` in `info` above ~60 KB.

Requests to the cube go through `curl`: on macOS, Local Network privacy blocks LAN access for
pyenv/Homebrew Python (`No route to host`), while the system `/usr/bin/curl` is allowed — including from launchd.
