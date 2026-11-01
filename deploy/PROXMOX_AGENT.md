# Agent runbook: deploy CubeBar in an LXC on Proxmox

> **For the human.** Clone the repository on the Proxmox host and start the agent in it:
> ```bash
> ssh root@<proxmox>
> apt-get install -y git          # if git isn't on the host yet
> git clone https://github.com/olegbal/cubebar.git /root/cubebar
> cd /root/cubebar && claude      # "Follow deploy/PROXMOX_AGENT.md"
> ```

You are working **on a Proxmox VE host** as root. The task is to create one new LXC container,
install CubeBar in it, and run it as a systemd service so the cube shows Claude Code and Codex
limits around the clock, independently of the user's Mac.

## What CubeBar is

Repository: https://github.com/olegbal/cubebar (public). You are running in a clone of it on the host;
the code goes into the container through a separate `git clone`, so it can be updated there with `git pull`.

- `cubebar.py` calls `codexbar usage --json` (the CodexBar CLI), renders a 240×240 image and uploads it
  to a GeekMagic HelloCubic-Lite cube over the stock firmware's HTTP API (Photo Album theme). No reflashing.
- `deploy/install-lxc.sh` installs everything inside the container: Python + Pillow, curl, the DejaVu font,
  the CodexBar CLI (static musl build, sha256-verified), Codex CLI, Claude Code; it enables the claude and
  codex providers in CodexBar. It is idempotent — safe to re-run.
- The script was tested in a Debian 12 (aarch64) container: install, rendering, the systemd unit and pushing to the cube.
  It has not been run on x86_64 or on a real Proxmox yet — read its output carefully.

## Known facts

| What | Value |
|---|---|
| Cube | HelloCubic-Lite, firmware Cube-V7.1.24, IP `192.168.88.23` (network 192.168.88.0/24, most likely MikroTik) |
| CubeBar files on the cube | `/image/cubebar-a.jpg` and `/image/cubebar-b.jpg` in turn, ~24 KB each |
| Space on the cube | ~106 KB free of 3 MB — almost all taken by the user's GIFs |
| User's files on the cube | `/image/Fire__Item_.gif`, `/image/matrix-ezgif.com-optimize.gif` — **do not touch** |
| Crystal | The user lost the prism, the screen is viewed directly → run with `--mirror` (top-to-bottom flip). The user hasn't confirmed the axis yet |
| Time zone | Probably UTC+4, unconfirmed — take the host's zone and confirm with the user |
| Mac | CubeBar is not installed as a background service on the Mac. Only one pusher must run at a time |

## Rules

1. **Create exactly one new container.** Do not change, stop or delete existing CTs/VMs, host networking,
   firewall, storage or cluster settings.
2. **Before `pct create`, show the user the plan** (CTID, template, storage, bridge, resources, time zone,
   cube IP, `--mirror` option) and wait for an explicit "yes". Before `pct destroy` — a separate explicit "yes".
3. **Credentials.** Sign-in to Claude and Codex happens only through their own CLIs inside the container.
   Never copy `~/.claude/.credentials.json` or `~/.codex/auth.json` from other machines (refresh tokens
   rotate, one of the machines will get logged out), never print their contents, never write tokens to logs/files.
4. **Cube.** Touch only `cubebar-a.jpg`/`cubebar-b.jpg`. Do not delete the user's files, do not change
   cube settings by hand, and never open `/update` (firmware).
5. **On an error, stop and investigate**; don't improvise destructive fixes. Check the
   "If something goes wrong" table.
6. Run long commands (install takes ~2–4 min) with a sufficient timeout.

## Helper for commands inside the container

`pct exec` runs commands with a stripped environment (`HOME` may be missing, `~/.local/bin` is not in PATH).
Always use the wrapper:

```bash
CT=<ctid>
ct() { pct exec "$CT" -- env HOME=/root TERM=xterm \
  PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash -c "$*"; }
```

## Step 1. Reconnaissance (read-only)

