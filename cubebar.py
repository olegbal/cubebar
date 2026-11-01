#!/usr/bin/env python3
"""CubeBar: AI coding limits from CodexBar on a GeekMagic HelloCubic-Lite.

Reads usage with the `codexbar` CLI, renders a 240x240 JPEG and pushes it to
the cube's stock firmware (Photo Album theme) over its HTTP API.
"""

import argparse
import getpass
import hashlib
import io
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

SIZE = 240
PAD = 8
BG = (0, 0, 0)  # black is "transparent" on the crystal display
FG = (238, 238, 238)
DIM = (125, 125, 125)
TRACK = (38, 38, 42)
TICK = (255, 255, 255)
GOOD, WARN, BAD = (52, 211, 153), (251, 191, 36), (248, 113, 113)

PROVIDERS = {
    "claude": ("Claude", (217, 119, 87)),
    "codex": ("Codex", (122, 162, 247)),
    "opencode": ("OpenCode", (250, 178, 131)),
    "opencodego": ("OpenCode Go", (250, 178, 131)),
    "minimax": ("MiniMax", (242, 63, 93)),
    "gemini": ("Gemini", (66, 133, 244)),
    "cursor": ("Cursor", (200, 200, 200)),
    "copilot": ("Copilot", (160, 120, 255)),
}

# Row geometry (px). A provider block is a header row plus one row per window.
HEADER_H = 16
ROW_TEXT_H = 17
BAR_H = 7


@dataclass(frozen=True)
class Layout:
    provider_h: int
    row_h: int
    gap: int
    compact: bool


# Roomy: label/countdown/percent on one line, the bar below it.
ROOMY = Layout(provider_h=22, row_h=ROW_TEXT_H + 2 + BAR_H + 6, gap=6, compact=False)
# Compact: label, bar, countdown and percent on a single line, to fit more providers on one screen.
COMPACT = Layout(provider_h=20, row_h=17, gap=4, compact=True)

IMAGE_DIR = "/image/"
SLOT_NAMES = ("cubebar-a.jpg", "cubebar-b.jpg")
STATE_FILE = Path.home() / ".cache" / "cubebar" / "state.json"


# ---------------------------------------------------------------- data model

@dataclass
class Window:
    label: str
    used: float
    resets_at: datetime | None = None
    expected_used: float | None = None


@dataclass
class Provider:
    id: str
    name: str
    accent: tuple
    plan: str = ""
    windows: list[Window] = field(default_factory=list)
    error: str = ""

    def height(self, layout):
        return layout.provider_h + max(len(self.windows), 1) * layout.row_h


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_label(minutes):
    if not minutes:
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def plan_label(usage):
    plan = usage.get("loginMethod") or (usage.get("identity") or {}).get("loginMethod") or ""
    plan = plan.removeprefix("Claude ").strip()
    return plan[:1].upper() + plan[1:] if plan else ""


def parse_entry(entry, include_extra=True):
    pid = entry.get("provider", "?")
    name, accent = PROVIDERS.get(pid, (pid.title(), (200, 200, 200)))
    usage = entry.get("usage") or {}
    pace = entry.get("pace") or {}
    p = Provider(pid, name, accent, plan_label(usage))

    unlimited = []
    for key in ("primary", "secondary", "tertiary"):
        w = usage.get(key)
        if not w or w.get("usedPercent") is None:
            continue
        window = Window(
            label=window_label(w.get("windowMinutes")),
            used=float(w["usedPercent"]),
            resets_at=parse_time(w.get("resetsAt")),
            expected_used=(pace.get(key) or {}).get("expectedUsedPercent"),
        )
        # An unlimited window (e.g. MiniMax Plus weekly) is always 100% left: skip it unless it's all there is.
        if (w.get("resetDescription") or "").strip().lower() == "unlimited":
            unlimited.append(window)
        else:
            p.windows.append(window)
    p.windows = p.windows or unlimited
    if include_extra:
        for x in usage.get("extraRateWindows") or []:
            w = x.get("window") or {}
            if w.get("usedPercent") is None:
                continue
            title = (x.get("title") or "extra").replace(" only", "")
            p.windows.append(Window(title[:7], float(w["usedPercent"]), parse_time(w.get("resetsAt"))))

    if not p.windows:
        err = entry.get("error")
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err)
        p.error = short_error(str(err or "no data"))
    return p


