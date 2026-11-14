#!/usr/bin/env python3
"""nguonc-dl - download movies and series from phim.nguonc.com.

How it works
------------
1. Movie page      scrape()              title, year and the episode list of every server.
2. Stream link     open_episode()        the embed player hands out an AES-GCM encrypted bootstrap
                                         over HTTP/2, which leads to an encrypted HLS playlist.
3. Quality         pick_variant()        the highest quality by default (-q picks another one).
4. Clean playlist  clean_playlist()      drops the ad segments spliced into the movie.
5. Size            estimate_size()       measures a few segments spread over the episode;
                   probe_resolution()    ffmpeg reads the real resolution from one segment.
6. Download        download_aria2()      aria2c, many segments at once, broken segments are fetched
                                         again (falls back to yt-dlp).
7. Mux             make_mp4()            the segments are piped into ffmpeg, which writes the MP4
                                         without re-encoding.

Code map
--------
Console output   log(), colors, class Board (live progress bars)   (all user-facing text goes through t())
HTTP             http_get(), h2_request() (curl or httpx with HTTP/2)
Movie info       scrape()
Stream           class Source, open_episode(), parse_variants(), pick_variant(), clean_playlist()
Size / quality   estimate_size(), probe_resolution(), probe_qualities()
Names            build_filenames(), episode_labels(), parse_selection()
Download / mux   download_aria2(), download_ytdlp(), make_mp4(), class Meter
Main flow        class Job, resolve_jobs(), download_jobs(), show_sizes(), run(), main()

Messages are bilingual: t("English", "Tiếng Việt"). Please add both when you add a message.
"""
from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import hmac
import html as htmllib
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__version__ = "1.0.0"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
SITE = "https://phim.nguonc.com"
SCRIPT_DIR = Path(__file__).resolve().parent
_SSL_CTX = ssl.create_default_context()

# Message language: "en" (default) or "vi". Set with --lang or the NGUONC_DL_LANG environment variable.
LANG = "en"


def t(en: str, vi: str) -> str:
    """Pick the message for the current language."""
    return vi if LANG == "vi" else en


def detect_lang(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1].lower()
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1].lower()
    env = os.environ.get("NGUONC_DL_LANG", "").lower()
    if env in ("en", "vi"):
        return env
    try:
        import locale
        name = (locale.getlocale()[0] or "").lower()
    except Exception:
        name = ""
    return "vi" if name.startswith(("vi", "vietnam")) else "en"


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #

_print_lock = threading.RLock()
_COLOR = False      # turned on in main() when running in a terminal
_VT = False         # the terminal understands cursor movement (needed for the live board)
_TTY = sys.stdout.isatty()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _style(code: str):
    return lambda s: f"\x1b[{code}m{s}\x1b[0m" if _COLOR else str(s)


bold = _style("1")
dim = _style("2")
gray = _style("90")
red = _style("31")
green = _style("32")
yellow = _style("33")
cyan = _style("36")
bold_red = _style("1;31")
bold_green = _style("1;32")
bold_cyan = _style("1;36")


def _enable_vt() -> bool:
    """Switch on ANSI escape codes (Windows 10+ needs VT mode turned on by hand)."""
    if not _TTY or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ok = False
        for handle_id in (-11, -12):   # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004)) or ok
        return ok
    except Exception:
        return False


def init_console(color: bool) -> None:
    global _COLOR, _VT
    _VT = _enable_vt()
    forced = bool(os.environ.get("FORCE_COLOR"))
    _COLOR = color and not os.environ.get("NO_COLOR") and (_VT or forced)


def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def term_width() -> int:
    # A line exactly as wide as the window wraps, which would break erasing the board.
    return max(40, shutil.get_terminal_size((100, 24)).columns - 1)


def log(msg: str = "", err: bool = False) -> None:
    """Print a line above the progress board, then draw the board again under it."""
    with _print_lock:
        sys.stdout.write(board.erase())
        sys.stdout.flush()
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)
        sys.stdout.write(board.render())
        sys.stdout.flush()


def warn(msg: str) -> None:
    log(f"  {yellow('!')} {yellow(msg)}", err=True)