```bash
pveversion
pct list                                     # is there already a CT with hostname cubebar?
pvesh get /cluster/nextid                    # free CTID
pvesm status --content vztmpl                # where templates live (usually local)
pvesm status --content rootdir               # where CT disks live (local-lvm / local-zfs)
pveam update && pveam available --section system | grep -E 'debian-1[23]-standard'
ip -br link | grep -E '^vmbr'                # bridges
timedatectl show -p Timezone --value         # host time zone
curl -s -m 5 http://192.168.88.23/v.json     # is the cube reachable from the host? expect {"m": "HelloCubic-Lite",...}
```

- If a CT with hostname `cubebar` already exists — don't create a new one; ask the user whether to update it
  (see "Updating") or recreate it.
- If existing CTs use special networking (VLAN tag, static IPs), look at `pct config <ctid>` of any working CT
  and copy its `net0` (bridge/tag) instead of guessing.
- If the cube doesn't respond from the host — tell the user: its IP may have changed (check DHCP leases on the router)
  or the host is on another network. Don't proceed until the cube is reachable from the network the container will be on.

Template: preferably the latest `debian-12-standard` (tested); `debian-13-standard` is acceptable.

## Step 2. Plan and confirmation

Show the user and wait for a "yes":

```
CTID:        <nextid>
Hostname:    cubebar
Template:    <template>  (template storage: <local>)
Disk:        <rootdir-storage>:6 GB
Resources:   1 vCPU, 1024 MB RAM, 512 MB swap, unprivileged, nesting=1, start on boot
Network:     net0 bridge=<vmbr0>, DHCP
Time zone:   <tz>
Cube:        192.168.88.23, --mirror mode (no crystal)
```

## Step 3. Create the container

```bash
TPL_STORE=<local>; ROOT_STORE=<local-lvm>; TEMPLATE=<debian-12-standard_..._amd64.tar.zst>; TZ=<tz>
pveam list "$TPL_STORE" | grep -q "$TEMPLATE" || pveam download "$TPL_STORE" "$TEMPLATE"

pct create "$CT" "$TPL_STORE:vztmpl/$TEMPLATE" \
  --hostname cubebar --unprivileged 1 --features nesting=1 \
  --cores 1 --memory 1024 --swap 512 --rootfs "$ROOT_STORE:6" \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --timezone "$TZ" --onboot 1 \
  --description "CubeBar: AI limits -> HelloCubic-Lite 192.168.88.23"
pct start "$CT"

for i in $(seq 30); do ct 'getent hosts github.com' >/dev/null && break; sleep 2; done
ct 'hostname -I; curl -s -m 5 http://192.168.88.23/v.json'
```

`nesting=1` is needed for systemd to work properly in an unprivileged Debian 12/13 container.

## Step 4. Code and install

```bash
ct 'apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates >/dev/null'
ct 'git clone https://github.com/olegbal/cubebar.git /root/cubebar'

ct "TZ_NAME=$TZ bash /root/cubebar/deploy/install-lxc.sh"     # ~2–4 minutes

ct 'codexbar --version; codex --version; claude --version; python3 -c "import PIL; print(\"Pillow\", PIL.__version__)"'
ct 'codexbar config providers | grep -E "^(claude|codex):"'   # both enabled
```

## Step 5. Sign in (done by the user)

Sign-in needs the user's browser. Don't enter codes or paste tokens on their behalf.

**Codex** — device flow; you may start it yourself in the background and pass the link and code from its output to the user:

```bash
ct 'codex login --device-auth'      # show the user the URL and one-time code, wait for it to finish
ct 'codex login status'
```

**Claude** — needs a code pasted back, so the user does it in their own terminal on the host:

```bash
pct enter <CT>
claude auth login          # open the link, sign in, paste the code back into the terminal
exit
```

Then verify (without printing the token files):

```bash
ct 'claude auth status --text'      # Login method: Claude Max account ...
ct 'codexbar usage'                 # Codex and Claude with Session/Weekly, no errors
ct 'codexbar usage --json' | grep -c '"error"'   # must be 0
```

If `codexbar usage` shows a scope/`user:profile` error for Claude, the user signed in with `claude setup-token`;
a regular `claude auth login` is required.