def short_error(message):
    # What codexbar reports when the provider's CLI was never logged in on this machine.
    if "authentication required" in message or "No available fetch strategy" in message:
        return "sign in required"
    return message


def find_codexbar():
    found = shutil.which("codexbar")
    if found:
        return found
    # Services get a minimal PATH without Homebrew or ~/.local/bin.
    for candidate in ("/opt/homebrew/bin/codexbar", "/usr/local/bin/codexbar",
                      str(Path.home() / ".local" / "bin" / "codexbar")):
        if os.access(candidate, os.X_OK):
            return candidate
    sys.exit("codexbar CLI not found: `brew install --cask codexbar` on macOS, deploy/install-lxc.sh on Linux")


def fetch_usage(args):
    if args.from_json:
        return json.loads(Path(args.from_json).read_text())
    cmd = [find_codexbar(), "usage", "--json", "--no-color"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"codexbar exited {res.returncode}: {res.stderr.strip()[:300]}")


def load_providers(args):
    entries = fetch_usage(args)
    providers = [parse_entry(e, not args.no_extra) for e in entries]
    if args.providers:
        order = [s.strip() for s in args.providers.split(",") if s.strip()]
        providers = sorted((p for p in providers if p.id in order), key=lambda p: order.index(p.id))
    return providers


# ----------------------------------------------------------------- rendering

SF_FONT = "/System/Library/Fonts/SFNS.ttf"  # macOS variable font, weights by name
STATIC_FONTS = [  # Linux (regular, bold): apt install fonts-dejavu-core
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
]
BOLD_WEIGHTS = {"Semibold", "Bold"}