def fmt_bytes(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_eta(sec: Optional[float]) -> str:
    if sec is None or sec < 0 or sec > 99 * 3600:
        return "--:--"
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def _bar(frac: float, width: int) -> str:
    frac = max(0.0, min(1.0, frac))
    if _COLOR:
        # "━━━━━╸━━━━" style: the done part is colored, with half-cell precision.
        halves = int(frac * width * 2)
        full, half = divmod(halves, 2)
        done = "━" * full + ("╸" if half else "")
        rest = "━" * (width - full - (1 if half else 0))
        return (green if frac >= 1 else cyan)(done) + gray(rest)
    # No colors: solid / shaded blocks so the done and remaining parts still look different.
    eighths = int(frac * width * 8)
    full, part = divmod(eighths, 8)
    partial = " ▏▎▍▌▋▊▉"[part] if part else ""
    return "█" * full + partial + "░" * (width - full - (1 if partial else 0))


class Board:
    """Live progress board, redrawn in place 8 times per second.

        ⠹ Ep 01   ━━━━━━━━━━━━━━╸━━━━━━━━━━   58.3%   412.0 MB / 706.5 MB   18.2 MB/s   ETA 00:16
        ⠼ Ep 02   ━━━━━╸━━━━━━━━━━━━━━━━━━━   21.0%   148.6 MB / 707.9 MB   17.9 MB/s   ETA 00:31
        ──────────────────────────────────────────────────────────────────────────────────────
        Σ Total   ━━━━━━━╸━━━━━━━━━━━━━━━━━   32.1%   3/12 done  │  2.3 GB / 8.4 GB  │  36.1 MB/s  │  ETA 02:53  │  ✗ 0

    One row per episode being downloaded, plus a total row when there is more than one episode.
    Outside a terminal (output redirected to a file) a plain line is printed every few seconds instead.
    """

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    PLAIN_EVERY = 5.0

    def __init__(self) -> None:
        self.enabled = False
        self.rows: dict[str, dict] = {}
        self.drawn = 0
        self.total = 0
        self.ok = self.failed = 0
        self.expected: dict[str, float] = {}   # label -> estimated bytes
        self.done_bytes = 0.0                  # bytes of finished episodes
        self.frame = 0
        self.started = time.monotonic()
        self._plain_last: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- life cycle ------------------------------------------------------- #

    def start(self, jobs: list[tuple[str, Optional[float]]]) -> None:
        self.enabled = _VT
        self.total = len(jobs)
        self.expected = {label: size or 0.0 for label, size in jobs}
        self.ok = self.failed = 0
        self.done_bytes = 0.0
        self.started = time.monotonic()
        if self.enabled:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        with _print_lock:
            sys.stdout.write(self.erase())
            sys.stdout.flush()
            self.enabled = False
            self.rows.clear()

    def _loop(self) -> None:
        while not self._stop.wait(0.125):
            with _print_lock:
                self.frame = (self.frame + 1) % len(self.SPINNER)
                sys.stdout.write(self.erase() + self.render())
                sys.stdout.flush()

    # -- state ------------------------------------------------------------ #

    def update(self, label: str, **state) -> None:
        """state: pct, done, total, speed, eta, or note="..." for a text-only row."""
        with _print_lock:
            if state.get("note"):
                # Keep the numbers of a row that turns into a note, so the total does not jump back.
                state = {**self.rows.get(label, {}), "speed": None, "eta": None, **state}
            self.rows[label] = state
        if not self.enabled and not state.get("note"):
            now = time.monotonic()
            if now - self._plain_last.get(label, 0) >= self.PLAIN_EVERY:
                self._plain_last[label] = now
                log(f"  {label}: {state.get('pct') or 0:5.1f}%  {fmt_bytes(state.get('done'))}"
                    f" / {fmt_bytes(state.get('total'))}  {fmt_bytes(state.get('speed') or 0)}/s"
                    f"  ETA {fmt_eta(state.get('eta'))}")

    def finish(self, label: str, ok: bool, size: float = 0) -> None:
        """Take an episode off the board (call before printing its done / failed line)."""
        with _print_lock:
            self.rows.pop(label, None)
            if ok:
                self.ok += 1
                self.done_bytes += size
            else:
                self.failed += 1
                self.expected.pop(label, None)

    # -- drawing ---------------------------------------------------------- #

    def erase(self) -> str:
        if not self.drawn:
            return ""
        # Move up to the first board line and clear to the end of the screen.
        seq, self.drawn = f"\x1b[{self.drawn}F\x1b[J", 0
        return seq

    # Columns after the bar: (name, width, importance). When the terminal is too narrow the
    # least important column is dropped first; every row uses the same columns so they line up.
    COLUMNS = (("pct", 6, 9), ("bytes", 21, 2), ("speed", 10, 4), ("eta", 11, 3))
    GAP = "   "

    def render(self) -> str:
        if not self.enabled or not self.rows:
            return ""
        width = term_width()
        rows = [(label, st) for label, st in self.rows.items()]
        total_label = self._total_label()
        labels = [label for label, _ in rows] + ([total_label] if self.total > 1 else [])
        label_w = min(max(len(label) for label in labels), width // 4)
        head_w = 4 + label_w + len(self.GAP)

        cols = list(self.COLUMNS)
        while True:
            bar_w = min(30, width - head_w - sum(w + len(self.GAP) for _, w, _ in cols))
            if bar_w >= 10 or len(cols) <= 1:
                break
            cols.remove(min(cols, key=lambda c: c[2]))
        bar_w = max(0, bar_w)

        spin = cyan(self.SPINNER[self.frame])
        lines = []
        for label, st in rows:
            head = f"  {spin} {bold(self._fit(label, label_w))}{self.GAP}"
            if st.get("note"):
                lines.append(self._clip(head + cyan(st["note"]), width))
            else:
                lines.append(self._clip(head + self._cells(st, bar_w, cols), width))
        if self.total > 1:
            total_row = self._clip(self._total_row(total_label, label_w, bar_w, cols), width)
            rule_w = max(visible_len(line) for line in lines + [total_row]) - 2
            lines += ["  " + gray("─" * rule_w), total_row]
        self.drawn = len(lines)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _fit(label: str, w: int) -> str:
        return (label if len(label) <= w else label[:w - 1] + "…").ljust(w)

    @staticmethod
    def _clip(line: str, width: int) -> str:
        return line if visible_len(line) <= width else _ANSI_RE.sub("", line)[:width]

    def _cells(self, st: dict, bar_w: int, cols: list) -> str:
        frac = max(0.0, min(1.0, (st.get("pct") or 0.0) / 100))
        done, total, speed = st.get("done") or 0, st.get("total"), st.get("speed")
        out = _bar(frac, bar_w) if bar_w else ""
        for name, w, _ in cols:
            if name == "pct":
                cell = bold(f"{frac * 100:5.1f}%".rjust(w))
            elif name == "bytes":
                cell = (f"{fmt_bytes(done):>9} / {fmt_bytes(total):<9}" if total else f"{fmt_bytes(done):>9}").ljust(w)
            elif name == "speed":
                cell = cyan(f"{(fmt_bytes(speed) + '/s') if speed else '--/s':>{w}}")
            else:
                eta = fmt_eta(st.get("eta"))
                cell = "ETA " + yellow(f"{eta:>{w - 4}}")
            out += (self.GAP if out else "") + cell
        return out

    def _total_label(self) -> str:
        label = f"{t('Total', 'Tổng')} {self.ok + self.failed}/{self.total}"
        return label + (f" ✗{self.failed}" if self.failed else "")

    def _total_row(self, label: str, label_w: int, bar_w: int, cols: list) -> str:
        active = list(self.rows.values())
        got = self.done_bytes + sum((st.get("done") or 0) for st in active)
        speed = sum((st.get("speed") or 0) for st in active)
        finished = self.ok + self.failed
        # Each running episode counts by its own percentage, so the total bar moves smoothly.
        frac = (finished + sum((st.get("pct") or 0) / 100 for st in self.rows.values())) / max(1, self.total)
        expected = sum(self.expected.values())
        if expected and all(self.expected.values()):
            total = max(expected, got)
            eta = (total - got) / speed if speed else None
        else:
            total = None
            elapsed = time.monotonic() - self.started
            eta = elapsed / frac * (1 - frac) if frac > 0.01 else None
        fitted = self._fit(label, label_w)
        name = bold(fitted)
        if "✗" in fitted:
            before, after = fitted.split("✗", 1)
            name = bold(before) + bold_red("✗" + after)
        st = {"pct": frac * 100, "done": got, "total": total, "speed": speed, "eta": eta}
        return f"  {bold_cyan('Σ')} {name}{self.GAP}" + self._cells(st, bar_w, cols)


board = Board()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _is_transient(error: Exception) -> bool:
    """Short network problems worth retrying (dropped connection, timeout, busy server)."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (429, 500, 502, 503, 504)
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def http_get(url: str, referer: str = "", tries: int = 3) -> str:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as ex:
            if attempt == tries - 1 or not _is_transient(ex):
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


@functools.lru_cache(maxsize=None)
def _curl_has_h2() -> bool:
    """Whether the system curl supports HTTP/2 (checked once)."""
    curl = shutil.which("curl")
    if not curl:
        return False
    try:
        proc = subprocess.run([curl, "--version"], capture_output=True, timeout=10)
        return "HTTP2" in proc.stdout.decode("utf-8", "replace")
    except Exception:
        return False


@functools.lru_cache(maxsize=None)
def _httpx_has_h2() -> bool:
    try:
        import httpx  # noqa: F401
        import h2  # noqa: F401
        return True
    except Exception:
        return False


def has_h2_client() -> bool:
    return _curl_has_h2() or _httpx_has_h2()


def _curl_request(url, method, headers, body, timeout):
    with tempfile.TemporaryDirectory(prefix="nguonc-dl-curl-") as td:
        out_path = os.path.join(td, "body")
        hdr_path = os.path.join(td, "headers")
        cmd = [
            shutil.which("curl"), "-sS", "--http2", "--compressed",
            "-m", str(timeout), "-o", out_path, "-D", hdr_path,
            "-w", "%{http_code}",
        ]
        if method != "GET":
            cmd += ["-X", method]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        if body is not None:
            cmd += ["--data-binary", "@-"]
        cmd.append(url)

        proc = subprocess.run(
            cmd,
            input=body.encode("utf-8") if body is not None else None,
            capture_output=True,
            timeout=timeout + 10,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(t("curl failed: ", "curl lỗi: ") + (err or f"exit {proc.returncode}"))

        status = int(proc.stdout.strip() or 0)
        resp_headers = {}
        if os.path.exists(hdr_path):
            with open(hdr_path, encoding="latin-1") as fh:
                for line in fh:
                    if ":" in line and not line.startswith("HTTP/"):
                        k, v = line.split(":", 1)
                        resp_headers[k.strip().lower()] = v.strip()
        data = b""
        if os.path.exists(out_path):
            with open(out_path, "rb") as fh:
                data = fh.read()
        return status, data, resp_headers


def _httpx_request(url, method, headers, body, timeout):
    import httpx

    with httpx.Client(http2=True, timeout=timeout, follow_redirects=True) as client:
        resp = client.request(
            method, url, headers=headers,
            content=body.encode("utf-8") if body is not None else None,
        )
        return resp.status_code, resp.content, {k.lower(): v for k, v in resp.headers.items()}


def h2_request(
    url: str,
    referer: str = "",
    method: str = "GET",
    headers: Optional[dict] = None,
    body: Optional[str] = None,
    timeout: int = 30,
) -> tuple[int, bytes, dict]:
    """The embed server blocks HTTP/1.1 (Cloudflare 403), so these requests go over HTTP/2."""
    all_headers = dict(headers or {})
    all_headers.setdefault("User-Agent", USER_AGENT)
    if referer:
        all_headers.setdefault("Referer", referer)
    method = method.upper()
    if _curl_has_h2():
        return _curl_request(url, method, all_headers, body, timeout)
    if _httpx_has_h2():
        return _httpx_request(url, method, all_headers, body, timeout)
    raise RuntimeError(t('HTTP/2 is needed: run `pip install "httpx[http2]"` or install a newer curl.',
                         'Cần HTTP/2: chạy `pip install "httpx[http2]"` hoặc cài curl mới hơn.'))


def describe_error(url: str, error: Exception) -> str:
    host = urllib.parse.urlparse(url).hostname or t("the server", "máy chủ")
    if isinstance(error, urllib.error.HTTPError):
        cf = " (Cloudflare)" if error.headers.get("CF-RAY") else ""
        return t(f"HTTP {error.code} from {host}{cf}", f"HTTP {error.code} từ {host}{cf}")
    if isinstance(error, urllib.error.URLError):
        return t(f"network error reaching {host}: {error.reason}", f"lỗi mạng tới {host}: {error.reason}")
    return str(error).strip() or type(error).__name__


# --------------------------------------------------------------------------- #
# Movie info
# --------------------------------------------------------------------------- #

def _extract_slug(url: str) -> Optional[str]:
    m = re.search(r"/phim/([^/?#]+?)(?:\.html?)?/?(?:[?#].*)?$", url)
    return m.group(1) if m else None


def _year_from_api(slug: str) -> str:
    try:
        data = json.loads(http_get(f"{SITE}/api/film/{slug}"))
        for cat in data.get("movie", {}).get("category", {}).values():
            if cat.get("group", {}).get("name") == "Năm":
                # Only a real year: it becomes part of the file and folder names.
                for x in cat.get("list", []):
                    m = re.search(r"\b(?:19|20)\d{2}\b", str(x.get("name", "")))
                    if m:
                        return m.group()
    except Exception:
        pass
    return ""


def _year_from_html(page: str) -> str:
    for pattern in (
        r'"(?:dateCreated|datePublished|releaseDate)":"?(\d{4})',
        r'itemprop=["\']name["\'][^>]*>(\d{4})<',
    ):
        m = re.search(pattern, page)
        if m:
            return m.group(1)
    return ""


def _find_servers(movie_url: str, page: str) -> list[dict]:
    # New layout: data-episode-url points to a JSON {"servers": [...]}.
    m = re.search(r'data-episode-url="([^"]+)"', page)
    if m:
        ep_url = urllib.parse.urljoin(movie_url, htmllib.unescape(m.group(1)))
        try:
            data = json.loads(http_get(ep_url, referer=movie_url))
            if isinstance(data, dict) and data.get("servers"):
                return data["servers"]
        except Exception:
            pass

    # Old layout: the episode data is inside the HTML.
    m = re.search(r'id="nc-episode-data">(\[.*?\])</', page, re.DOTALL)
    if not m:
        m = re.search(r"var episodes\s*=\s*(\[.*?\]);", page, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    try:
        data = json.loads(http_get(movie_url + "/episodes", referer=movie_url))
        if isinstance(data, dict) and data.get("servers"):
            return data["servers"]
    except Exception:
        pass

    raise ValueError(t("no episode list found on the page", "không tìm thấy danh sách tập trên trang"))


def scrape(movie_url: str) -> dict:
    page = http_get(movie_url)

    title = ""
    m = re.search(r"<title>(.*?)</title>", page, re.DOTALL)
    if m:
        title = htmllib.unescape(m.group(1)).strip()
    parts = title.split(" - ", 1)
    english_title = parts[1].strip() if len(parts) == 2 else title

    slug = _extract_slug(movie_url)
    year = (_year_from_api(slug) if slug else "") or _year_from_html(page)

    servers = []
    for i, s in enumerate(_find_servers(movie_url, page)):
        servers.append({
            "name": s.get("name") or s.get("server_name") or f"Server {i + 1}",
            "list": [ep for ep in (s.get("list") or []) if ep.get("embed")],
        })

    return {
        "title": title,
        "english_title": english_title or title or slug or "nguonc",
        "year": year,
        "servers": servers,
    }


# --------------------------------------------------------------------------- #
# Stream
# --------------------------------------------------------------------------- #

# Waits longer than this for the server's rate limit are not worth it: the episode fails instead
# and the next run of the same command picks it up.
MAX_RATE_WAIT = 120


class _NoBootstrap(ValueError):
    pass


class RateLimited(ValueError):
    def __init__(self, seconds: int):
        super().__init__(t(f"rate limited by the server (try again in {seconds} s)",
                           f"bị máy chủ giới hạn lượt (thử lại sau {seconds} giây)"))
        self.seconds = seconds


def _decrypt_m3u8(encrypted: str, video_hash: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv_hex = data_line = None
    for line in encrypted.strip().splitlines():
        if "#ENC-AESGCM" in line:
            m = re.search(r"iv=([0-9a-fA-F]+)", line)
            if m:
                iv_hex = m.group(1)
        elif line.strip() and not line.startswith("#"):
            data_line = line.strip()
    if not iv_hex or not data_line:
        raise ValueError(t("could not read the encrypted playlist", "không đọc được playlist mã hóa"))

    key = hmac.new(b"stream-derive-v1", video_hash.encode("utf-8"), hashlib.sha256).digest()
    plain = AESGCM(key).decrypt(bytes.fromhex(iv_hex), base64.b64decode(data_line), None)
    return plain.decode("utf-8")


def _open_bootstrap(envelope: dict, api_href: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if envelope.get("format") != "aesgcm-v1":
        raise ValueError(t("unknown bootstrap format", "định dạng bootstrap lạ") + f": {envelope.get('format')!r}")
    iv_hex = envelope.get("iv", "")
    data_b64 = envelope.get("data", "")
    if not re.fullmatch(r"[0-9a-fA-F]{24}", iv_hex) or not data_b64:
        raise ValueError(t("broken bootstrap envelope", "bootstrap envelope hỏng"))
    aad = f"stream-bootstrap-v1\n{api_href}".encode("utf-8")
    key = hashlib.sha256(aad).digest()
    plain = AESGCM(key).decrypt(bytes.fromhex(iv_hex), base64.b64decode(data_b64), aad)
    return json.loads(plain.decode("utf-8"))


class Source:
    """Fetches the playlists of one episode the way its player does: the new embed server only
    answers over HTTP/2, and any playlist (master or media) may be AES-GCM encrypted."""

    def __init__(self, embed_url: str, video_hash: str = "", http2: bool = False):
        self.embed_url = embed_url
        self.video_hash = video_hash
        self.http2 = http2

    def fetch(self, url: str) -> str:
        if self.http2:
            status, body, _ = h2_request(url, referer=self.embed_url)
            if status != 200:
                raise ValueError(t(f"HTTP {status} while fetching the playlist", f"HTTP {status} khi tải playlist"))
            text = body.decode("utf-8", "replace")
        else:
            text = http_get(url, referer=self.embed_url)
        if "#ENC-AESGCM" in text:
            text = _decrypt_m3u8(text, self.video_hash)
        # A byte order mark would make "#EXTM3U" look like a segment address.
        return text.lstrip("\ufeff")


def _bootstrap_playlist(movie_url: str, embed_url: str) -> tuple[str, str, Source]:
    parsed = urllib.parse.urlparse(embed_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.hostname or "embed"

    status, html_bytes, _ = h2_request(embed_url, referer=movie_url)
    if status != 200:
        raise ValueError(t(f"HTTP {status} from {host}", f"HTTP {status} từ {host}"))
    page = html_bytes.decode("utf-8", "replace")

    m = re.search(r'<script id="stream-bootstrap"[^>]*>(.*?)</script>', page, re.DOTALL)
    if not m:
        raise _NoBootstrap()
    api_href = json.loads(m.group(1)).get("api")
    if not api_href or urllib.parse.urlparse(api_href).netloc != parsed.netloc:
        raise ValueError(t("invalid bootstrap address", "địa chỉ bootstrap không hợp lệ"))

    hash_m = re.search(r"[?&]hash=([0-9a-f]+)", embed_url)
    video_hash = hash_m.group(1) if hash_m else ""

    movie_parsed = urllib.parse.urlparse(movie_url)
    body = json.dumps({
        "action": "bootstrap",
        "referrer": movie_url[:4096],
        "frame_origins": [f"{movie_parsed.scheme}://{movie_parsed.netloc}"],
        "request_grant": True,
        "playlist_format": "aesgcm-v2",
        "pretty_url": True,
        "path_chunks": True,
        "bootstrap_format": "aesgcm-v1",
    })
    status, resp, resp_headers = h2_request(
        api_href,
        method="POST",
        headers={"Content-Type": "application/json", "Origin": origin, "Referer": embed_url},
        body=body,
    )
    if status in (429, 503):
        try:
            seconds = int(resp_headers.get("retry-after", "60"))
        except ValueError:
            seconds = 60
        raise RateLimited(seconds)
    if status != 200:
        detail = ""
        try:
            detail = json.loads(resp).get("error", "")
        except Exception:
            pass
        raise ValueError(t(f"HTTP {status} from {host}", f"HTTP {status} từ {host}")
                         + f" (bootstrap{': ' + detail if detail else ''})")

    envelope = json.loads(resp)
    opened = _open_bootstrap(envelope, api_href) if envelope.get("format") == "aesgcm-v1" else envelope

    preissued = opened.get("preissued") or {}
    if opened.get("turnstileEnabled") and not preissued:
        raise ValueError(t("the server asks for a Turnstile check (captcha)",
                           "máy chủ yêu cầu xác minh Turnstile (captcha)"))
    playlist_url = preissued.get("playlist")
    if not playlist_url:
        raise ValueError(t("bootstrap returned no playlist", "bootstrap không trả về playlist"))
    if urllib.parse.urlparse(playlist_url).netloc != parsed.netloc:
        raise ValueError(t("playlist is on an unknown server", "playlist nằm trên máy chủ lạ"))

    source = Source(embed_url, opened.get("video") or video_hash, http2=True)
    return source.fetch(playlist_url), playlist_url, source


def _legacy_playlist(movie_url: str, embed_url: str) -> tuple[str, str, Source]:
    """Old embed pages (data-obf)."""
    page = http_get(embed_url, referer=movie_url)
    m = re.search(r'data-obf="([^"]+)"', page)
    if not m:
        hint = "" if has_h2_client() else t(' (no HTTP/2: run `python -m pip install "httpx[http2]"`)',
                                            ' (thiếu HTTP/2: chạy `python -m pip install "httpx[http2]"`)')
        raise ValueError(t("could not read the embed page", "không đọc được trang nhúng") + hint)
    data = json.loads(base64.b64decode(m.group(1)).decode())
    parsed = urllib.parse.urlparse(embed_url)
    url = f"{parsed.scheme}://{parsed.netloc}/{data['sUb']}.m3u8"
    source = Source(embed_url, data.get("hD", ""))
    return source.fetch(url), url, source


def open_episode(movie_url: str, embed_url: str) -> tuple[str, str, Source]:
    """The top playlist of one episode: (text, URL, Source). Waits and retries when rate limited."""
    for attempt in range(3):
        try:
            if has_h2_client():
                try:
                    return _bootstrap_playlist(movie_url, embed_url)
                except _NoBootstrap:
                    pass
            return _legacy_playlist(movie_url, embed_url)
        except RateLimited as ex:
            if attempt == 2 or ex.seconds > MAX_RATE_WAIT:
                raise
            warn(t(f"Rate limited by the server, waiting {ex.seconds} s...",
                   f"Bị máy chủ giới hạn lượt, đợi {ex.seconds} giây..."))
            time.sleep(ex.seconds + 1)
    raise AssertionError("unreachable")


def resolution_label(width: int, height: int) -> str:
    """'1080p' for 1920x1080, but also for 1920x800 (wide movies) or 1440x1080 (4:3)."""
    for min_w, min_h, name in ((3800, 2100, "2160p"), (2500, 1400, "1440p"), (1900, 1000, "1080p"),
                               (1260, 700, "720p"), (840, 470, "480p"), (630, 350, "360p")):
        if width >= min_w or height >= min_h:
            return name
    return f"{height}p"


def parse_variants(text: str, base_url: str) -> list[dict]:
    """The qualities listed in a master playlist, highest first."""
    variants, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
        if not nxt:
            continue
        bw = re.search(r"[:,]BANDWIDTH=(\d+)", line)
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        width, height = (int(res.group(1)), int(res.group(2))) if res else (0, 0)
        variants.append({
            "url": urllib.parse.urljoin(base_url, nxt),
            "bandwidth": int(bw.group(1)) if bw else 0,
            "width": width,
            "height": height,
            "p": int(resolution_label(width, height)[:-1]) if height else 0,
        })
    variants.sort(key=lambda v: (v["p"], v["height"], v["bandwidth"]), reverse=True)
    return variants


def quality_label(variant: dict) -> str:
    if variant.get("height"):
        return f"{resolution_label(variant['width'], variant['height'])} ({variant['width']}x{variant['height']})"
    if variant.get("bandwidth"):
        return f"{variant['bandwidth'] // 1000} kbps"
    return t("source", "gốc")


def pick_variant(variants: list[dict], quality: Optional[str]) -> dict:
    """quality: None/'best' = highest, 'worst' = lowest, '720' or '720p' = closest."""
    q = (quality or "best").strip().lower()
    if q == "best":
        return variants[0]
    if q == "worst":
        return variants[-1]
    m = re.fullmatch(r"(\d+)p?", q)
    if not m:
        raise ValueError(t("invalid quality", "chất lượng không hợp lệ") + f": {quality!r}")
    target = int(m.group(1))
    # Closest one; on a tie take the higher one.
    return min(variants, key=lambda v: (abs(v["p"] - target), -v["p"], -v["bandwidth"]))


def clean_playlist(text: str, base_url: str) -> tuple[str, int]:
    """Make segment / key links absolute and drop ad segments spliced into the movie.

    Ads are inserted as a few segments from a completely different folder than the movie
    (e.g. convertv8/...), between #EXT-X-DISCONTINUITY tags.
    Returns (new playlist, number of segments removed).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    seg_urls = [urllib.parse.urljoin(base_url, l) for l in lines if not l.startswith("#")]
    if not seg_urls:
        return text, 0
    def folder(u: str) -> str:
        # Without the query string: a "/" in a token must not look like another folder.
        return u.split("?", 1)[0].rsplit("/", 1)[0]

    dirs: dict[str, int] = {}
    for u in seg_urls:
        d = folder(u)
        dirs[d] = dirs.get(d, 0) + 1
    main_dir = max(dirs, key=dirs.get)
    minority = len(seg_urls) - dirs[main_dir]
    drop_ads = 0 < minority <= 0.2 * len(seg_urls)

    out, pending, removed = [], [], 0
    for line in lines:
        if line.startswith(("#EXT-X-KEY", "#EXT-X-MAP")):
            line = re.sub(r'URI="([^"]+)"',
                          lambda m: f'URI="{urllib.parse.urljoin(base_url, m.group(1))}"', line)
        if line.startswith("#EXT-X-DISCONTINUITY") and drop_ads:
            continue
        if line.startswith(("#EXTINF", "#EXT-X-BYTERANGE", "#EXT-X-PROGRAM-DATE-TIME")):
            pending.append(line)
            continue
        if line.startswith("#"):
            out.append(line)
            continue
        url = urllib.parse.urljoin(base_url, line)
        if drop_ads and folder(url) != main_dir:
            removed += 1
        else:
            out.extend(pending)
            out.append(url)
        pending = []
    return "\n".join(out) + "\n", removed


def playlist_entries(playlist: str) -> list[tuple[str, float]]:
    """(segment URL, duration in seconds) of a cleaned media playlist."""
    entries, dur = [], 0.0
    for raw in playlist.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:([\d.]+)", line)
            dur = float(m.group(1)) if m else 0.0
        elif line and not line.startswith("#"):
            entries.append((line, dur))
            dur = 0.0
    return entries


def parse_segments(playlist: str) -> Optional[list[str]]:
    """Segment URLs for aria2c, or None when the playlist needs yt-dlp
    (AES-128 keys, byte ranges, init segment)."""
    for line in playlist.splitlines():
        if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-MAP", "#EXT-X-BYTERANGE")):
            return None
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            return None
    return [url for url, _ in playlist_entries(playlist)] or None


def media_of(source: Source, variant: dict) -> tuple[str, str]:
    """The media playlist of one quality of a master playlist: (text, URL)."""
    media = source.fetch(variant["url"])
    if parse_variants(media, variant["url"]):
        raise ValueError(t("nested master playlist", "playlist chọn chất lượng lồng nhau"))
    return media, variant["url"]


# --------------------------------------------------------------------------- #
# Segments, size and resolution
# --------------------------------------------------------------------------- #

TS_PACKET = 188


def ts_start(data: bytes) -> int:
    """Where the MPEG-TS stream starts in a segment, or -1 if it is not one.
    Some CDNs put a fake image header in front of the video data."""
    if len(data) > 2 * TS_PACKET and data[0] == 0x47 and data[TS_PACKET] == 0x47:
        return 0
    for i in range(min(len(data) - 3 * TS_PACKET, 65536)):
        if data[i] == 0x47 and data[i + TS_PACKET] == 0x47 and data[i + 2 * TS_PACKET] == 0x47:
            return i
    return -1


def ts_payload(data: bytes) -> bytes:
    start = ts_start(data)
    if start < 0:
        raise ValueError(t("a segment is not a video file", "một đoạn tải về không phải video"))
    return data[start:] if start else data


def _get(url: str, referer: str, method: str = "GET", byte_range: str = "", timeout: int = 15):
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    if byte_range:
        headers["Range"] = f"bytes={byte_range}"
    req = urllib.request.Request(url, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)


def remote_size(url: str, referer: str) -> Optional[int]:
    """File size on the server (HEAD, or a 1-byte GET), without downloading it."""
    for method, byte_range in (("HEAD", ""), ("GET", "0-0")):
        try:
            with _get(url, referer, method, byte_range) as resp:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                if m:
                    return int(m.group(1))
                length = resp.headers.get("Content-Length")
                if length and (method == "HEAD" or resp.status == 200):
                    return int(length)
        except Exception:
            continue
    return None


def estimate_size(playlist: str, referer: str, samples: int = 40) -> tuple[Optional[float], float]:
    """Estimate the size of one episode from a few segments spread over it.
    Returns (bytes or None, duration in seconds)."""
    entries = playlist_entries(playlist)
    total_dur = sum(d for _, d in entries)
    # Byte ranges: every segment is part of the same file, so measuring them means nothing.
    if not entries or "#EXT-X-BYTERANGE" in playlist:
        return None, total_dur

    n = min(samples, len(entries))
    picks = sorted({round(i * (len(entries) - 1) / max(1, n - 1)) for i in range(n)})
    sizes: dict[int, int] = {}

    def probe(i):
        size = remote_size(entries[i][0], referer)
        if size:
            sizes[i] = size

    run_parallel(probe, picks, 8)
    if not sizes:
        return None, total_dur
    sampled_dur = sum(entries[i][1] for i in sizes)
    if total_dur > 0 and sampled_dur > 0:
        return sum(sizes.values()) / sampled_dur * total_dur, total_dur
    return sum(sizes.values()) / len(sizes) * len(entries), total_dur


def probe_resolution(playlist: str, referer: str, ffmpeg: Optional[str]) -> Optional[tuple[int, int]]:
    """Width and height of the video, read by ffmpeg from the start of one segment."""
    entries = playlist_entries(playlist)
    if not entries or not ffmpeg:
        return None
    try:
        with _get(entries[len(entries) // 2][0], referer, byte_range="0-1048575", timeout=20) as resp:
            data = ts_payload(resp.read())
        proc = subprocess.run([ffmpeg, "-hide_banner", "-f", "mpegts", "-i", "pipe:0"],
                              input=data, capture_output=True, timeout=30)
    except Exception:
        return None
    m = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", proc.stderr.decode("utf-8", "replace"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def describe_quality(variant: Optional[dict], playlist: str, referer: str, ffmpeg: Optional[str]) -> str:
    """Quality label of the stream that will be downloaded, e.g. '1080p (1920x960)'."""
    if variant and variant.get("height"):
        return quality_label(variant)
    res = probe_resolution(playlist, referer, ffmpeg)
    if res:
        return quality_label({"width": res[0], "height": res[1]})
    return quality_label(variant or {})


def probe_qualities(movie_url: str, ep: dict, ffmpeg: Optional[str]) -> list[dict]:
    """Every quality of one episode with its estimated size, without downloading the movie.
    Each item: {"label", "size" (bytes or None), "duration" (seconds)}."""
    embed_url = ep["embed"]
    text, url, source = open_episode(movie_url, embed_url)
    variants = parse_variants(text, url)
    streams = []
    for v in variants or [None]:
        media, media_url = media_of(source, v) if v else (text, url)
        streams.append((v, clean_playlist(media, media_url)[0]))

    labels = [describe_quality(v, pl, embed_url, ffmpeg) for v, pl in streams]
    entries = []
    for (v, pl), label in zip(streams, labels):
        if labels.count(label) > 1 and v and v["bandwidth"]:
            # Two variants with the same resolution: tell them apart by bitrate.
            label = f"{label} {v['bandwidth'] // 1000}k"
        size, duration = estimate_size(pl, embed_url)
        if size is None and v and v["bandwidth"] and duration:
            size = v["bandwidth"] * duration / 8
        entries.append({"label": label, "size": size, "duration": duration})
    return entries


# --------------------------------------------------------------------------- #
# File names, episode selection
# --------------------------------------------------------------------------- #

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


MAX_NAME = 120   # keeps the folder + file name + ".parts/00000.ts.aria2" under the Windows path limit


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", name).strip()[:MAX_NAME].rstrip(". ")
    # Windows reads "CON.anything" as the device CON: the part before the first dot must change.
    head, dot, rest = name.partition(".")
    if head.strip().upper() in WINDOWS_RESERVED:
        name = f"{head.strip()}_{dot}{rest}"
    return name if name not in ("", ".", "..") else "nguonc"


def dotted_name(name: str) -> str:
    """'Con Air' -> 'Con_.Air': a safe name with dots instead of spaces."""
    return safe_name(re.sub(r"\s+", ".", safe_name(name)))


def build_filenames(title: str, year: str, episodes: list[dict]) -> list[str]:
    """File names for every episode of a server, always unique:
    Movie.Name.2024.mp4 for a movie, Series.Name.S01E01.mp4 for a series."""
    base = dotted_name(title)
    if len(episodes) == 1:
        return [f"{base}.{year}.mp4" if year else f"{base}.mp4"]

    names, used = [], set()
    for ep in episodes:
        label = str(ep.get("name", ""))
        num = re.search(r"\d+", label)
        if num:
            stem = f"{base}.S01E{int(num.group()):02d}"
        else:
            # (no backslash inside an f-string expression: that is a syntax error before Python 3.12)
            stem = base + "." + dotted_name(label)
        candidate, n = stem, 2
        while candidate.lower() in used:
            candidate = f"{stem}.{n}"
            n += 1
        used.add(candidate.lower())
        names.append(f"{candidate}.mp4")
    return names


def episode_labels(episodes: list[dict]) -> list[str]:
    """'Movie' for a single episode, else 'Ep 01', 'Ep 02', ... (numbers padded to the same width)."""
    if len(episodes) == 1:
        return [t("Movie", "Phim")]
    names = [str(ep.get("name") or "?") for ep in episodes]
    width = max((len(n) for n in names if n.isdigit()), default=1)
    return [f"{t('Ep', 'Tập')} {n.zfill(width) if n.isdigit() else n}" for n in names]


def parse_selection(text: str, count: int) -> list[int]:
    """'all', '1-5,8', '10-' -> list of 0-based indexes."""
    text = (text or "").strip().lower()
    if text in ("", "all", "*", "tatca", "tất cả"):
        return list(range(count))
    picked = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m:
            start = int(m.group(1) or 1)
            end = int(m.group(2) or count)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(t(f"cannot read '{part}'", f"không hiểu '{part}'"))
        if start < 1 or end > count or start > end:
            raise ValueError(t(f"'{part}' is outside 1-{count}", f"'{part}' nằm ngoài khoảng 1-{count}"))
        picked.extend(range(start - 1, end))
    return sorted(set(picked))


# --------------------------------------------------------------------------- #
# Download / mux
# --------------------------------------------------------------------------- #

def find_tool(name: str) -> Optional[str]:
    """Prefer an executable next to nguonc-dl.py (aria2c.exe, ffmpeg.exe), then PATH."""
    for candidate in (SCRIPT_DIR / f"{name}.exe", SCRIPT_DIR / name):
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return shutil.which(name)


def find_ffmpeg() -> Optional[str]:
    """ffmpeg next to the script or on PATH, else the copy shipped with imageio-ffmpeg."""
    path = find_tool("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# Running aria2c / ffmpeg processes, so Ctrl+C stops them too.
_children: set = set()
_children_lock = threading.Lock()
_stopping = threading.Event()


def spawn(cmd: list, **kwargs) -> subprocess.Popen:
    """Start a child process that kill_children() can stop. After Ctrl+C no new one is started
    (a download thread would otherwise start the next aria2c pass / ffmpeg retry and leave it running)."""
    with _children_lock:
        if _stopping.is_set():
            raise KeyboardInterrupt
        proc = subprocess.Popen(cmd, **kwargs)
        _children.add(proc)
    return proc


def kill_children() -> None:
    with _children_lock:
        _stopping.set()
        procs = list(_children)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass


def _remove(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def make_mp4(ffmpeg: str, out_path: str, ts_file: Optional[str] = None, parts: Optional[list[Path]] = None) -> None:
    """Join MPEG-TS into a real MP4 without re-encoding, from a .ts file or straight from the
    segment files (piped into ffmpeg, so no big temporary .ts is written)."""
    tmp = out_path + ".part"
    err = ""
    # aac_adtstoasc is needed for AAC audio in TS; retry without it if the audio is not AAC.
    attempts = [(["-f", "mpegts"], ["-bsf:a", "aac_adtstoasc"]), (["-f", "mpegts"], [])]
    if ts_file:
        # A file from yt-dlp is not always MPEG-TS (playlists with an init segment are fragmented
        # MP4): as a last try let ffmpeg find the format itself.
        attempts.append(([], []))
    for fmt, extra in attempts:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *fmt, "-i", ts_file or "pipe:0",
               "-map", "0:v?", "-map", "0:a?", "-c", "copy", *extra, "-movflags", "+faststart", "-f", "mp4", tmp]
        with tempfile.TemporaryFile() as err_file:
            proc = spawn(cmd, stdin=subprocess.PIPE if parts else subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=err_file)
            try:
                if parts:
                    try:
                        for part in parts:
                            proc.stdin.write(ts_payload(part.read_bytes()))
                    except OSError:
                        pass   # ffmpeg stopped early; its error message says why
                    finally:
                        try:
                            proc.stdin.close()   # flushes the buffer: fails again if ffmpeg is gone
                        except OSError:
                            pass
                code = proc.wait()
            except BaseException:
                proc.kill()   # e.g. a segment that is not video: do not leave ffmpeg and its .part behind
                proc.wait()
                _remove(tmp)
                raise
            finally:
                _children.discard(proc)
            err_file.seek(0)
            err = err_file.read().decode("utf-8", "replace").strip()
        if code == 0:
            os.replace(tmp, out_path)
            return
    _remove(tmp)
    raise RuntimeError(t("ffmpeg could not make the MP4", "ffmpeg không ghép được MP4") + f": {err[-300:]}")


def _note_joining(label: str) -> None:
    if board.enabled:
        board.update(label, note=t("joining into MP4...", "đang ghép MP4..."))
    else:
        log(f"  {label}: " + t("joining into MP4...", "đang ghép MP4..."))


class Meter:
    """Speed (5-second moving average), total and ETA of one episode, shown on the board."""

    def __init__(self, label: str, count: int, expected: Optional[float]):
        self.label, self.count, self.expected = label, count, expected
        self.samples: list[tuple[float, int]] = []

    def report(self, done_count: int, done_bytes: int, all_bytes: int) -> None:
        now = time.monotonic()
        self.samples = [s for s in self.samples if now - s[0] <= 5] + [(now, all_bytes)]
        t0, b0 = self.samples[0]
        speed = (all_bytes - b0) / (now - t0) if now - t0 >= 0.5 else None
        # At the start a few segments are not enough to guess the total: use the earlier estimate.
        if self.expected and done_count < max(5, self.count // 20):
            total = max(self.expected, all_bytes)
        else:
            total = done_bytes / done_count * self.count if done_count else None
        eta = (total - all_bytes) / speed if total and speed else None
        board.update(self.label, pct=100.0 * done_count / self.count, done=all_bytes,
                     total=total, speed=speed, eta=eta)


# aria2c runs again for the segments that failed or came back broken, before giving up.
ARIA2_PASSES = 3


def _file_sizes(folder: Path) -> dict[str, int]:
    """name -> size of the files in a folder. aria2c creates and deletes files (.aria2 control
    files) while it runs, so a file can vanish between listing it and reading its size."""
    sizes = {}
    with os.scandir(folder) as it:
        for entry in it:
            try:
                sizes[entry.name] = entry.stat().st_size
            except OSError:
                pass
    return sizes


def _check_parts(parts_dir: Path, names: list[str], good: set[str]) -> None:
    """Add the finished segments that really are video to `good`; delete broken ones
    (empty files, error pages) so they are downloaded again."""
    listing = _file_sizes(parts_dir)
    for name in names:
        if name in good or name not in listing or name + ".aria2" in listing:
            continue
        path = parts_dir / name
        try:
            with open(path, "rb") as fh:
                head = fh.read(65536 + 3 * TS_PACKET)
        except OSError:
            continue
        if listing[name] > 0 and ts_start(head) >= 0:
            good.add(name)
        else:
            _remove(path)


def _run_aria2(aria2: str, parts_dir: Path, pending: list[tuple[str, str]], referer: str,
               connections: int, meter: Meter, names: set[str]) -> None:
    input_path = parts_dir / "input.txt"
    input_path.write_text("".join(f"{url}\n  out={name}\n" for url, name in pending), encoding="utf-8")
    cmd = [
        aria2, "--no-conf",
        f"--input-file={input_path}", f"--dir={parts_dir}",
        # The CDN limits speed per connection: download many segments at once,
        # and split each segment (~2 MB) over a few more connections.
        f"--max-concurrent-downloads={connections}",
        "--split=4", "--max-connection-per-server=4", "--min-split-size=1M",
        f"--user-agent={USER_AGENT}", f"--referer={referer}",
        "--http-accept-gzip=true",
        "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=true",
        "--file-allocation=none", "--max-tries=10", "--retry-wait=2",
        "--connect-timeout=15", "--timeout=30",
        "--console-log-level=error", "--summary-interval=0",
        "--download-result=hide", "--show-console-readout=false",
        f"--log={parts_dir / 'aria2.log'}", "--log-level=warn",
    ]
    proc = spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                finished = True
            except subprocess.TimeoutExpired:
                finished = False
            listing = _file_sizes(parts_dir)
            done =[n for n in names if n in listing and n + ".aria2" not in listing]
            meter.report(len(done), sum(listing[n] for n in done),
                         sum(size for name, size in listing.items() if name in names))
            if finished:
                return
    except BaseException:
        proc.terminate()
        raise
    finally:
        _children.discard(proc)


def download_aria2(aria2: str, segments: list[str], out_path: str, referer: str, label: str,
                   ffmpeg: Optional[str], connections: int = 16, expected: Optional[float] = None,
                   tag: str = "") -> str:
    """Download the segments with aria2c, check them, then join them into the MP4.

    Unfinished segments live in a hidden .<name>.parts folder next to the target file,
    so Ctrl+C and running the command again continues instead of starting over.
    `tag` names the stream (quality): segments kept from another stream are thrown away.
    """
    stem = os.path.splitext(out_path)[0]
    parts_dir = Path(out_path).parent / f".{os.path.basename(stem)}.parts"
    parts_dir.mkdir(exist_ok=True)
    names = [f"{i:05d}.ts" for i in range(len(segments))]

    # Segment files are only numbered, so segments left by a run with another -q (or another
    # number of segments) would be joined into this movie. Start over when the stream changed.
    stamp_path = parts_dir / "stream.txt"
    stamp = f"{len(segments)} {tag}".strip()
    try:
        old_stamp = stamp_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        old_stamp = None   # folder from an older version: keep its segments
    if old_stamp is not None and old_stamp != stamp:
        shutil.rmtree(parts_dir, ignore_errors=True)
        parts_dir.mkdir(exist_ok=True)
    stamp_path.write_text(stamp + "\n", encoding="utf-8")

    good: set[str] = set()
    meter = Meter(label, len(names), expected)

    for _ in range(ARIA2_PASSES):
        _check_parts(parts_dir, names, good)
        pending = [(u, n) for u, n in zip(segments, names) if n not in good]
        if not pending:
            break
        _run_aria2(aria2, parts_dir, pending, referer, connections, meter, set(names))
    _check_parts(parts_dir, names, good)

    missing = len(names) - len(good)
    if missing:
        detail = ""
        try:
            with open(parts_dir / "aria2.log", encoding="utf-8", errors="replace") as fh:
                errors = [l.strip() for l in fh if "ERROR" in l or "error" in l]
            if errors:
                detail = f": {errors[-1][-200:]}"
        except OSError:
            pass
        raise RuntimeError(t(f"{missing}/{len(names)} segments could not be downloaded{detail} "
                             "(run the same command again to continue)",
                             f"còn {missing}/{len(names)} đoạn chưa tải được{detail} "
                             "(chạy lại lệnh để tải tiếp)"))

    parts = [parts_dir / n for n in names]
    if ffmpeg:
        _note_joining(label)
        make_mp4(ffmpeg, out_path, parts=parts)
        final = out_path
    else:
        # Plain concatenation of MPEG-TS segments is a valid stream.
        final = stem + ".ts"
        with open(final + ".part", "wb") as out:
            for part in parts:
                out.write(ts_payload(part.read_bytes()))
        os.replace(final + ".part", final)
    shutil.rmtree(parts_dir, ignore_errors=True)
    return final


def download_ytdlp(src: str, out_path: str, referer: str, label: str, ffmpeg: Optional[str],
                   fragments: int = 8, expected: Optional[float] = None) -> str:
    """Download one episode (src is the temporary .m3u8) with yt-dlp. Returns the final file path."""
    import yt_dlp

    ts_path = os.path.splitext(out_path)[0] + ".ts"

    def hook(d):
        if d.get("status") != "downloading":
            return
        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or expected
        frag_i, frag_n = d.get("fragment_index"), d.get("fragment_count")
        if frag_n:
            pct = 100.0 * (frag_i or 0) / frag_n
        elif total:
            pct = 100.0 * done / total
        else:
            pct = 0.0
        board.update(label, pct=pct, done=done, total=total, speed=d.get("speed"), eta=d.get("eta"))

    opts = {
        # yt-dlp treats % as a template character in file names.
        "outtmpl": ts_path.replace("%", "%%"),
        # ffmpeg makes the MP4 below; yt-dlp must not touch the file.
        "fixup": "never",
        # The CDN limits speed per connection, so many fragments at once is much faster.
        "concurrent_fragment_downloads": fragments,
        "retries": 10,
        "fragment_retries": 10,
        # A missing fragment is an error: never join a file with broken or missing frames.
        "skip_unavailable_fragments": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "http_headers": {"Referer": referer},
        "enable_file_urls": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        failed = ydl.download([Path(src).resolve().as_uri()])
    if failed or not os.path.exists(ts_path):
        raise RuntimeError(t("yt-dlp reported an error", "yt-dlp báo lỗi khi tải"))
    if not ffmpeg:
        return ts_path
    _note_joining(label)
    make_mp4(ffmpeg, out_path, ts_file=ts_path)
    os.remove(ts_path)
    return out_path


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def run_parallel(fn, items: list, workers: int) -> None:
    """Run fn(item) in parallel while Ctrl+C still works.

    On Windows, pool.map() / `with ThreadPoolExecutor` blocks the main thread in a lock that
    cannot be interrupted, so Ctrl+C is ignored until everything is done. Waiting in short steps
    lets KeyboardInterrupt through.
    """
    if not items:
        return
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))))
    futures = [pool.submit(fn, item) for item in items]
    try:
        while wait(futures, timeout=0.3).not_done:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for fut in futures:
        fut.result()


@dataclass
class Job:
    """One episode to download."""
    label: str
    ep: dict
    out: str
    src: str = ""                    # temporary .m3u8 (cleaned media playlist)
    playlist: str = ""
    quality: str = ""
    stream: str = ""                 # which quality of the master playlist (kept with unfinished segments)
    ads_removed: int = 0
    size: Optional[float] = None     # estimated bytes
    duration: float = 0.0
    status: str = ""                 # "", "ok", "skip" or "fail"
    error: str = ""

    def fail(self, error: str) -> None:
        self.status, self.error = "fail", error


def aria2_tip() -> str:
    return t("Tip: aria2c downloads several times faster. Get aria2c.exe from "
             "https://github.com/aria2/aria2/releases and put it next to nguonc-dl.py "
             "(macOS: brew install aria2, Linux: sudo apt install aria2).",
             "Mẹo: aria2c tải nhanh hơn nhiều lần. Tải aria2c.exe tại "
             "https://github.com/aria2/aria2/releases rồi đặt cạnh nguonc-dl.py "
             "(macOS: brew install aria2, Linux: sudo apt install aria2).")


def fmt_bitrate(size: Optional[float], duration: float) -> str:
    return f"{size * 8 / duration / 1e6:.1f} Mbps" if size and duration else "?"


def show_sizes(url: str, episodes: list[dict], labels: list[str], picked: list[int],
               sample_only: bool, ffmpeg: Optional[str]) -> int:
    """Print the quality, size and bitrate of every stream and exit, without downloading.

    For a series without -e only the first episode is measured and multiplied for the whole series,
    because every nguonc episode costs one link request (the server allows about 20 per hour).
    """
    probe_list = picked[:1] if sample_only else picked
    what = t("the first episode", "tập đầu") if len(probe_list) == 1 else t(f"{len(probe_list)} episodes",
                                                                            f"{len(probe_list)} tập")
    log("\n" + cyan(t(f"Measuring {what} (nothing is downloaded)...",
                      f"Đang đo {what} (không tải phim)...")))
    results: dict[int, list[dict]] = {}

    def probe(i):
        try:
            results[i] = probe_qualities(url, episodes[i], ffmpeg)
        except Exception as ex:
            warn(f"{labels[i]}: {describe_error(episodes[i].get('embed', ''), ex)}")

    run_parallel(probe, probe_list, 3)
    if not results:
        return 1

    # Group by quality, keeping the playlist's high -> low order.
    totals: dict[str, dict] = {}
    for i in probe_list:
        for e in results.get(i, []):
            tot = totals.setdefault(e["label"], {"size": 0.0, "duration": 0.0, "count": 0, "unknown": 0})
            tot["count"] += 1
            tot["duration"] += e["duration"] or 0
            if e["size"]:
                tot["size"] += e["size"]
            else:
                tot["unknown"] += 1

    scale = len(picked) if sample_only and len(picked) > 1 else 1
    q_w = max(12, *(len(label) for label in totals)) + 2
    h_size = t(f"Whole series ({len(picked)} eps)", f"Cả bộ ({len(picked)} tập)") if scale > 1 else t("Size", "Dung lượng")
    s_w = max(14, len(h_size) + 2)
    head = f"{t('Quality', 'Chất lượng'):<{q_w}}"
    if scale > 1:
        head += f"{t('Per episode', 'Mỗi tập'):<14}"
    head += f"{h_size:<{s_w}}{t('Bitrate', 'Bitrate'):<12}{t('Length', 'Thời lượng')}"
    log("\n  " + bold(head))
    log("  " + gray("─" * visible_len(head)))
    for n, (label, tot) in enumerate(totals.items()):
        known = tot["count"] - tot["unknown"]
        per_ep = tot["size"] / known if known and tot["size"] else 0
        known_dur = tot["duration"] / tot["count"] if tot["count"] else 0
        total = per_ep * scale if scale > 1 else tot["size"]
        size_txt = f"~{fmt_bytes(total)}" if total else t("unknown", "không đo được")
        row = f"{label:<{q_w}}"
        if scale > 1:
            row = bold(row) + f"{(f'~{fmt_bytes(per_ep)}' if per_ep else '?'):<14}"
        else:
            row = bold(row)
        row += green(f"{size_txt:<{s_w}}") + f"{fmt_bitrate(per_ep, known_dur):<12}"
        row += fmt_eta(tot["duration"] * scale) if tot["duration"] else "?"
        if tot["unknown"] and tot["size"]:
            row += yellow(t(f"  ({tot['unknown']} episodes unknown)", f"  (thiếu {tot['unknown']} tập)"))
        if n == 0:
            row += "  " + cyan(t("← downloaded by default", "← mặc định tải bản này"))
        log("  " + row)
    if scale > 1:
        log(gray(t("\n  (Estimated from the first episode; use -e to measure specific episodes.)",
                   "\n  (Ước tính từ tập đầu; dùng -e để đo chính xác các tập cụ thể.)")))
    if len(totals) > 1:
        log(gray(t("  Pick another quality with -q, for example: -q 720",
                   "  Chọn chất lượng khác bằng -q, ví dụ: -q 720")))
    else:
        log(gray(t("  This is the only quality the site has for this movie.",
                   "  Đây là chất lượng duy nhất trang có cho phim này.")))
    if not find_tool("aria2c"):
        log("\n" + yellow(aria2_tip()))
    return 0


def ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return answer or default


def choose_server(servers: list[dict], args) -> Optional[dict]:
    """The server from -s; ask when there are several, otherwise server 1."""
    if args.server is not None:
        server_no = args.server
    elif len(servers) > 1 and sys.stdin.isatty() and not args.yes:
        try:
            server_no = int(ask("\n" + t("Choose a server", "Chọn server"), "1"))
        except ValueError:
            server_no = 0
    else:
        server_no = 1
    if not 1 <= server_no <= len(servers):
        log(red(t(f"The server must be 1 to {len(servers)}.", f"Server phải từ 1 đến {len(servers)}.")), err=True)
        return None
    return servers[server_no - 1]


def check_dependencies() -> bool:
    missing = [name for name in ("yt_dlp", "cryptography") if importlib.util.find_spec(name) is None]
    if missing:
        log(red(t(f"Missing library ({', '.join(missing)}). Run: python -m pip install -r requirements.txt",
                  f"Thiếu thư viện ({', '.join(missing)}). Chạy: python -m pip install -r requirements.txt")),
            err=True)
        return False
    if not has_h2_client():
        warn(t('No HTTP/2 (curl or httpx[http2]); Cloudflare may block the requests. '
               'Run: python -m pip install "httpx[http2]"',
               'Không có HTTP/2 (curl hoặc httpx[http2]); có thể bị Cloudflare chặn. '
               'Chạy: python -m pip install "httpx[http2]"'))
    return True


def resolve_jobs(url: str, todo: list[Job], tmp_dir: str, quality: Optional[str], ffmpeg: Optional[str]) -> None:
    """Get the stream, pick the quality and estimate the size of every episode, in parallel."""
    log("\n" + cyan(t(f"Getting links for {len(todo)} episode(s)...", f"Đang lấy link {len(todo)} tập...")))

    def resolve(job: Job):
        embed = job.ep["embed"]
        try:
            text, pl_url, source = open_episode(url, embed)
            variants = parse_variants(text, pl_url)
            chosen = pick_variant(variants, quality) if variants else None
            media, media_url = media_of(source, chosen) if chosen else (text, pl_url)
            job.playlist, job.ads_removed = clean_playlist(media, media_url)
            if chosen:
                job.stream = f"{chosen['width']}x{chosen['height']} {chosen['bandwidth']}"
        except Exception as ex:
            job.fail(describe_error(embed, ex))
            log(f"  {red('✗')} {job.label}: {red(job.error)}", err=True)
            return
        fd, job.src = tempfile.mkstemp(suffix=".m3u8", dir=tmp_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(job.playlist)
        job.quality = describe_quality(chosen, job.playlist, embed, ffmpeg)
        try:
            job.size, job.duration = estimate_size(job.playlist, embed)
        except Exception:
            job.size, job.duration = None, 0.0
        details = [
            bold(job.quality),
            t("only quality", "chất lượng duy nhất") if not variants else
            t(f"best of {len(variants)}", f"cao nhất trong {len(variants)} bản") if chosen is variants[0] else
            t(f"{len(variants)} available", f"có {len(variants)} bản"),
            f"~{fmt_bytes(job.size)}" if job.size else None,
            fmt_bitrate(job.size, job.duration) if job.size else None,
            fmt_eta(job.duration) if job.duration else None,
            t(f"{job.ads_removed} ad segments removed", f"đã bỏ {job.ads_removed} đoạn quảng cáo")
            if job.ads_removed else None,
        ]
        log(f"  {gray('•')} {job.label}: " + gray(" · ").join(d for d in details if d))

    run_parallel(resolve, todo, 3)


def download_jobs(ready: list[Job], aria2: Optional[str], ffmpeg: Optional[str],
                  jobs: int, fragments: int) -> None:
    board.start([(j.label, j.size) for j in ready])

    def download(job: Job):
        if board.enabled:
            board.update(job.label, note=t("starting...", "đang chuẩn bị..."))
        else:
            log(f"> {job.label}: " + t("start", "bắt đầu") + f" -> {os.path.basename(job.out)}")
        try:
            segments = parse_segments(job.playlist) if aria2 else None
            if aria2 and not segments:
                log(gray(f"  {job.label}: " + t("special playlist, using yt-dlp",
                                                 "playlist dạng đặc biệt, chuyển sang yt-dlp")))
            if segments:
                final = download_aria2(aria2, segments, job.out, job.ep["embed"], job.label, ffmpeg,
                                       connections=fragments, expected=job.size, tag=job.stream)
            else:
                final = download_ytdlp(job.src, job.out, job.ep["embed"], job.label, ffmpeg,
                                       fragments=fragments, expected=job.size)
            size = os.path.getsize(final)
            job.status = "ok"
            board.finish(job.label, True, size)
            log(f"  {bold_green('✓')} {bold(job.label)}  {green(os.path.basename(final))}  {gray(fmt_bytes(size))}")
        except Exception as ex:
            job.fail(str(ex))
            board.finish(job.label, False)
            log(f"  {bold_red('✗')} {bold(job.label)}  {red(job.error)}", err=True)

    try:
        run_parallel(download, ready, jobs)
    finally:
        board.stop()


def write_failed(out_dir: Path, url: str, failed: list[Job]) -> Optional[Path]:
    """failed.txt in the output folder: each failed episode with its link and reason.
    Removed again when nothing fails."""
    path = out_dir / "failed.txt"
    if not failed:
        _remove(path)
        return None
    lines = [url, ""]
    for job in failed:
        lines.append(f"{job.label}  {job.ep.get('embed', '')}")
        lines.append(f"    {job.error}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(args) -> int:
    url = args.url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        # "movie-name", or a link pasted without https:// (phim.nguonc.com/phim/movie-name)
        url = f"https://{url}" if "/" in url else f"{SITE}/phim/{url}"

    log(bold_cyan("nguonc-dl") + gray(f" v{__version__}"))
    log(gray(t("Reading movie page: ", "Đang đọc trang phim: ") + url))
    try:
        info = scrape(url)
    except Exception as ex:
        log(red(t("Error: ", "Lỗi: ") + describe_error(url, ex)), err=True)
        return 1

    title, year = info["english_title"], info["year"]
    log("\n  " + bold(title) + (gray(f" ({year})") if year else ""))
    servers = info["servers"]
    if not servers:
        log(red(t("This movie has no servers.", "Phim này không có server nào.")), err=True)
        return 1
    for i, s in enumerate(servers, 1):
        log(f"  {cyan(f'[{i}]')} {s['name']} {gray('·')} " + t(f"{len(s['list'])} episode(s)", f"{len(s['list'])} tập"))

    if args.list:
        for i, s in enumerate(servers, 1):
            log("\n" + bold(f"Server {i}: {s['name']}"))
            for j, ep in enumerate(s["list"], 1):
                log(f"  {gray(f'{j:>3}.')} {ep.get('name')}")
        return 0

    server = choose_server(servers, args)
    if server is None:
        return 2
    episodes = server["list"]
    if not episodes:
        log(red(t("This server has no episodes.", "Server này không có tập nào.")), err=True)
        return 1

    # Series: download every episode by default; -e picks some of them.
    try:
        picked = parse_selection(args.eps or "all", len(episodes))
    except ValueError as ex:
        log(red(t("Episode selection: ", "Lỗi chọn tập: ") + str(ex)), err=True)
        return 2
    if not picked:
        log(red(t("No episode selected.", "Không có tập nào được chọn.")), err=True)
        return 2

    if not check_dependencies():
        return 1
    ffmpeg = find_ffmpeg()
    labels = episode_labels(episodes)
    if args.info:
        return show_sizes(url, episodes, labels, picked, not args.eps, ffmpeg)

    if not ffmpeg:
        warn(t("ffmpeg not found, files will be saved as .ts "
               "(put ffmpeg.exe next to nguonc-dl.py or run `python -m pip install imageio-ffmpeg`).",
               "Không tìm thấy ffmpeg, file sẽ được lưu dạng .ts "
               "(đặt ffmpeg.exe cạnh nguonc-dl.py hoặc chạy `python -m pip install imageio-ffmpeg`)."))
    aria2 = find_tool("aria2c") if args.downloader in ("auto", "aria2") else None
    if args.downloader == "aria2" and not aria2:
        log(red(t("aria2c not found: put aria2c.exe next to nguonc-dl.py or install it on PATH.",
                  "Không tìm thấy aria2c: đặt aria2c.exe cạnh nguonc-dl.py hoặc cài vào PATH.")), err=True)
        return 1
    log(gray(t("Downloader: ", "Bộ tải: ") + ("aria2c" if aria2 else "yt-dlp")
             + "  ·  ffmpeg: " + (t("yes", "có") if ffmpeg else t("no", "không"))))
    if not aria2 and args.downloader == "auto":
        log(yellow(aria2_tip()))

    out_dir = Path(args.output).expanduser() if args.output else SCRIPT_DIR / "downloads"
    if not args.no_subfolder:
        out_dir = out_dir / safe_name(f"{title} ({year})" if year else title)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        log(red(t(f"Cannot create the output folder {out_dir}: {ex}",
                  f"Không tạo được thư mục lưu {out_dir}: {ex}")), err=True)
        return 1

    jobs = max(1, min(args.jobs, 8, len(picked)))
    if args.fragments is None:
        fragments = 16 if aria2 else 8
    else:
        fragments = max(1, min(args.fragments, 64 if aria2 else 32))

    filenames = build_filenames(title, year, episodes)
    all_jobs = [Job(labels[i], episodes[i], str(out_dir / filenames[i])) for i in picked]

    todo = []
    for job in all_jobs:
        done_files = [job.out] + ([] if ffmpeg else [os.path.splitext(job.out)[0] + ".ts"])
        existing = next((f for f in done_files if os.path.exists(f) and os.path.getsize(f) > 0), None)
        if existing:
            log(f"  {gray('=')} {job.label}: " + gray(t("already there, skipped", "đã có, bỏ qua")
                                                      + f" ({os.path.basename(existing)})"))
            job.status = "skip"
        else:
            todo.append(job)

    tmp_dir = tempfile.mkdtemp(prefix="nguonc-dl-")
    try:
        resolve_jobs(url, todo, tmp_dir, args.quality, ffmpeg)
        ready = [j for j in todo if j.src]
        total_size = sum(j.size or 0 for j in ready)
        if total_size:
            unknown = sum(1 for j in ready if not j.size)
            log("\n" + t("Estimated size: ", "Dung lượng ước tính: ") + bold(f"~{fmt_bytes(total_size)}")
                + (gray(t(f" ({unknown} episodes unknown)", f" (chưa tính {unknown} tập không đo được)"))
                   if unknown else ""))
            free = shutil.disk_usage(out_dir).free
            if free < total_size * 1.05:
                warn(t(f"Only {fmt_bytes(free)} free on this drive, it may not be enough.",
                       f"Ổ đĩa chỉ còn trống {fmt_bytes(free)}, có thể không đủ chỗ."))
        if ready:
            log(t(f"Downloading {len(ready)} episode(s) to ", f"Tải {len(ready)} tập vào ") + bold(str(out_dir))
                + gray(t(f"  ({min(jobs, len(ready))} at a time × {fragments} segments)",
                         f"  ({min(jobs, len(ready))} tập cùng lúc × {fragments} đoạn)")) + "\n")
            download_jobs(ready, aria2, ffmpeg, jobs, fragments)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    ok = sum(1 for j in all_jobs if j.status in ("ok", "skip"))
    failed = [j for j in all_jobs if j.status == "fail"]
    failed_file = write_failed(out_dir, url, failed)
    done_txt = t(f"Done: {ok}/{len(all_jobs)} episode(s).", f"Hoàn tất: {ok}/{len(all_jobs)} tập.")
    log("\n" + (bold_green(done_txt) if not failed else bold_red(done_txt)) + " " + gray(str(out_dir)))
    if failed_file:
        log(yellow(t(f"Failed episodes and reasons: {failed_file}", f"Tập lỗi và lý do: {failed_file}")))
        log(gray(t("Run the same command again to retry them.", "Chạy lại đúng lệnh cũ để thử lại.")))
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nguonc-dl",
        description=t("nguonc-dl: download movies and series from phim.nguonc.com.",
                      "nguonc-dl: tải phim lẻ và phim bộ từ phim.nguonc.com."),
        epilog=t('Example: python nguonc-dl.py "https://phim.nguonc.com/phim/movie-name" -e 1-5\n\n'
                 "aria2c is recommended for much faster downloads: put aria2c.exe next to nguonc-dl.py\n"
                 "(https://github.com/aria2/aria2/releases).",
                 'Ví dụ: python nguonc-dl.py "https://phim.nguonc.com/phim/ten-phim" -e 1-5\n\n'
                 "Khuyên dùng aria2c để tải nhanh hơn nhiều: đặt aria2c.exe cạnh nguonc-dl.py\n"
                 "(https://github.com/aria2/aria2/releases)."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help=t("movie link on phim.nguonc.com, or just its name (e.g. movie-name)",
                                 "link phim trên phim.nguonc.com, hoặc chỉ tên phim (vd: ten-phim)"))
    p.add_argument("-s", "--server", type=int, help=t("server number: 1, 2, ... (default: ask, or 1)",
                                                      "số thứ tự server: 1, 2, ... (mặc định: hỏi, hoặc 1)"))
    p.add_argument("-e", "--eps", help=t('only some episodes: "1-5,8", "10-" (default: all)',
                                         'chỉ tải một số tập: "1-5,8", "10-" (mặc định: tải hết)'))
    p.add_argument("-q", "--quality", default="best",
                   help=t("quality when there are several: best, worst, 1080, 720, ... (default: best)",
                          "chất lượng khi có nhiều bản: best, worst, 1080, 720, ... (mặc định: best)"))
    p.add_argument("-i", "--info", action="store_true",
                   help=t("only show the estimated size of each quality, do not download",
                          "chỉ xem dung lượng ước tính của từng chất lượng, không tải"))
    p.add_argument("-o", "--output", help=t("output folder (default: downloads/ next to nguonc-dl.py)",
                                            "thư mục lưu (mặc định: downloads/ cạnh nguonc-dl.py)"))
    p.add_argument("-j", "--jobs", type=int, default=2, help=t("episodes downloaded at once, 1-8 (default: 2)",
                                                               "số tập tải cùng lúc, 1-8 (mặc định: 2)"))
    p.add_argument("-n", "--fragments", type=int,
                   help=t("segments downloaded at once per episode (default: 16 with aria2c, 8 with yt-dlp)",
                          "số đoạn video tải cùng lúc trong mỗi tập (mặc định: 16 với aria2c, 8 với yt-dlp)"))
    p.add_argument("-d", "--downloader", choices=("auto", "aria2", "ytdlp"), default="auto",
                   help=t("auto = aria2c if found (next to the script or on PATH), else yt-dlp",
                          "auto = aria2c nếu có (cạnh script hoặc trong PATH), không thì yt-dlp"))
    p.add_argument("--no-subfolder", action="store_true",
                   help=t("do not make a subfolder named after the movie", "không tạo thư mục con theo tên phim"))
    p.add_argument("-l", "--list", action="store_true",
                   help=t("only list servers and episodes", "chỉ liệt kê server và tập"))
    p.add_argument("-y", "--yes", action="store_true", help=t("never ask, use defaults", "không hỏi gì, dùng mặc định"))
    p.add_argument("--lang", choices=("en", "vi"), default=LANG,
                   help=t("message language (default: auto)", "ngôn ngữ thông báo (mặc định: tự nhận)"))
    p.add_argument("--no-color", action="store_true", help=t("plain text, no colors", "chữ trơn, không màu"))
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    global LANG
    argv = sys.argv[1:] if argv is None else argv
    LANG = detect_lang(argv)
    if LANG not in ("en", "vi"):
        LANG = "en"
    parser = build_parser()
    args = parser.parse_args(argv)
    LANG = args.lang
    init_console(not args.no_color)
    if not re.fullmatch(r"best|worst|\d+p?", args.quality.strip().lower()):
        parser.error(t(f"invalid -q: {args.quality!r} (e.g. best, worst, 1080, 720p)",
                       f"-q không hợp lệ: {args.quality!r} (vd: best, worst, 1080, 720p)"))

    try:
        return run(args)
    except KeyboardInterrupt:
        kill_children()
        board.stop()
        log("\n" + yellow(t("Stopped. Run the same command again to continue.",
                            "Đã dừng. Chạy lại đúng lệnh cũ để tải tiếp.")), err=True)
        # yt-dlp threads cannot be stopped by Ctrl+C; end the whole process.
        os._exit(130)


if __name__ == "__main__":
    sys.exit(main())