## Step 6. Check the cube and start the service

```bash
ct 'python3 /opt/cubebar/cubebar.py info --host 192.168.88.23'   # free should be > ~60000
ct 'python3 /opt/cubebar/cubebar.py preview --mirror --scale 2 -o /tmp/preview.png'
pct pull "$CT" /tmp/preview.png /tmp/cubebar-preview.png          # look at the image if you can read images
```

Confirm with the user that CubeBar isn't running on the Mac (if it was: `python3 cubebar.py uninstall-service` on the Mac).

```bash
ct 'python3 /opt/cubebar/cubebar.py install-service --host 192.168.88.23 --mirror'
# the first push takes ~15–30 s (codexbar polls the providers)
for i in $(seq 30); do ct 'journalctl -u cubebar --no-pager | grep -qE "pushed|error"' && break; sleep 3; done
ct 'systemctl is-active cubebar; journalctl -u cubebar -n 20 --no-pager'   # active and "pushed" lines
curl -s -m 10 'http://192.168.88.23/filelist?dir=/image/' | grep -oE "href='[^']+'"
```

Expect exactly one `cubebar-*.jpg` plus the user's files. Ask the user to look at the cube:

- text reads normally — done;
- text is mirrored left to right — reinstall the service with `--mirror h`;
- the user got the crystal back — reinstall without `--mirror`.

Reinstalling is the same `install-service` command with new flags (it rewrites the unit and restarts the service).

## Step 7. Report to the user

Briefly: the container's CTID and IP, what was installed (codexbar/codex/claude versions), service state,
what the cube shows, what's left for the user. Always recommend reserving the cube's IP
(`192.168.88.23`) for its MAC in the router's DHCP — otherwise the address may change after the cube reboots
and the service will start logging errors.

## Updating

```bash
ct 'git -C /root/cubebar pull && bash /root/cubebar/deploy/install-lxc.sh && systemctl restart cubebar'
# a different CodexBar version:
ct 'CODEXBAR_VERSION=<x.y.z> bash /root/cubebar/deploy/install-lxc.sh && systemctl restart cubebar'
```

Logs: `ct 'journalctl -u cubebar -f'`. Changing the cube IP or flags — run `install-service` again.

## Rollback (only when the user asks)

```bash
ct 'python3 /opt/cubebar/cubebar.py uninstall-service'
curl -s 'http://192.168.88.23/delete?file=%2Fimage%2Fcubebar-a.jpg'; curl -s 'http://192.168.88.23/delete?file=%2Fimage%2Fcubebar-b.jpg'
pct stop "$CT" && pct destroy "$CT"     # irreversible — only after an explicit "yes"
```

## If something goes wrong

| Symptom | Cause | What to do |
|---|---|---|
| `GLIBC_2.38 not found` | the glibc build of CodexBar got installed | the script installs the musl build — check that the current `install-lxc.sh` was run |
| `sign in required` on the cube | the provider's CLI isn't signed in inside the container | step 5 for that provider |
| `codexbar usage` for Claude: 401/expired | the token expired and wasn't refreshed | `ct 'claude auth status --text'`; if needed, the user repeats `claude auth login` |
| `curl ... 403`, timeouts, `No route to host` to the cube | the cube's IP changed, different VLAN/subnet, the cube is rebooting | check the IP on the router, `ping` from the CT, compare the CT's and cube's networks |
| `error:` in the journal every 2 minutes, then `pushed` | transient network/API failures | nothing, the service retries on its own |
| Wrong time in the image corner | the CT's time zone | `pct set $CT --timezone <tz>` and `systemctl restart cubebar` |
| `free` < 60 KB in `info` | the cube is low on space | tell the user; don't delete their GIFs without permission |
| systemd services in the CT fail with namespace errors | the container was created without `nesting=1` | `pct set $CT --features nesting=1`, `pct reboot $CT` |
| The image on the cube doesn't change though the journal says `pushed` | the cube's theme was switched by hand | `ct 'systemctl restart cubebar'` — theme 3 (Photo Album) is set when the service starts |