class Fonts:
    def __init__(self):
        self.cache = {}

    def get(self, size, weight="Regular"):
        key = (size, weight)
        if key not in self.cache:
            self.cache[key] = self._open(size, weight)
        return self.cache[key]

    @staticmethod
    def _open(size, weight):
        # BASIC layout keeps hinted glyph advances: raqm spaces digits unevenly at these sizes.
        basic = ImageFont.Layout.BASIC
        if os.path.exists(SF_FONT):
            font = ImageFont.truetype(SF_FONT, size, layout_engine=basic)
            font.set_variation_by_name(weight)
            return font
        for regular, bold in STATIC_FONTS:
            path = bold if weight in BOLD_WEIGHTS else regular
            if os.path.exists(path):
                # Static fonts are a bit wider than SF; shave a pixel to keep rows fitting.
                return ImageFont.truetype(path, size - 1, layout_engine=basic)
        try:
            return ImageFont.load_default(size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()


FONTS = Fonts()


def fmt_countdown(resets_at, now):
    if not resets_at:
        return ""
    secs = int((resets_at - now).total_seconds())
    if secs <= 0:
        return "now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h{mins:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def level_color(left):
    if left > 50:
        return GOOD
    if left > 20:
        return WARN
    return BAD


def text_right(draw, x_right, y, text, font, fill):
    draw.text((x_right - draw.textlength(text, font=font), y), text, font=font, fill=fill)
    return x_right - draw.textlength(text, font=font)


def draw_window(draw, y, w, now, show_used):
    left = max(0.0, min(100.0, 100.0 - w.used))
    value = w.used if show_used else left
    color = level_color(left)
    x0, x1 = PAD, SIZE - PAD

    draw.text((x0, y), w.label, font=FONTS.get(14, "Medium"), fill=DIM)
    x = text_right(draw, x1, y - 1, f"{value:.0f}%", FONTS.get(16, "Bold"), color)
    countdown = fmt_countdown(w.resets_at, now)
    if countdown:
        text_right(draw, x - 8, y + 1, countdown, FONTS.get(13), DIM)

    by = y + ROW_TEXT_H + 2
    draw.rounded_rectangle((x0, by, x1, by + BAR_H), radius=BAR_H // 2, fill=TRACK)
    fill_w = (x1 - x0) * value / 100
    if fill_w >= 2:
        draw.rounded_rectangle((x0, by, x0 + fill_w, by + BAR_H), radius=BAR_H // 2, fill=color)
    if w.expected_used is not None:
        expected = w.expected_used if show_used else 100 - w.expected_used
        tx = x0 + (x1 - x0) * max(0, min(100, expected)) / 100
        draw.rectangle((tx - 1, by - 2, tx, by + BAR_H + 2), fill=TICK)


def draw_window_compact(draw, y, w, now, show_used):
    left = max(0.0, min(100.0, 100.0 - w.used))
    value = w.used if show_used else left
    color = level_color(left)
    x0, x1 = PAD, SIZE - PAD
    # Fixed columns so bars and numbers line up across rows: label | bar | countdown | percent.
    pct_right, cd_right = x1, x1 - 47
    bar_x0, bar_x1 = x0 + 42, cd_right - 43

    draw.text((x0, y + 1), w.label, font=FONTS.get(13, "Medium"), fill=DIM)
    text_right(draw, pct_right, y, f"{value:.0f}%", FONTS.get(14, "Bold"), color)
    countdown = fmt_countdown(w.resets_at, now)
    if countdown:
        text_right(draw, cd_right, y + 2, countdown, FONTS.get(12), DIM)

    bh = BAR_H - 1
    by = y + 6
    draw.rounded_rectangle((bar_x0, by, bar_x1, by + bh), radius=bh // 2, fill=TRACK)
    fill_w = (bar_x1 - bar_x0) * value / 100
    if fill_w >= 2:
        draw.rounded_rectangle((bar_x0, by, bar_x0 + fill_w, by + bh), radius=bh // 2, fill=color)
    if w.expected_used is not None:
        expected = w.expected_used if show_used else 100 - w.expected_used
        tx = bar_x0 + (bar_x1 - bar_x0) * max(0, min(100, expected)) / 100
        draw.rectangle((tx - 1, by - 2, tx, by + bh + 2), fill=TICK)


def draw_provider(draw, y, p, now, show_used, layout):
    if layout.compact:
        draw.ellipse((PAD, y + 5, PAD + 8, y + 13), fill=p.accent)
        draw.text((PAD + 14, y), p.name, font=FONTS.get(15, "Bold"), fill=FG)
        if p.plan:
            text_right(draw, SIZE - PAD, y + 2, p.plan, FONTS.get(12, "Medium"), DIM)
    else:
        draw.ellipse((PAD, y + 6, PAD + 8, y + 14), fill=p.accent)
        draw.text((PAD + 14, y), p.name, font=FONTS.get(17, "Bold"), fill=FG)
        if p.plan:
            text_right(draw, SIZE - PAD, y + 3, p.plan, FONTS.get(13, "Medium"), DIM)
    y += layout.provider_h
    if p.error:
        lines = textwrap.wrap(p.error, 32, max_lines=1 if layout.compact else 2, placeholder="…")
        for i, line in enumerate(lines):
            draw.text((PAD, y + 2 + i * 15), line, font=FONTS.get(13), fill=BAD)
        return
    draw_row = draw_window_compact if layout.compact else draw_window
    for w in p.windows:
        draw_row(draw, y, w, now, show_used)
        y += layout.row_h


def paginate(providers, layout):
    """Pack providers into pages that fit below the header."""
    budget = SIZE - HEADER_H - 2 * 4
    pages, page, used = [], [], 0
    for p in providers:
        need = p.height(layout) + (layout.gap if page else 0)
        if page and used + need > budget:
            pages.append(page)
            page, used, need = [], 0, p.height(layout)
        page.append(p)
        used += need
    if page:
        pages.append(page)
    return pages or [[]]


def plan_pages(providers):
    """Prefer one screen: roomy rows, else compact rows; page roomy rows only if even compact overflows."""
    for layout in (ROOMY, COMPACT):
        pages = paginate(providers, layout)
        if len(pages) == 1:
            return pages, layout
    return paginate(providers, ROOMY), ROOMY


def render(page, layout, page_no=0, pages=1, show_used=False):
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    now = datetime.now(timezone.utc)

    title = "USED" if show_used else "LEFT"
    if pages > 1:
        title += f"  {page_no + 1}/{pages}"
    draw.text((PAD, 2), title, font=FONTS.get(12, "Semibold"), fill=DIM)
    text_right(draw, SIZE - PAD, 2, datetime.now().strftime("%H:%M"), FONTS.get(12, "Semibold"), DIM)

    if not page:
        draw.text((PAD, SIZE // 2 - 10), "no providers", font=FONTS.get(16), fill=DIM)
        return img

    content_h = sum(p.height(layout) for p in page) + layout.gap * (len(page) - 1)
    y = HEADER_H + max(4, (SIZE - HEADER_H - content_h) // 2)
    for p in page:
        draw_provider(draw, y, p, now, show_used, layout)
        y += p.height(layout) + layout.gap
    return img


def orient(img, mirror):
    """Pre-flip for viewing the bare screen without the crystal.

    The screen lies face up under a 45-degree beam splitter, so the firmware
    draws for a top-bottom reflection; seen directly from the front the image
    needs a vertical flip ("v"), seen from behind a horizontal one ("h").
    """
    if mirror == "v":
        return ImageOps.flip(img)
    if mirror == "h":
        return ImageOps.mirror(img)
    return img


def to_jpeg(img, subsampling):
    buf = io.BytesIO()
    # Baseline JPEG: the firmware's decoder does not handle progressive files.
    img.save(buf, "JPEG", quality=92, subsampling=subsampling, progressive=False, optimize=False)
    return buf.getvalue()


# ---------------------------------------------------------------- cube API

class Cube:
    """HTTP client for the stock firmware.

    Goes through /usr/bin/curl: macOS Local Network privacy blocks LAN access
    for non-Apple interpreters (pyenv/Homebrew Python get EHOSTUNREACH), while
    the Apple-signed curl is allowed, including from launchd agents. On Linux
    this is just the distro curl.
    """

    def __init__(self, host, timeout=15):
        self.base = host if host.startswith("http") else f"http://{host}"
        self.timeout = timeout

    def _curl(self, url, *extra, data=None, timeout=None):
        cmd = ["/usr/bin/curl", "-sS", "--fail", "-m", str(timeout or self.timeout), *extra, url]
        res = subprocess.run(cmd, input=data, capture_output=True)
        if res.returncode:
            raise OSError(f"curl exit {res.returncode}: {res.stderr.decode().strip()}")
        return res.stdout.decode("utf-8", "replace")

    def get(self, path, **params):
        return self._curl(self.base + path + ("?" + urllib.parse.urlencode(params) if params else ""))

    def upload(self, name, data, directory=IMAGE_DIR):
        return self._curl(
            f"{self.base}/doUpload?dir={urllib.parse.quote(directory)}",  # slashes kept, as the web UI sends
            "-H", "Expect:",  # ESP8266WebServer does not answer 100-continue
            "-F", f"file=@-;filename={name};type=image/jpeg",
            data=data,
            timeout=self.timeout * 3,
        )

    def show(self, jpeg, state):
        """Upload into the free slot, display it, then drop the previous slot.

        Two alternating names avoid overwriting the file the firmware is
        currently drawing and force it to reload the picture.
        """
        prev = state.get("slot")
        name = SLOT_NAMES[1] if prev == SLOT_NAMES[0] else SLOT_NAMES[0]
        self.upload(name, jpeg)
        self.get("/set", img=IMAGE_DIR + name)
        # Without saved state (first run, or another machine pushed before) the
        # other slot may still hold an old picture; drop it quietly.
        stale = prev or next(s for s in SLOT_NAMES if s != name)
        try:
            self.get("/delete", file=IMAGE_DIR + stale)
        except OSError as e:
            if prev:
                log(f"delete {stale} failed: {e}")
        state["slot"] = name


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- commands

def cmd_preview(args):
    pages, layout = plan_pages(load_providers(args))
    out = Path(args.output)
    for i, page in enumerate(pages):
        img = orient(render(page, layout, i, len(pages), args.used), args.mirror)
        path = out if len(pages) == 1 else out.with_stem(f"{out.stem}-{i + 1}")
        if args.scale > 1:
            img = img.resize((SIZE * args.scale, SIZE * args.scale), Image.NEAREST)
        img.save(path)
        print(path)


def cmd_info(args):
    cube = Cube(args.host)
    for path in ("/v.json", "/app.json", "/album.json", "/space.json"):
        try:
            print(f"{path}: {cube.get(path).strip()}")
        except OSError as e:
            print(f"{path}: ERROR {e}")


def push_once(cube, args, state, tick=0):
    pages, layout = plan_pages(load_providers(args))
    page_no = tick % len(pages)
    img = orient(render(pages[page_no], layout, page_no, len(pages), args.used), args.mirror)
    jpeg = to_jpeg(img, args.subsampling)
    digest = hashlib.sha1(jpeg).hexdigest()
    if digest == state.get("digest"):
        return False
    if not state.get("theme_set"):
        cube.get("/set", theme=3)  # Photo Album
        state["theme_set"] = True
    cube.show(jpeg, state)
    state["digest"] = digest
    save_state(state)
    return True


def cmd_push(args):
    state = load_state()
    state.pop("theme_set", None)
    changed = push_once(Cube(args.host), args, state)
    log("pushed" if changed else "unchanged, skipped")


def cmd_run(args):
    cube, state, tick = Cube(args.host), load_state(), 0
    state.pop("theme_set", None)
    log(f"cubebar -> {cube.base}, every {args.interval}s")
    while True:
        try:
            if push_once(cube, args, state, tick):
                log("pushed")
        except Exception as e:  # keep the daemon alive across network/CLI hiccups
            log(f"error: {e}")
            state.pop("theme_set", None)
        tick += 1
        time.sleep(args.interval)


SERVICE_NAME = "cubebar"
LAUNCHD_LABEL = "com.cubebar.agent"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def service_command(args):
    program = [sys.executable, str(Path(__file__).resolve()), "run", "--host", args.host,
               "--interval", str(args.interval)]
    if args.providers:
        program += ["--providers", args.providers]
    if args.used:
        program.append("--used")
    if args.no_extra:
        program.append("--no-extra")
    if args.mirror:
        program += ["--mirror", args.mirror]
    if args.subsampling:
        program += ["--subsampling", str(args.subsampling)]
    return program


def service_path():
    """PATH for the service: codexbar spawns `claude`/`codex` to refresh their logins."""
    dirs = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
            *os.environ.get("PATH", "").split(os.pathsep), "/usr/bin", "/bin"]
    return os.pathsep.join(dict.fromkeys(d for d in dirs if d))


def systemd_unit():
    """Unit path and systemctl scope: a system unit as root (LXC), a user unit otherwise."""
    if os.geteuid() == 0:
        return Path("/etc/systemd/system") / f"{SERVICE_NAME}.service", []
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service", ["--user"]


def install_systemd(program, dry_run):
    unit_path, scope = systemd_unit()
    user_line = f"User={getpass.getuser()}\n" if not scope else ""  # User= also sets HOME
    unit = (
        "[Unit]\n"
        "Description=CubeBar: AI coding limits on HelloCubic-Lite\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={shlex.join(program)}\n"
        f"{user_line}"
        f'Environment="PATH={service_path()}"\n'
        "Restart=always\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        f"WantedBy={'default.target' if scope else 'multi-user.target'}\n"
    )
    if dry_run:
        print(f"# {unit_path}\n{unit}", end="")
        return
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit)
    systemctl = ["systemctl", *scope]
    subprocess.run([*systemctl, "daemon-reload"], check=True)
    subprocess.run([*systemctl, "enable", SERVICE_NAME], check=True)
    subprocess.run([*systemctl, "restart", SERVICE_NAME], check=True)
    print(f"installed {unit_path}\nlogs: {shlex.join(['journalctl', *scope, '-u', SERVICE_NAME, '-f'])}")
    if scope:
        print(f"to keep it running after logout: sudo loginctl enable-linger {getpass.getuser()}")


def install_launchd(program, dry_run):
    log_path = Path.home() / "Library" / "Logs" / "cubebar.log"
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": program,
        "EnvironmentVariables": {"PATH": service_path()},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if dry_run:
        print(plistlib.dumps(plist).decode(), end="")
        return
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCHD_PLIST)], check=True)
    print(f"installed {LAUNCHD_PLIST}\nlogs: {log_path}")


def cmd_install_service(args):
    install = install_launchd if sys.platform == "darwin" else install_systemd
    install(service_command(args), args.print)


def cmd_uninstall_service(args):
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], capture_output=True)
        LAUNCHD_PLIST.unlink(missing_ok=True)
        print(f"removed {LAUNCHD_PLIST}")
        return
    unit_path, scope = systemd_unit()
    subprocess.run(["systemctl", *scope, "disable", "--now", SERVICE_NAME], capture_output=True)
    unit_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", *scope, "daemon-reload"], capture_output=True)
    print(f"removed {unit_path}")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--providers", help="comma-separated filter and order, e.g. claude,codex,minimax")
    common.add_argument("--used", action="store_true", help="show used %% instead of remaining")
    common.add_argument("--no-extra", action="store_true", help="hide model-scoped windows (e.g. Fable only)")
    common.add_argument("--from-json", help="read a saved `codexbar usage --json` dump instead of running it")
    common.add_argument("--mirror", nargs="?", const="v", choices=("v", "h"),
                        help="flip for a bare screen without the crystal: v=top-bottom (default), h=left-right")

    device = argparse.ArgumentParser(add_help=False)
    device.add_argument("--host", default=os.environ.get("CUBE_HOST"), required="CUBE_HOST" not in os.environ,
                        help="cube IP or hostname (or set CUBE_HOST)")
    device.add_argument("--subsampling", type=int, default=0, choices=(0, 1, 2),
                        help="JPEG chroma subsampling: 0=4:4:4 (sharp), 2=4:2:0 (safest)")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preview", parents=[common], help="render to a local PNG")
    p.add_argument("-o", "--output", default="cubebar-preview.png")
    p.add_argument("--scale", type=int, default=1, help="upscale factor for easier viewing")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("info", parents=[device], help="query the cube")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("push", parents=[common, device], help="render and push once")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("run", parents=[common, device], help="push in a loop")
    p.add_argument("--interval", type=int, default=120, help="seconds between refreshes")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("install-service", aliases=["install-agent"], parents=[common, device],
                       help="run in the background: launchd agent on macOS, systemd unit on Linux")
    p.add_argument("--interval", type=int, default=120)
    p.add_argument("--print", action="store_true", help="only print the unit/plist, install nothing")
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser("uninstall-service", aliases=["uninstall-agent"], help="remove the background service")
    p.set_defaults(func=cmd_uninstall_service)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
