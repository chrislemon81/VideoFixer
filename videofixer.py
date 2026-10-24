#!/usr/bin/env python3
"""
VideoFixer - recover playable video from corrupted camcorder / dashcam files.

Cheap camcorders write the "table of contents" of a video (the MP4/MOV `moov`
box, or the AVI header and index) only when recording stops cleanly.  If the
battery dies, the card fills up or the camera crashes, the picture and sound
data are still on the card, but players (and Windows thumbnails) cannot find
them.  VideoFixer:

  1. tries a tolerant ffmpeg rebuild (fixes broken indexes, bad packets), and
  2. if that is not enough, scans the raw bytes of the file for video frames
     (H.264, H.265/HEVC, Motion-JPEG) and audio, and rebuilds a brand new file.

The original file is never modified.  The result is written as a new, normal
MP4 (H.264 + AAC) that plays everywhere, including Windows 11.

Usage:
    python videofixer.py                         open the window (GUI)
    python videofixer.py BROKEN.AVI [more...]    fix files or whole folders
    python videofixer.py --help                  all options

Requires ffmpeg + ffprobe (https://ffmpeg.org) on the PATH or next to this file.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

__version__ = "1.0.0"

Log = Callable[[str], None]

VIDEO_EXTS = {
    ".avi", ".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".mts", ".m2ts", ".ts",
    ".mpg", ".mpeg", ".wmv", ".asf", ".lrv",
}

STANDARD_FPS = (10, 12, 15, 20, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 119.88, 120)
STANDARD_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
DEFAULT_FPS = 30.0

START_CODE = b"\x00\x00\x00\x01"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def u16be(b, o):
    return struct.unpack_from(">H", b, o)[0]


def u32be(b, o):
    return struct.unpack_from(">I", b, o)[0]


def u16le(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32le(b, o):
    return struct.unpack_from("<I", b, o)[0]


def is_fourcc(b) -> bool:
    return len(b) == 4 and all(32 <= c < 127 or c == 0xA9 for c in b)


def fmt_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def snap(value: float, choices, tolerance: float) -> float:
    """Round `value` to the nearest standard value if it is within `tolerance` (relative)."""
    best = min(choices, key=lambda c: abs(c - value))
    return float(best) if abs(best - value) <= best * tolerance else value


def default_out_dir() -> Path:
    videos = Path.home() / "Videos"
    return (videos if videos.is_dir() else Path.home()) / "VideoFixer"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def _console_print(msg: str) -> None:
    if sys.stdout is not None:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode(), flush=True)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):          # PyInstaller .exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_tool(name: str) -> Optional[str]:
    exe = name + (".exe" if os.name == "nt" else "")
    here = _app_dir()
    candidates = [here / exe, here / "ffmpeg" / exe, here / "ffmpeg" / "bin" / exe]
    if hasattr(sys, "_MEIPASS"):
        candidates.insert(0, Path(sys._MEIPASS) / exe)
    for c in candidates:
        if c.is_file():
            return str(c)
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        # winget installs ffmpeg here, but the PATH change only shows up in new windows.
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        link = local / "Microsoft" / "WinGet" / "Links" / exe
        if link.is_file():
            return str(link)
        for c in (local / "Microsoft" / "WinGet" / "Packages").glob(f"*FFmpeg*/**/bin/{exe}"):
            return str(c)
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:"):
            c = Path(base) / "ffmpeg" / "bin" / exe
            if c.is_file():
                return str(c)
    return None


def _subprocess_kwargs() -> dict:
    kw = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flashing up
    return kw


class Tools:
    def __init__(self):
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


MISSING_FFMPEG = (
    "ffmpeg was not found.\n\n"
    "VideoFixer needs the free ffmpeg program. To install it on Windows, open\n"
    "PowerShell and run:\n\n"
    "    winget install Gyan.FFmpeg\n\n"
    "then close and reopen VideoFixer. (Or download ffmpeg from\n"
    "https://www.gyan.dev/ffmpeg/builds/ and put ffmpeg.exe and ffprobe.exe\n"
    "in the same folder as VideoFixer.)"
)


def probe(tools: Tools, path: Path) -> Tuple[Optional[dict], str]:
    cmd = [tools.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600, **_subprocess_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    err = r.stderr.decode("utf-8", "replace").strip()
    try:
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        return None, err
    if not data.get("streams"):
        return None, err
    return data, err


def probe_summary(data: Optional[dict]) -> Tuple[bool, bool, float, Optional[str]]:
    """-> (has_video, has_audio, duration_seconds, video_codec_name)"""
    if not data:
        return False, False, 0.0, None
    streams = data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video"
            and s.get("disposition", {}).get("attached_pic") != 1]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    durations = []
    for item in [data.get("format", {})] + streams:
        try:
            durations.append(float(item.get("duration", 0) or 0))
        except (TypeError, ValueError):
            pass
    dur = max(durations) if durations else 0.0
    return bool(vids), bool(auds), dur, (vids[0].get("codec_name") if vids else None)


def run_ffmpeg(tools: Tools, args: List[str], log: Log, expected: float = 0.0) -> Tuple[bool, str]:
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-nostats", "-progress", "pipe:1"] + args
    with tempfile.TemporaryFile() as errf:
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                 universal_newlines=True, **_subprocess_kwargs())
        except OSError as e:
            return False, str(e)
        with p:
            rc = _follow_progress(p, log, expected)
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
    return rc == 0, err


def _follow_progress(p: subprocess.Popen, log: Log, expected: float) -> int:
    last = time.monotonic()
    for line in p.stdout:
        if line.startswith("out_time_us=") and time.monotonic() - last > 4:
            last = time.monotonic()
            try:
                t = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            if expected > 0:
                log(f"  encoding... {min(99, int(100 * t / expected))}%  ({fmt_time(t)} of {fmt_time(expected)})")
            else:
                log(f"  encoding... {fmt_time(t)} done")
    return p.wait()


def last_lines(text: str, n: int = 4) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return "\n".join("    " + l for l in lines[-n:])


# ---------------------------------------------------------------------------
# Stream descriptions
# ---------------------------------------------------------------------------

@dataclass
class VideoInfo:
    codec: Optional[str] = None          # 'h264', 'hevc', 'mjpeg', 'mpeg4'
    extradata: Optional[bytes] = None    # avcC / hvcC box payload
    fps: Optional[float] = None
    width: int = 0
    height: int = 0

    @property
    def nal_length_size(self) -> int:
        e = self.extradata
        try:
            if self.codec == "h264" and e and len(e) >= 5:
                return (e[4] & 3) + 1
            if self.codec == "hevc" and e and len(e) >= 22:
                return (e[21] & 3) + 1
        except IndexError:
            pass
        return 4

    def parameter_sets(self) -> List[bytes]:
        """SPS/PPS (and VPS) NAL units from avcC / hvcC."""
        e, out = self.extradata, []
        if not e:
            return out
        try:
            if self.codec == "h264":
                pos, n = 6, e[5] & 0x1F
                for _ in range(n):
                    ln = u16be(e, pos)
                    out.append(e[pos + 2:pos + 2 + ln])
                    pos += 2 + ln
                n = e[pos]
                pos += 1
                for _ in range(n):
                    ln = u16be(e, pos)
                    out.append(e[pos + 2:pos + 2 + ln])
                    pos += 2 + ln
            elif self.codec == "hevc":
                pos, arrays = 23, e[22]
                for _ in range(arrays):
                    n = u16be(e, pos + 1)
                    pos += 3
                    for _ in range(n):
                        ln = u16be(e, pos)
                        out.append(e[pos + 2:pos + 2 + ln])
                        pos += 2 + ln
        except (IndexError, struct.error):
            pass
        return [x for x in out if x]


@dataclass
class AudioInfo:
    pcm: Optional[str] = None            # e.g. 's16le', 's16be', 'u8', 'mulaw' (None = not PCM)
    fourcc: bytes = b""
    sample_rate: int = 0
    channels: int = 0

    @property
    def describe(self) -> str:
        name = self.pcm or self.fourcc.decode("latin-1").strip() or "unknown"
        return f"{name}, {self.sample_rate} Hz, {self.channels} ch"


VIDEO_FOURCCS = {
    b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc",
    b"jpeg": "mjpeg", b"mjpa": "mjpeg", b"mjpb": "mjpeg", b"AVDJ": "mjpeg",
    b"dmb1": "mjpeg", b"MJPG": "mjpeg", b"mp4v": "mpeg4",
}

AVI_FOURCCS = {
    b"MJPG": "mjpeg", b"AVRN": "mjpeg", b"DMB1": "mjpeg", b"JPEG": "mjpeg", b"MJPA": "mjpeg",
    b"H264": "h264", b"X264": "h264", b"AVC1": "h264", b"DAVC": "h264", b"H.264": "h264",
    b"HEVC": "hevc", b"H265": "hevc", b"HEV1": "hevc", b"HVC1": "hevc",
    b"XVID": "mpeg4", b"DIVX": "mpeg4", b"DX50": "mpeg4", b"FMP4": "mpeg4", b"MP4V": "mpeg4",
}

RAW_DEMUXER = {"h264": "h264", "hevc": "hevc", "mjpeg": "mjpeg", "mpeg4": "m4v"}


def pcm_format(fourcc: bytes, bits: int, flags: int = 0) -> Optional[str]:
    if fourcc == b"sowt":
        return {8: "s8", 24: "s24le", 32: "s32le"}.get(bits, "s16le")
    if fourcc == b"twos":
        return {8: "s8", 24: "s24be", 32: "s32be"}.get(bits, "s16be")
    if fourcc in (b"raw ", b"NONE"):
        return "u8" if bits == 8 else "s16be"
    simple = {b"ulaw": "mulaw", b"alaw": "alaw", b"in24": "s24be", b"in32": "s32be",
              b"fl32": "f32be", b"fl64": "f64be"}
    if fourcc in simple:
        return simple[fourcc]
    if fourcc == b"lpcm" and bits in (8, 16, 24, 32, 64):
        endian = "be" if flags & 2 else "le"
        if flags & 1:
            return f"f{bits}{endian}"
        if bits == 8:
            return "s8" if flags & 4 else "u8"
        return ("s" if flags & 4 else "u") + f"{bits}{endian}"
    return None


# ---------------------------------------------------------------------------
# WAV writer for recovered audio (lets ffmpeg read any PCM/ADPCM reliably)
# ---------------------------------------------------------------------------

def pcm_wav_fmt(pcm: str, rate: int, channels: int) -> Tuple[bytes, int, Optional[Callable[[bytes], bytes]]]:
    """-> (fmt chunk, block align, transform) to store `pcm` samples in a WAV file."""
    transform: Optional[Callable[[bytes], bytes]] = None
    if pcm in ("mulaw", "alaw"):
        tag, bits = (7 if pcm == "mulaw" else 6), 8
    else:
        kind, bits, endian = pcm[0], int(re.sub(r"\D", "", pcm)), pcm[-2:]
        tag = 3 if kind == "f" else 1
        width = bits // 8
        if width > 1 and endian == "be":       # WAV is little-endian: swap bytes
            def swap(data, w=width):
                out = bytearray(len(data))
                for k in range(w):
                    out[k::w] = data[w - 1 - k::w]
                return bytes(out)
            transform = swap
        elif bits == 8 and kind == "s":          # WAV 8-bit is unsigned
            table = bytes((i ^ 0x80) for i in range(256))

            def to_unsigned(data):
                return data.translate(table)
            transform = to_unsigned
    block = max(1, channels * bits // 8)
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)
    return fmt, block, transform


class WavWriter:
    """Writes audio data now, the WAV header (whose values may still change) on close."""

    def __init__(self, path: Path, fmt_len: int = 16):
        self.path = path
        self.f = open(path, "wb")
        self.fmt_len = fmt_len
        self.f.write(b"\0" * self._header_len())
        self.bytes = 0
        self.align = 1
        self.transform: Optional[Callable[[bytes], bytes]] = None
        self.strf: Optional[bytes] = None     # original WAVEFORMATEX (AVI audio)

    def _header_len(self) -> int:
        return 12 + 8 + self.fmt_len + (self.fmt_len & 1) + 8

    def write(self, data: bytes) -> None:
        if self.align > 1:
            data = data[:len(data) - len(data) % self.align]
        if not data:
            return
        if self.transform:
            data = self.transform(data)
        self.f.write(data)
        self.bytes += len(data)

    def close(self, fmt: bytes) -> None:
        fmt = fmt[:self.fmt_len].ljust(self.fmt_len, b"\0")
        if self.bytes & 1:
            self.f.write(b"\0")
        total = self._header_len() - 8 + self.bytes + (self.bytes & 1)
        self.f.seek(0)
        self.f.write(b"RIFF" + struct.pack("<I", min(total, 0xFFFFFFFF)) + b"WAVE")
        self.f.write(b"fmt " + struct.pack("<I", self.fmt_len) + fmt + (b"\0" if self.fmt_len & 1 else b""))
        self.f.write(b"data" + struct.pack("<I", min(self.bytes, 0xFFFFFFFF)))
        self.f.close()

    def discard(self) -> None:
        if not self.f.closed:
            self.f.close()


# ---------------------------------------------------------------------------
# MP4 / MOV structure
# ---------------------------------------------------------------------------

MP4_TOP = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid",
           b"meta", b"pdin", b"moof", b"mfra", b"styp", b"sidx", b"junk"}


def read_atoms(buf, start: int, end: int):
    """Yield (offset, size, type, header_len) of consecutive boxes. Size may run past `end`."""
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
            if size == 0:
                size = end - pos
        elif size == 0:
            size = end - pos
        if size < hdr or not is_fourcc(typ):
            return
        yield pos, size, typ, hdr
        pos += size


def find_child(buf, start: int, end: int, typ: bytes) -> Optional[Tuple[int, int]]:
    for o, s, t, h in read_atoms(buf, start, end):
        if t == typ:
            return o + h, min(o + s, end)
    return None


def parse_moov(buf, start: int, end: int) -> Tuple[Optional[VideoInfo], Optional[AudioInfo]]:
    video = audio = None
    for o, s, t, h in read_atoms(buf, start, end):
        if t != b"trak":
            continue
        try:
            res = _parse_trak(buf, o + h, min(o + s, end))
        except (struct.error, IndexError, ValueError, OverflowError):
            continue
        if not res:
            continue
        if res[0] == "video" and video is None:
            video = res[1]
        elif res[0] == "audio" and audio is None:
            audio = res[1]
    return video, audio


def _parse_trak(buf, s: int, e: int):
    mdia = find_child(buf, s, e, b"mdia")
    if not mdia:
        return None
    hd = find_child(buf, *mdia, b"hdlr")
    handler = bytes(buf[hd[0] + 8:hd[0] + 12]) if hd else b""
    timescale = 0
    md = find_child(buf, *mdia, b"mdhd")
    if md:
        timescale = u32be(buf, md[0] + (20 if buf[md[0]] == 1 else 12))
    minf = find_child(buf, *mdia, b"minf")
    stbl = find_child(buf, *minf, b"stbl") if minf else None
    if not stbl:
        return None
    stsd = find_child(buf, *stbl, b"stsd")
    if not stsd or stsd[1] - stsd[0] < 16:
        return None
    es = stsd[0] + 8
    ee = min(es + u32be(buf, es), stsd[1])
    fourcc = bytes(buf[es + 4:es + 8])

    if handler == b"vide" or fourcc in VIDEO_FOURCCS:
        vi = VideoInfo(codec=VIDEO_FOURCCS.get(fourcc))
        if es + 36 <= ee:
            vi.width, vi.height = u16be(buf, es + 32), u16be(buf, es + 34)
        for o, sz, t, h in read_atoms(buf, es + 86, ee):
            if t in (b"avcC", b"hvcC"):
                vi.extradata = bytes(buf[o + h:min(o + sz, ee)])
        st = find_child(buf, *stbl, b"stts")
        if st and timescale:
            count = u32be(buf, st[0] + 4)
            deltas = Counter()
            for i in range(min(count, 4096)):
                p = st[0] + 8 + 8 * i
                if p + 8 > st[1]:
                    break
                c, d = struct.unpack_from(">II", buf, p)
                if d:
                    deltas[d] += c
            if deltas:
                fps = timescale / deltas.most_common(1)[0][0]
                if 1 <= fps <= 300:
                    vi.fps = fps
        return "video", vi

    if handler == b"soun":
        ver = u16be(buf, es + 16)
        channels, bits = u16be(buf, es + 24), u16be(buf, es + 26)
        rate = u32be(buf, es + 32) / 65536.0
        flags = 0
        if ver == 2 and es + 64 <= ee:
            rate = struct.unpack_from(">d", buf, es + 40)[0]
            channels, bits, flags = u32be(buf, es + 48), u32be(buf, es + 56), u32be(buf, es + 60)
        return "audio", AudioInfo(pcm=pcm_format(fourcc, bits, flags), fourcc=fourcc,
                                  sample_rate=int(round(rate)), channels=channels)
    return None


def mp4_layout(buf, size: int) -> Tuple[List[Tuple[int, int]], Optional[Tuple[int, int]], List[str]]:
    """-> (media data regions, moov range or None, notes)"""
    regions, moov, covered, notes = [], None, 0, []
    for o, s, t, h in read_atoms(buf, 0, size):
        e = min(o + s, size)
        if t == b"mdat":
            regions.append((o + h, e))
            if o + s > size:
                notes.append("the media data is cut off (file truncated)")
        elif t == b"moov":
            moov = (o + h, e)
            if o + s > size:
                notes.append("the index (moov) is cut off")
        covered = e
    if not regions:
        i = buf.find(b"mdat", 0, min(size, 64 << 20))
        if i >= 4:
            regions.append((i + 4, size))
            covered = size
            notes.append("the file header is damaged")
    if regions and covered < size - 4096:
        regions.append((covered, size))        # structure broke off; scan the rest too
        notes.append(f"damaged data after offset {covered}")
    if not moov:
        notes.insert(0, "the index (moov) is missing - recording was probably interrupted")
    return regions, moov, notes


# ---------------------------------------------------------------------------
# H.264 / H.265 frame scanner (length-prefixed NAL units, as stored in MP4/MOV)
# ---------------------------------------------------------------------------

class BitReader:
    """Reads bits / Exp-Golomb codes from the start of a byte string."""

    def __init__(self, data: bytes):
        self.v = int.from_bytes(data, "big")
        self.n = len(data) * 8
        self.p = 0

    def u(self, k: int) -> int:
        if k == 0:
            return 0
        if self.p + k > self.n:
            raise ValueError("out of bits")
        self.p += k
        return (self.v >> (self.n - self.p)) & ((1 << k) - 1)

    def ue(self) -> int:
        zeros = 0
        while not self.u(1):
            zeros += 1
            if zeros > 31:
                raise ValueError("bad Exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def unescape(nal: bytes) -> bytes:
    """Remove H.264/H.265 emulation-prevention bytes (00 00 03 -> 00 00)."""
    return nal.replace(b"\x00\x00\x03", b"\x00\x00")


H264_PROFILES = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
H264_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def parse_h264_sps(nal: bytes) -> Tuple[int, int, int, int]:
    """-> (sps_id, log2_max_frame_num, separate_colour_plane_flag, raw frame size in bytes)"""
    r = BitReader(unescape(nal[1:]))
    profile = r.u(8)
    r.u(16)
    sps_id = r.ue()
    separate = 0
    if profile in H264_HIGH_PROFILES:
        chroma = r.ue()
        if chroma == 3:
            separate = r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        if r.u(1):                                  # scaling matrices present
            for i in range(12 if chroma == 3 else 8):
                if r.u(1):
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    log2_fn = r.ue() + 4
    raw_size = 0
    try:
        poc_type = r.ue()
        if poc_type == 0:
            r.ue()
        elif poc_type == 1:
            r.u(1)
            r.se()
            r.se()
            for _ in range(r.ue()):
                r.se()
        r.ue()
        r.u(1)
        w_mbs, h_map = r.ue() + 1, r.ue() + 1
        h_mbs = h_map * (2 - r.u(1))
        raw_size = w_mbs * h_mbs * 256 * 3 // 2
    except ValueError:
        pass
    return sps_id, log2_fn, separate, raw_size


class NalScanner:
    """Walks a byte range, picking out NAL units and reporting the gaps between them
    (in camcorder files the gaps are the interleaved audio).

    Audio bytes can look like a NAL unit by chance, and a false match claims a random
    length that would swallow real frames.  Candidates are therefore checked against the
    slice header, against the typical frame size, and (H.264) against frame_num, which
    has to continue the numbering of the previous frame."""

    MAX_NAL = 32 << 20

    def __init__(self, codec: str, length_size: int = 4, param_sets=(), check_frame_num: bool = True):
        self.hevc = codec == "hevc"
        self.ls = length_size
        self.frames = self.keyframes = self.ps_seen = 0
        self.video_bytes = self.gap_bytes = 0
        self.fn_rejects = 0
        self.cur_slice_type = self.cur_fn = self.prev_ref_fn = None
        self.check_fn = check_frame_num and not self.hevc
        self.sps, self.pps = {}, {}
        self.p_sizes, self.i_sizes = deque(maxlen=64), deque(maxlen=16)
        self.limit, self._since = self.MAX_NAL, 0
        self.frame_cap = self.MAX_NAL
        self.p_median = self.i_median = 0
        self.window, self.max_gap = 256 << 10, 0
        for nal in param_sets:
            self._learn(nal)
        if self.hevc:
            types = list(range(0, 10)) + list(range(16, 22)) + [32, 33, 34, 35, 39, 40]
            hdr = b"[" + b"".join(b"\\x%02x" % (t << 1) for t in types) + b"]\\x01"
        else:
            hb = set()
            for ref in range(4):
                for t in (1, 5, 6, 7, 8, 9):
                    if (t in (5, 7, 8) and ref == 0) or (t in (6, 9) and ref != 0):
                        continue
                    hb.add((ref << 5) | t)
            hdr = b"[" + b"".join(b"\\x%02x" % c for c in sorted(hb)) + b"]"
        if length_size >= 3:
            prefix = b"\\x00" + b"[\\x00-\\xff]" * (length_size - 1)
        else:
            prefix = b"[\\x00-\\xff]" * length_size
        self.rx = re.compile(prefix + hdr, re.DOTALL)

    def _learn(self, nal: bytes) -> None:
        """Remember H.264 SPS/PPS so slice headers can be read further (frame_num)."""
        if self.hevc or len(nal) < 2:
            return
        try:
            t = nal[0] & 0x1F
            if t == 7:
                sps_id, log2_fn, separate, raw_size = parse_h264_sps(nal)
                self.sps[sps_id] = (log2_fn, separate)
                if raw_size > 4096:
                    # an encoded frame is never bigger than the uncompressed picture
                    self.frame_cap = max(self.frame_cap if self.frame_cap < self.MAX_NAL else 0, raw_size)
            elif t == 8:
                r = BitReader(unescape(nal[1:16]))
                pps_id = r.ue()
                self.pps[pps_id] = r.ue()
        except ValueError:
            pass

    def _record_size(self, n: int, key: bool) -> None:
        (self.i_sizes if key else self.p_sizes).append(n)
        self._since += 1
        if self._since >= 8 and len(self.p_sizes) + len(self.i_sizes) >= 16:
            self._since = 0
            pm = sorted(self.p_sizes)[len(self.p_sizes) // 2] if self.p_sizes else 0
            im = sorted(self.i_sizes)[len(self.i_sizes) // 2] if self.i_sizes else 0
            self.p_median, self.i_median = pm, im
            self.limit = max(16 * pm, 4 * im, 256 << 10)

    def _kind(self, h: bytes, strict: bool, prev_vcl: bool):
        """-> (kind, first_slice, nal_type, frame_num, is_reference) or None.
        h = NAL header + up to 12 payload bytes."""
        if self.hevc:
            if h[0] & 0x81 or h[1] != 1:
                return None
            t = h[0] >> 1
            if t <= 9 or 16 <= t <= 21:
                if h[2] & 0x80:
                    try:
                        r = BitReader(h[2:10])
                        r.u(2 if t >= 16 else 1)
                        if r.ue() > (7 if strict else 63):
                            return None
                    except ValueError:
                        return None
                    return "vcl", True, t, None, True
                if strict or not prev_vcl:
                    return None
                return "vcl", False, t, None, True
            if t in (32, 33, 34):
                return "ps", False, t, None, True
            if t in (35, 39, 40):
                return "sei", False, t, None, False
            return None

        b0 = h[0]
        if b0 & 0x80:
            return None
        t, ref = b0 & 0x1F, b0 >> 5
        if t in (1, 5):
            if t == 5 and ref == 0:
                return None
            try:
                r = BitReader(unescape(h[1:]))
                first_mb, st, pps_id = r.ue(), r.ue(), r.ue()
                if st > 9 or pps_id > (7 if strict else 255):
                    return None
                st %= 5
                if t == 5 and st not in (2, 4):
                    return None
                sps = self.sps.get(self.pps.get(pps_id, -1))
                if sps is None and len(self.sps) == 1 and pps_id not in self.pps:
                    sps = next(iter(self.sps.values()))
                fn = None
                if sps:
                    if sps[1]:
                        r.u(2)
                    fn = r.u(sps[0])
            except ValueError:
                return None
            if first_mb == 0:
                if fn is not None and self.check_fn:
                    if t == 5:
                        if fn != 0:
                            return None
                    elif self.prev_ref_fn is not None and (fn - self.prev_ref_fn) % (1 << sps[0]) > 3:
                        if not strict and prev_vcl:
                            self.fn_rejects += 1
                        return None
                return "vcl", True, t, fn, bool(ref)
            if strict or not prev_vcl or first_mb > 36864 or st != self.cur_slice_type:
                return None
            if fn is not None and self.cur_fn is not None and fn != self.cur_fn:
                return None
            return "vcl", False, t, fn, bool(ref)
        if t in (7, 8) and ref:
            if t == 7 and strict and h[1] not in H264_PROFILES:
                return None
            return "ps", False, t, None, True
        if t in (6, 9) and not ref:
            return "sei", False, t, None, False
        return None

    def _header_ok(self, buf, pos: int, end: int) -> bool:
        ls = self.ls
        if pos + ls + 2 > end:
            return False
        n = int.from_bytes(buf[pos:pos + ls], "big")
        if n < 2 or pos + ls + n > end:
            return False
        h0 = buf[pos + ls]
        if self.hevc:
            t = h0 >> 1
            return not h0 & 0x81 and buf[pos + ls + 1] == 1 and (t <= 21 or 32 <= t <= 40)
        return not h0 & 0x80 and (h0 & 0x1F) in (1, 5, 6, 7, 8, 9)

    def classify(self, buf, pos: int, end: int, strict: bool, prev_vcl: bool):
        """-> (length, kind, first_slice, nal_type, frame_num, is_reference) or None"""
        ls = self.ls
        hlen = 2 if self.hevc else 1
        if pos + ls + hlen + 1 > end:
            return None
        n = int.from_bytes(buf[pos:pos + ls], "big")
        if n <= hlen or pos + ls + n > end or n > self.limit or n > self.frame_cap:
            return None
        p = pos + ls
        h = bytes(buf[p:p + min(n, hlen + 12)]).ljust(hlen + 12, b"\0")
        res = self._kind(h, strict, prev_vcl)
        if res is None:
            return None
        kind = res[0]
        if kind == "vcl":
            if strict and n < 16:
                return None
        elif (kind == "ps" and n > 4096) or not self._header_ok(buf, pos + ls + n, end):
            # parameter sets / SEI / delimiters are small and always followed by more NAL units
            return None
        return (n,) + res

    def _candidate(self, buf, pos: int, end: int, limit: int):
        """Next position in [pos, limit) that passes the strict NAL checks -> (pos, classify result)."""
        while True:
            m = self.rx.search(buf, pos, limit)
            if not m:
                return None
            p = m.start()
            r = self.classify(buf, p, end, True, False)
            if r:
                return p, r
            pos = p + 1

    def _score(self, buf, p: int, r, end: int) -> Tuple[int, int]:
        """How convincing a resync candidate is -> (score, best possible score without a chain)."""
        n, kind, first, t, fn, ref = r
        score = 2 if self._header_ok(buf, p + self.ls + n, end) else 0
        best = 0
        if kind == "vcl" and first and fn is not None and self.check_fn:
            best += 1
            if t == 5:
                score += fn == 0
            elif self.prev_ref_fn is not None:
                log2 = next(iter(self.sps.values()))[0] if len(self.sps) == 1 else None
                if log2 and fn == (self.prev_ref_fn + 1) % (1 << log2):
                    score += 1
        if kind == "vcl":
            key = (16 <= t <= 21) if self.hevc else t == 5
            median = self.i_median if key else self.p_median
            if median:
                best += 1
                score += n <= 3 * median
        return score, max(best, 1)

    def resync(self, buf, pos: int, end: int) -> int:
        """Find where the video continues after a gap (usually an audio chunk).  Audio can
        contain false matches before the real next frame, so a weak first match is compared
        with the other candidates nearby and the most convincing one wins."""
        c = self._candidate(buf, pos, end, end)
        if c is None:
            return end
        best_pos = c[0]
        best_score, possible = self._score(buf, c[0], c[1], end)
        if best_score < min(2, possible):
            # only a match inside the claimed length of the first one competes with it
            limit = min(end, c[0] + self.window, c[0] + self.ls + c[1][0])
            q = c[0] + 1
            while True:
                c = self._candidate(buf, q, end, limit)
                if c is None:
                    break
                sc, _ = self._score(buf, c[0], c[1], end)
                if sc > best_score:
                    best_pos, best_score = c[0], sc
                    if sc >= min(2, possible):
                        break
                q = c[0] + 1
        if best_score >= min(2, possible):
            gap = best_pos - pos
            if gap > self.max_gap:
                self.max_gap = gap
                self.window = min(4 << 20, max(256 << 10, 2 * gap))
        return best_pos

    def scan(self, buf, start: int, end: int, on_nal=None, on_gap=None, progress=None) -> None:
        ls, pos, prev_vcl = self.ls, start, False
        step = max((end - start) // 20, 1 << 20)
        next_report = start + step
        while pos < end:
            r = self.classify(buf, pos, end, False, prev_vcl)
            if r is None:
                nxt = self.resync(buf, pos + 1, end)
                self.gap_bytes += nxt - pos
                if on_gap:
                    on_gap(pos, nxt)
                pos, prev_vcl = nxt, False
            else:
                n, kind, first, t, fn, ref = r
                if kind == "vcl":
                    key = (16 <= t <= 21) if self.hevc else t == 5
                    self._record_size(n, key)
                    if first:
                        self.frames += 1
                        self.keyframes += key
                        self.cur_fn = fn
                        if ref and fn is not None:
                            self.prev_ref_fn = fn
                        if not self.hevc:
                            try:
                                br = BitReader(unescape(bytes(buf[pos + ls + 1:pos + ls + 13])))
                                br.ue()
                                self.cur_slice_type = br.ue() % 5
                            except ValueError:
                                self.cur_slice_type = None
                elif kind == "ps":
                    if t in (7, 33):
                        self.ps_seen += 1
                    self._learn(bytes(buf[pos + ls:pos + ls + n]))
                self.video_bytes += n
                if on_nal:
                    on_nal(pos + ls, n)
                pos += ls + n
                prev_vcl = kind == "vcl"
            if progress and pos >= next_report:
                next_report = pos + step
                progress(pos)


# ---------------------------------------------------------------------------
# Motion-JPEG frame scanner
# ---------------------------------------------------------------------------

MAX_JPEG = 16 << 20


def jpeg_end(buf, p: int, end: int) -> Optional[int]:
    """Given a JPEG start (FF D8) at p, return the offset just past its end (FF D9)."""
    q = p + 2
    limit = min(end, p + MAX_JPEG)
    while q + 4 <= limit:
        if buf[q] != 0xFF:
            return None
        m = buf[q + 1]
        if m == 0xFF:
            q += 1
            continue
        if m == 0xD9:
            return q + 2
        if m in (0x00, 0xD8) or 0xD0 <= m <= 0xD7 or m == 0x01:
            if m in (0x00, 0xD8):
                return None
            q += 2
            continue
        seg = u16be(buf, q + 2)
        if seg < 2:
            return None
        if m == 0xDA:   # start of scan: compressed data runs up to the EOI marker
            e = buf.find(b"\xff\xd9", q + 2 + seg, limit)
            return e + 2 if e >= 0 else None
        q += 2 + seg
    return None


def scan_jpeg(buf, start: int, end: int, on_frame=None, on_gap=None, progress=None) -> int:
    pos, frames = start, 0
    step = max((end - start) // 20, 1 << 20)
    next_report = start + step
    while pos < end:
        p = buf.find(b"\xff\xd8\xff", pos, end)
        if p < 0:
            if on_gap:
                on_gap(pos, end)
            break
        q = jpeg_end(buf, p, end)
        if q is None:
            if on_gap:
                on_gap(pos, p + 3)
            pos = p + 3
            continue
        if p > pos and on_gap:
            on_gap(pos, p)
        if on_frame:
            on_frame(p, q)
        frames += 1
        pos = q
        if progress and pos >= next_report:
            next_report = pos + step
            progress(pos)
    return frames


# ---------------------------------------------------------------------------
# AVI structure
# ---------------------------------------------------------------------------

AVI_STREAM_RE = re.compile(rb"[0-9]{2}(?:dc|db|wb|pc|tx)")
AVI_MAX_CHUNK = 32 << 20


def riff_chunks(buf, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        cid = bytes(buf[pos:pos + 4])
        size = u32le(buf, pos + 4)
        if not is_fourcc(cid) or pos + 8 + size > end:
            return
        yield pos, cid, size
        pos += 8 + size + (size & 1)


def parse_avi_header(buf, size: int) -> dict:
    out = {"us_per_frame": 0, "streams": []}
    lim = min(size, 8 << 20)
    i = buf.find(b"hdrl", 0, lim)
    if i < 8 or buf[i - 8:i - 4] != b"LIST":
        return out
    lsize = u32le(buf, i - 4)
    s = i + 4
    e = i + lsize if 4 <= lsize <= lim else min(s + 65536, size)
    e = min(e, size)
    for pos, cid, sz in riff_chunks(buf, s, e):
        d0 = pos + 8
        if cid == b"avih" and sz >= 4:
            out["us_per_frame"] = u32le(buf, d0)
        elif cid == b"LIST" and buf[d0:d0 + 4] == b"strl":
            st = {}
            for p2, c2, s2 in riff_chunks(buf, d0 + 4, d0 + sz):
                if c2 == b"strh" and s2 >= 28:
                    st["type"] = bytes(buf[p2 + 8:p2 + 12])
                    st["handler"] = bytes(buf[p2 + 12:p2 + 16])
                    st["scale"] = u32le(buf, p2 + 8 + 20)
                    st["rate"] = u32le(buf, p2 + 8 + 24)
                elif c2 == b"strf":
                    st["strf"] = bytes(buf[p2 + 8:p2 + 8 + s2])
            out["streams"].append(st)
    return out


def avi_index_problems(buf, size: int) -> List[str]:
    """Check an AVI's structure and index the way players such as VLC do.
    Returns a list of problems; an empty list means the file is intact."""
    if bytes(buf[0:4]) != b"RIFF" or bytes(buf[8:12]) != b"AVI ":
        return ["the AVI header is missing or damaged"]
    problems = []
    riff_size = u32le(buf, 4)
    if riff_size == 0:
        problems.append("the file header was never finished (recording was interrupted)")
    elif 8 + riff_size > size:
        problems.append("the file is shorter than its header says (it was cut off)")
    end = min(8 + riff_size, size) if riff_size else size

    # files over 1 GB continue in further 'RIFF AVIX' sections, which must all be complete
    pos = 8 + riff_size + (riff_size & 1) if riff_size else size
    while pos + 12 <= size:
        sz = u32le(buf, pos + 4)
        if bytes(buf[pos:pos + 4]) != b"RIFF" or bytes(buf[pos + 8:pos + 12]) != b"AVIX":
            if any(buf[pos:min(size, pos + 4096)]):
                problems.append(f"there is damaged data after offset {pos}")
            break
        if sz == 0 or pos + 8 + sz > size:
            problems.append("the last part of the file is cut off or unfinished")
            break
        pos += 8 + sz + (sz & 1)

    hdrl = opendml = False
    movi = idx1 = None
    pos = 12
    while pos + 8 <= end:
        cid, sz = bytes(buf[pos:pos + 4]), u32le(buf, pos + 4)
        if not is_fourcc(cid):
            problems.append(f"the file structure is damaged at offset {pos}")
            break
        if cid == b"LIST" and pos + 12 <= size:
            ltype = bytes(buf[pos + 8:pos + 12])
            if ltype == b"hdrl":
                hdrl = True
                opendml = buf.find(b"indx", pos, min(pos + 8 + sz, size)) >= 0
            elif ltype == b"movi":
                movi = (pos + 8, pos + 8 + sz)          # movi[0] = position of the 'movi' tag
        elif cid == b"idx1":
            idx1 = (pos + 8, pos + 8 + sz)
        if sz == 0 and cid == b"LIST":
            break
        pos += 8 + sz + (sz & 1)
    if not hdrl:
        problems.append("the AVI header (hdrl) is missing")
    if movi is None:
        problems.append("the video data list (movi) is missing")
        return problems
    if movi[1] - movi[0] <= 4:
        problems.append("the video data list was never finished (recording was interrupted)")
        movi = (movi[0], size)
    elif movi[1] > size:
        problems.append("the video data is cut off")

    # every chunk actually in the video data list
    chunks = []
    p, mend = movi[0] + 4, min(movi[1], size)
    while p + 8 <= mend:
        cid, sz = bytes(buf[p:p + 4]), u32le(buf, p + 4)
        if cid == b"LIST" and p + 12 <= mend:
            p += 12
            continue
        if not is_fourcc(cid) or p + 8 + sz > mend:
            problems.append(f"the video data is damaged at offset {p}")
            break
        if AVI_STREAM_RE.fullmatch(cid):
            chunks.append(p)
        p += 8 + sz + (sz & 1)

    if idx1 is None:
        if not opendml:
            problems.append("the index (idx1) is missing")
        return problems
    if idx1[1] > size:
        problems.append("the index (idx1) is cut off")
    data = bytes(buf[idx1[0]:min(idx1[1], size)])
    entries = [e for e in struct.iter_unpack("<4sIII", data[:len(data) // 16 * 16])
               if AVI_STREAM_RE.fullmatch(e[0])]
    if not entries:
        problems.append("the index (idx1) is empty")
        return problems
    if len(entries) < len(chunks) * 0.98 - 2:
        problems.append(f"the index lists only {len(entries)} of the {len(chunks)} chunks in the file")

    def points_ok(base, e):
        q = base + e[2]
        return 0 <= q and q + 8 <= size and bytes(buf[q:q + 4]) == e[0] and u32le(buf, q + 4) == e[3]
    # offsets are normally relative to the 'movi' tag, but some writers use file offsets
    base = next((b for b in (movi[0], 0) if points_ok(b, entries[0])), None)
    step = max(1, len(entries) // 200)
    sample = entries[::step] + [entries[-1]]
    bad = len(sample) if base is None else sum(not points_ok(base, e) for e in sample)
    if bad > max(1, len(sample) // 100):
        problems.append("the index points to the wrong places in the file")
    return problems


def avi_stream_codec(st: dict) -> Optional[str]:
    names = []
    strf = st.get("strf", b"")
    if len(strf) >= 20:
        names.append(strf[16:20])
    names.append(st.get("handler", b""))
    for n in names:
        c = AVI_FOURCCS.get(n.upper().rstrip(b"\0 "))
        if c:
            return c
    return None


def avi_fps(st: dict, us_per_frame: int) -> Optional[float]:
    if st.get("scale") and st.get("rate"):
        fps = st["rate"] / st["scale"]
        if 1 <= fps <= 300:
            return fps
    if us_per_frame:
        fps = 1e6 / us_per_frame
        if 1 <= fps <= 300:
            return fps
    return None


def wave_format_ok(strf: bytes) -> bool:
    if len(strf) < 16:
        return False
    tag, ch, rate, avg, align = struct.unpack_from("<HHIIH", strf, 0)
    return tag != 0 and 1 <= ch <= 8 and 4000 <= rate <= 192000 and avg > 0 and align > 0


def _avi_id_ok(buf, pos: int, end: int) -> bool:
    if pos + 8 > end:
        return pos >= end - 8
    cid = bytes(buf[pos:pos + 4])
    return bool(AVI_STREAM_RE.fullmatch(cid)) or cid in (b"LIST", b"JUNK", b"idx1", b"RIFF") \
        or (cid[:2] == b"ix" and is_fourcc(cid))


def avi_resync(buf, pos: int, end: int) -> int:
    while True:
        m = AVI_STREAM_RE.search(buf, pos, end)
        if not m:
            return end
        p = m.start()
        if p + 8 <= end:
            sz = u32le(buf, p + 4)
            if sz <= AVI_MAX_CHUNK and p + 8 + sz <= end:
                q = p + 8 + sz
                if _avi_id_ok(buf, q + (sz & 1), end) or _avi_id_ok(buf, q, end):
                    return p
        pos = p + 1


def walk_avi(buf, start: int, end: int, on_chunk, progress=None) -> int:
    """Calls on_chunk(stream_no, kind, offset, size) for every stream chunk. Returns chunk count."""
    pos, count = start, 0
    step = max((end - start) // 20, 1 << 20)
    next_report = start + step
    while pos + 8 <= end:
        cid = bytes(buf[pos:pos + 4])
        sz = u32le(buf, pos + 4)
        nxt = pos + 8 + sz + (sz & 1)
        if AVI_STREAM_RE.fullmatch(cid) and sz <= AVI_MAX_CHUNK and pos + 8 + sz <= end:
            on_chunk(int(cid[:2]), cid[2:], pos + 8, sz)
            count += 1
            if sz & 1 and not _avi_id_ok(buf, nxt, end) and _avi_id_ok(buf, nxt - 1, end):
                nxt -= 1                       # writer forgot the padding byte
            pos = nxt
        elif cid in (b"LIST", b"RIFF") and pos + 12 <= end:
            ltype = bytes(buf[pos + 8:pos + 12])
            if ltype in (b"movi", b"rec ", b"AVIX", b"AVI "):
                pos += 12
            elif is_fourcc(ltype) and pos + 8 + sz <= end:
                pos = nxt
            else:
                pos = avi_resync(buf, pos + 1, end)
        elif (cid in (b"JUNK", b"idx1") or (cid[:2] == b"ix" and is_fourcc(cid))) and pos + 8 + sz <= end:
            pos = nxt
        else:
            pos = avi_resync(buf, pos + 1, end)
        if progress and pos >= next_report:
            next_report = pos + step
            progress(pos)
    return count


def sniff_frame_codec(frame: bytes) -> Optional[str]:
    if frame[:2] == b"\xff\xd8":
        return "mjpeg"
    for sc in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        if frame.startswith(sc) and len(frame) > len(sc) + 1:
            h0, h1 = frame[len(sc)], frame[len(sc) + 1]
            if not h0 & 0x81 and h1 == 1 and (h0 >> 1) in (32, 33, 34, 35, 39, 1, 19, 20, 21):
                return "hevc"
            if not h0 & 0x80 and (h0 & 0x1F) in (1, 5, 6, 7, 8, 9):
                return "h264"
            if h0 in (0xB0, 0xB5, 0xB6) or h0 <= 0x2F:
                return "mpeg4"
    return None


# ---------------------------------------------------------------------------
# Container detection / diagnosis
# ---------------------------------------------------------------------------

def detect_container(buf, size: int) -> str:
    head = bytes(buf[:min(size, 16)])
    if head[:4] == b"RIFF" and head[8:12] in (b"AVI ", b"AVIX"):
        return "avi"
    if head[4:8] in MP4_TOP:
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "mkv"
    if size > 376 and head[0] == 0x47 and buf[188] == 0x47:
        return "ts"
    if size > 384 and buf[4] == 0x47 and buf[196] == 0x47:
        return "ts"     # .MTS (192-byte packets)
    lim = min(size, 1 << 20)
    if buf.find(b"ftyp", 0, 4096) >= 0 or buf.find(b"mdat", 0, lim) >= 0 or buf.find(b"moov", 0, lim) >= 0:
        return "mp4"
    if buf.find(b"hdrl", 0, lim) >= 0 or buf.find(b"movi", 0, lim) >= 0:
        return "avi"
    return "unknown"


CONTAINER_NAMES = {"avi": "AVI", "mp4": "MP4/MOV", "mkv": "Matroska (MKV)",
                   "ts": "MPEG transport stream (TS/MTS)", "unknown": "unrecognised"}


def filled_fraction(buf, size: int, value: int, samples: int = 64, chunk: int = 65536) -> float:
    if size == 0:
        return 1.0
    total = hits = 0
    step = max(size // samples, 1)
    for i in range(0, size, step):
        b = buf[i:i + chunk]
        total += len(b)
        hits += b.count(value)
    return hits / total if total else 1.0


# ---------------------------------------------------------------------------
# Salvage (rebuild from raw data)
# ---------------------------------------------------------------------------

@dataclass
class Options:
    out_dir: Optional[Path] = None
    reference: Optional[Path] = None
    deep: bool = False
    copy: bool = False
    fps: Optional[float] = None
    audio_rate: Optional[int] = None
    skip_healthy: bool = False


@dataclass
class Salvaged:
    video_path: Path
    demuxer: str                 # ffmpeg raw demuxer name
    codec: str
    fps: float
    frames: int
    audio_path: Optional[Path] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class Reference:
    video: Optional[VideoInfo] = None
    audio: Optional[AudioInfo] = None
    avi: Optional[dict] = None


def load_reference(path: Path, log: Log) -> Reference:
    ref = Reference()
    try:
        size = path.stat().st_size
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            ctype = detect_container(buf, size)
            if ctype == "mp4":
                _, moov, _ = mp4_layout(buf, size)
                if moov:
                    ref.video, ref.audio = parse_moov(buf, *moov)
            elif ctype == "avi":
                ref.avi = parse_avi_header(buf, size)
    except (OSError, ValueError) as e:
        log(f"Could not read the reference video: {e}")
        return ref
    if ref.video:
        log(f"Reference video: {ref.video.codec or 'unknown codec'} {ref.video.width}x{ref.video.height}"
            + (f" @ {ref.video.fps:.3g} fps" if ref.video.fps else "")
            + (f"; audio {ref.audio.describe}" if ref.audio else ""))
    elif ref.avi and ref.avi["streams"]:
        log("Reference video: AVI header loaded")
    else:
        log("Warning: the reference video has no usable header (is it a working video?)")
    return ref


class GapAudio:
    """Turns the gaps between video frames into the audio track.  Camcorders write audio in
    chunks of one fixed size; a gap clearly bigger than that also holds video that could not
    be used, so only one chunk's worth is kept - from whichever end of the gap it is at."""

    def __init__(self, wav: WavWriter, buf, frame_at: Callable[[int], bool]):
        self.wav, self.buf, self.frame_at = wav, buf, frame_at
        self.sizes: Counter = Counter()
        self.count = 0

    def chunk_size(self) -> int:
        if self.count < 8:
            return 0
        size, n = self.sizes.most_common(1)[0]
        return size if n >= 0.3 * self.count else 0

    def __call__(self, p: int, q: int) -> None:
        size = q - p
        if size <= 1 << 20:
            self.sizes[size] += 1
            self.count += 1
        g = self.chunk_size()
        if g and size > g + g // 2 and size % g:
            if self.frame_at(p + g):
                q = p + g           # audio chunk first, then unusable video
            else:
                p = q - g           # unusable video first, then the audio chunk
        self.wav.write(self.buf[p:q])


class Salvager:
    def __init__(self, path: Path, buf, size: int, opts: Options, ref: Reference, tmp: Path, log: Log):
        self.path, self.buf, self.size = path, buf, size
        self.opts, self.ref, self.tmp, self.log = opts, ref, tmp, log

    def _progress(self, start: int, end: int, label: str):
        last = [-1]

        def report(pos):
            pct = int(100 * (pos - start) / max(1, end - start))
            if pct // 10 != last[0]:
                last[0] = pct // 10
                self.log(f"  {label}... {pct}%")
        return report

    def _fps(self, *candidates, frames=0, audio_seconds=0.0) -> Tuple[float, str]:
        if self.opts.fps:
            return self.opts.fps, "set by you"
        for c in candidates:
            if c:
                return c, "from the file header"
        if frames and audio_seconds > 1:
            return snap(frames / audio_seconds, STANDARD_FPS, 0.04), "estimated from the audio length"
        return DEFAULT_FPS, "guessed - set the frame rate yourself if the speed looks wrong"

    # -- MP4 / MOV ---------------------------------------------------------
    def salvage_mp4(self) -> Optional[Salvaged]:
        regions, moov, notes = mp4_layout(self.buf, self.size)
        for n in notes:
            self.log(f"  Problem found: {n}")
        own_v = own_a = None
        if moov:
            own_v, own_a = parse_moov(self.buf, *moov)
        v = own_v or VideoInfo()
        rv = self.ref.video
        if rv:
            if not v.codec:
                v.codec = rv.codec
            if not v.extradata and rv.codec == v.codec:
                v.extradata = rv.extradata
            v.fps = v.fps or rv.fps
        a = own_a or self.ref.audio
        return self.salvage_payload(regions, v, a)

    def salvage_generic(self) -> Optional[Salvaged]:
        regions = [(0, self.size)]
        v = VideoInfo()
        if self.ref.video:
            v = VideoInfo(codec=self.ref.video.codec, extradata=self.ref.video.extradata, fps=self.ref.video.fps)
        return self.salvage_payload(regions, v, self.ref.audio)

    def sniff(self, regions) -> Optional[str]:
        s, e = max(regions, key=lambda r: r[1] - r[0])
        e = min(e, s + (16 << 20))
        scores = {}
        for codec in ("h264", "hevc"):
            sc = NalScanner(codec)
            sc.scan(self.buf, s, e)
            scores[codec] = sc.frames
        scores["mjpeg"] = scan_jpeg(self.buf, s, e)
        sample = bytes(self.buf[s:e])
        scores["annexb-h264"] = len(re.findall(rb"\x00\x00\x01[\x65\x45\x25\x41\x21\x01]", sample)) // 2
        scores["annexb-hevc"] = len(re.findall(rb"\x00\x00\x01[\x26\x28\x2a\x02]\x01", sample)) // 2
        best = max(scores, key=scores.get)
        self.log("  Looking for video frames: " + ", ".join(f"{k}={v}" for k, v in scores.items()))
        return best if scores[best] >= 3 else None

    def salvage_payload(self, regions, v: VideoInfo, a: Optional[AudioInfo]) -> Optional[Salvaged]:
        codec = v.codec
        if codec not in ("h264", "hevc", "mjpeg"):
            if codec:
                self.log(f"  The video codec ({codec}) cannot be rebuilt from raw data; trying anyway.")
            codec = self.sniff(regions)
            if not codec:
                self.log("  No recognisable video frames were found in this file.")
                return None
            if codec != v.codec:
                v = VideoInfo(codec=codec, fps=v.fps)
        if codec.startswith("annexb-"):
            raw = codec.split("-")[1]
            fps, why = self._fps(v.fps)
            self.log(f"  Video stream with start codes ({raw}); audio cannot be separated in this layout.")
            return Salvaged(self.path, raw, raw, fps, 0, notes=[f"frame rate {fps:.3g} ({why})"])

        audio_ok = bool(a and a.pcm and a.sample_rate and a.channels)
        if a and not audio_ok:
            self.log(f"  Audio is {a.describe}: it can't be rebuilt without the index, "
                     "so the fixed video will be silent.")
        buf = self.buf
        total = sum(e - s for s, e in regions)
        vpath = self.tmp / ("video." + RAW_DEMUXER[codec])
        notes: List[str] = []

        def new_wav() -> Optional[WavWriter]:
            if not audio_ok:
                return None
            fmt, block, transform = pcm_wav_fmt(a.pcm, self.opts.audio_rate or a.sample_rate, a.channels)
            w = WavWriter(self.tmp / "audio.wav", len(fmt))
            w.align, w.transform = block, transform
            return w

        def scan_all(wav, on_gap, scan_one) -> None:
            on_gap = GapAudio(wav, buf, on_gap) if wav else None
            try:
                for s, e in regions:
                    scan_one(s, e, on_gap, self._progress(s, e, f"scanning {fmt_size(e - s)}"))
            except BaseException:
                if wav:
                    wav.discard()
                raise

        if codec in ("h264", "hevc"):
            ps = v.parameter_sets()
            for check in (True, False):
                wav = new_wav()
                sc = NalScanner(codec, v.nal_length_size, ps, check_frame_num=check)
                with open(vpath, "wb") as vf:
                    for nal in ps:
                        vf.write(START_CODE + nal)

                    def on_nal(p, n):
                        vf.write(START_CODE)
                        vf.write(buf[p:p + n])
                    scan_all(wav, lambda q: sc._header_ok(buf, q, self.size),
                             lambda s, e, gap, prog: sc.scan(buf, s, e, on_nal, gap, prog))
                if check and sc.check_fn and sc.fn_rejects > max(20, sc.frames // 2):
                    # this camera numbers its frames unusually; scan again without that check
                    self.log("  Rescanning with relaxed frame checks...")
                    if wav:
                        wav.discard()
                    continue
                break
            frames = sc.frames
            self.log(f"  Found {frames} {codec.upper()} frames ({sc.keyframes} keyframes), "
                     f"{fmt_size(sc.video_bytes)} video, {fmt_size(sc.gap_bytes)} other data")
            if not ps and not sc.ps_seen:
                if wav:
                    wav.discard()
                self.log("  The decoder settings (SPS/PPS) are missing from this file.\n"
                         "  -> Choose a REFERENCE video: any video that plays fine and was recorded\n"
                         "     with the same camera at the same resolution, then try again.")
                return None
            if frames and not sc.keyframes:
                notes.append("no keyframes found; the picture may be garbled")
        else:
            wav = new_wav()
            count = [0]
            with open(vpath, "wb") as vf:
                def on_frame(p, q):
                    vf.write(buf[p:q])

                def scan_one(s, e, gap, prog):
                    count[0] += scan_jpeg(buf, s, e, on_frame, gap, prog)
                scan_all(wav, lambda q: buf[q:q + 3] == b"\xff\xd8\xff", scan_one)
            frames = count[0]
            self.log(f"  Found {frames} Motion-JPEG frames in {fmt_size(total)}")
        if frames == 0:
            if wav:
                wav.discard()
            self.log("  No video frames were found.")
            return None
        audio_path, audio_seconds = None, 0.0
        if wav:
            rate = self.opts.audio_rate or a.sample_rate
            fmt, block, _ = pcm_wav_fmt(a.pcm, rate, a.channels)
            wav.close(fmt)
            if wav.bytes:
                audio_path = wav.path
                audio_seconds = wav.bytes / (rate * block)
                self.log(f"  Recovered {fmt_time(audio_seconds)} of audio ({a.describe})")
        fps, why = self._fps(v.fps, frames=frames, audio_seconds=audio_seconds)
        notes.append(f"frame rate {fps:.3g} fps ({why})")
        return Salvaged(vpath, RAW_DEMUXER[codec], codec, fps, frames, audio_path, notes)

    # -- AVI ---------------------------------------------------------------
    def salvage_avi(self) -> Optional[Salvaged]:
        buf, size = self.buf, self.size
        hdr = parse_avi_header(buf, size)
        streams = hdr["streams"]
        if not any(st.get("type") for st in streams):
            self.log("  Problem found: the AVI header is missing or damaged")
            if self.ref.avi and self.ref.avi["streams"]:
                self.log("  Using the stream settings from the reference video")
                hdr = self.ref.avi
                streams = hdr["streams"]

        videos = {}   # stream no -> dict(file, frames, codec, last)
        audios = {}   # stream no -> WavWriter
        tmp = self.tmp

        def on_chunk(sid, kind, off, sz):
            if kind in (b"dc", b"db"):
                ent = videos.get(sid)
                if ent is None:
                    ent = videos[sid] = {"file": open(tmp / f"video{sid}.bin", "wb"), "frames": 0,
                                         "codec": None, "last": None, "dropped": 0}
                    st = streams[sid] if sid < len(streams) else {}
                    ent["codec"] = avi_stream_codec(st)
                if sz == 0:
                    # empty chunk = "repeat previous frame"; keep timing for MJPEG
                    if ent["last"] and ent["codec"] == "mjpeg":
                        ent["file"].write(buf[ent["last"][0]:ent["last"][1]])
                        ent["frames"] += 1
                    else:
                        ent["dropped"] += 1
                    return
                if ent["codec"] is None:
                    ent["codec"] = sniff_frame_codec(bytes(buf[off:off + 16]))
                ent["file"].write(buf[off:off + sz])
                ent["last"] = (off, off + sz)
                ent["frames"] += 1
            elif kind == b"wb" and sz:
                w = audios.get(sid)
                if w is None:
                    st = streams[sid] if sid < len(streams) else {}
                    strf = st.get("strf", b"")
                    w = audios[sid] = WavWriter(tmp / f"audio{sid}.wav", len(strf) if wave_format_ok(strf) else 16)
                    w.strf = strf if wave_format_ok(strf) else None
                w.write(buf[off:off + sz])

        try:
            walk_avi(buf, 0, size, on_chunk, self._progress(0, size, f"scanning {fmt_size(size)}"))
        finally:
            for ent in videos.values():
                ent["file"].close()
        if not videos:
            for w in audios.values():
                w.discard()
            self.log("  No AVI video chunks were found.")
            return None

        vsid = max(videos, key=lambda k: videos[k]["frames"])
        ent = videos[vsid]
        codec = ent["codec"]
        self.log(f"  Found {ent['frames']} video frames ({codec or 'unknown codec'})"
                 + (f", {ent['dropped']} empty" if ent["dropped"] else ""))
        if codec not in RAW_DEMUXER:
            for w in audios.values():
                w.discard()
            self.log("  The video format inside this AVI is not supported for rebuilding.")
            return None
        vst = streams[vsid] if vsid < len(streams) else {}
        header_fps = avi_fps(vst, hdr.get("us_per_frame", 0))

        audio_path, audio_seconds, audio_known, notes = None, 0.0, False, []
        if audios:
            asid = max(audios, key=lambda k: audios[k].bytes)
            for k, w in audios.items():
                if k != asid:
                    w.discard()
            w = audios[asid]
            if w.strf:
                fmt, avg = w.strf, u32le(w.strf, 8)
                if self.opts.audio_rate:
                    avg = avg * self.opts.audio_rate // u32le(fmt, 4)
                    fmt = fmt[:4] + struct.pack("<II", self.opts.audio_rate, avg) + fmt[12:]
                audio_seconds = w.bytes / avg
                audio_known = not self.opts.audio_rate
                desc = f"format 0x{u16le(fmt, 0):04x}, {u32le(fmt, 4)} Hz, {u16le(fmt, 2)} ch"
            else:
                fps_guess = self.opts.fps or header_fps or DEFAULT_FPS
                rate = self.opts.audio_rate or int(snap(w.bytes / 2 / max(ent["frames"] / fps_guess, 0.1),
                                                        STANDARD_RATES, 0.05))
                fmt, _, _ = pcm_wav_fmt("s16le", rate, 1)
                audio_seconds = w.bytes / (2 * rate)
                desc = f"assumed 16-bit PCM mono {rate} Hz - set the audio rate yourself if it sounds wrong"
                notes.append("audio format guessed")
            w.close(fmt)
            audio_path = w.path
            self.log(f"  Recovered {fmt_time(audio_seconds)} of audio ({desc})")
        fps, why = self._fps(header_fps, frames=ent["frames"], audio_seconds=audio_seconds if audio_known else 0)
        notes.append(f"frame rate {fps:.3g} fps ({why})")
        return Salvaged(tmp / f"video{vsid}.bin", RAW_DEMUXER[codec], codec, fps, ent["frames"],
                        audio_path, notes)


# ---------------------------------------------------------------------------
# Repair pipeline
# ---------------------------------------------------------------------------

@dataclass
class Result:
    src: Path
    ok: bool
    out: Optional[Path] = None
    method: str = ""
    duration: float = 0.0
    has_audio: bool = False
    message: str = ""
    skipped: bool = False


def encoder_args(copy: bool, codec: Optional[str]) -> Tuple[List[str], str]:
    """-> (ffmpeg output arguments, file extension)"""
    common = ["-max_muxing_queue_size", "4096", "-dn", "-sn"]
    if not copy:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart"] + common, ".mp4"
    if codec == "mjpeg":
        return ["-c:v", "copy", "-c:a", "pcm_s16le"] + common, ".avi"
    if codec in ("h264", "hevc", "mpeg4"):
        tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
        return ["-c:v", "copy"] + tag + ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"] + common, ".mp4"
    return ["-c:v", "copy", "-c:a", "copy"] + common, ".mkv"


def trusted_and_healthy(buf, size: int, ctype: str, data: Optional[dict], err: str,
                        vcodec: Optional[str], duration: float) -> bool:
    """True for a file that is structurally intact and uses a codec Windows plays."""
    if not data or err.strip() or duration <= 0 or not container_matches(ctype, data):
        return False
    if vcodec not in ("h264", "mjpeg", "mpeg4"):
        return False
    if ctype == "mp4":
        regions, moov, notes = mp4_layout(buf, size)
        return bool(moov) and len(notes) == 0
    if ctype == "avi":
        return not avi_index_problems(buf, size)
    return False


def container_matches(ctype: str, data: Optional[dict]) -> bool:
    name = (data or {}).get("format", {}).get("format_name", "")
    families = {"avi": ("avi",), "mp4": ("mov", "mp4"), "mkv": ("matroska",), "ts": ("mpegts",)}
    return any(f in name.split(",") for f in families.get(ctype, ())) or ctype == "unknown" and bool(name)


def _remove(path: Optional[Path]) -> None:
    try:
        if path:
            path.unlink()
    except OSError:
        pass


def check_output(tools: Tools, out: Path) -> Tuple[bool, float, bool]:
    if not out.is_file() or out.stat().st_size < 1024:
        return False, 0.0, False
    data, _ = probe(tools, out)
    has_v, has_a, dur, _ = probe_summary(data)
    return has_v and dur > 0, dur, has_a


def out_path_for(src: Path, opts: Options, ext: str) -> Path:
    out_dir = opts.out_dir or default_out_dir()
    return unique_path(out_dir / f"{src.stem}_fixed{ext}")


def try_direct(src: Path, size: int, vcodec: Optional[str], opts: Options, tools: Tools,
               log: Log, expected: float, ignore_index: bool = False) -> Optional[Result]:
    log("Step 1: rebuilding the file with ffmpeg (tolerant mode)...")
    out_args, ext = encoder_args(opts.copy, vcodec)
    out = out_path_for(src, opts, ext)
    # with a broken index, read the data chunk by chunk instead of trusting the index
    flags = "+genpts+discardcorrupt+igndts" + ("+ignidx" if ignore_index else "")
    args = ["-err_detect", "ignore_err", "-fflags", flags,
            "-analyzeduration", "100M", "-probesize", "100M", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?"] + out_args + [str(out)]
    ok, err = run_ffmpeg(tools, args, log, expected)
    good, dur, has_a = check_output(tools, out) if ok else (False, 0.0, False)
    if good and dur < 1.0 and size > (10 << 20):
        log(f"  Only {dur:.1f} s came out of a {fmt_size(size)} file - not good enough.")
        good = False
    if good:
        return Result(src, True, out, "tolerant rebuild", dur, has_a)
    log("  That did not work." + (("\n" + last_lines(err)) if err.strip() else ""))
    _remove(out)
    return None


def encode_salvaged(src: Path, sv: Salvaged, opts: Options, tools: Tools, log: Log) -> Optional[Result]:
    out_args, ext = encoder_args(opts.copy, sv.codec)
    out = out_path_for(src, opts, ext)
    args = ["-fflags", "+genpts", "-err_detect", "ignore_err", "-framerate", f"{sv.fps:.6g}",
            "-f", sv.demuxer, "-i", str(sv.video_path)]
    if sv.audio_path:
        args += ["-i", str(sv.audio_path), "-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0"]
    args += out_args + [str(out)]
    expected = sv.frames / sv.fps if sv.frames else 0.0
    log("  Writing the repaired video...")
    ok, err = run_ffmpeg(tools, args, log, expected)
    good, dur, has_a = check_output(tools, out) if ok else (False, 0.0, False)
    if good:
        return Result(src, True, out, "rebuilt from raw data", dur, has_a, "; ".join(sv.notes))
    log("  Writing the video failed." + (("\n" + last_lines(err)) if err.strip() else ""))
    _remove(out)
    return None


def repair_file(src, opts: Options, tools: Tools, log: Log = _console_print,
                ref: Optional[Reference] = None) -> Result:
    src = Path(src)
    log("")
    log(f"=== {src.name} ===")
    try:
        size = src.stat().st_size
    except OSError as e:
        return Result(src, False, message=f"Can't read the file: {e}")
    if size == 0:
        return Result(src, False, message="The file is empty (0 bytes); there is nothing to recover.")
    out_dir = opts.out_dir or default_out_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return Result(src, False, message=f"Can't create the output folder {out_dir}: {e}")
    if ref is None:
        ref = load_reference(opts.reference, log) if opts.reference else Reference()

    try:
        f = open(src, "rb")
    except OSError as e:
        return Result(src, False, message=f"Can't open the file: {e}")
    with f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        ctype = detect_container(buf, size)
        log(f"Size {fmt_size(size)}, format: {CONTAINER_NAMES[ctype]}")
        for value, what in ((0, "zeros"), (0xFF, "0xFF bytes (erased memory)")):
            if filled_fraction(buf, size, value) > 0.995:
                return Result(src, False, message=f"The file contains only {what}. The video data itself "
                              "is gone, so it cannot be repaired. (Card recovery software such as "
                              "PhotoRec may still find it on the SD card.)")

        avi_problems = avi_index_problems(buf, size) if ctype == "avi" else []
        for problem in avi_problems:
            log(f"  Problem found: {problem}")

        data, err = probe(tools, src)
        has_v, _, in_dur, vcodec = probe_summary(data)
        if has_v:
            log(f"ffmpeg can read it: {vcodec} video" + (f", {fmt_time(in_dur)} long" if in_dur else ""))
        else:
            log("ffmpeg cannot open it" + (f": {err.splitlines()[-1]}" if err.strip() else ""))

        if opts.skip_healthy and trusted_and_healthy(buf, size, ctype, data, err, vcodec, in_dur):
            log("  This video looks fine (intact index, plays in Windows) - skipped.\n"
                "  (If it still won't play, untick 'Skip videos that already look fine' and try again.)")
            return Result(src, True, method="skipped", duration=in_dur, skipped=True,
                          message="looks fine already")

        # ffmpeg sometimes "opens" a file only by guessing it is a bare video stream
        # (e.g. an AVI whose header is gone); then the sound is lost, so also try a deep scan.
        trusted = has_v and container_matches(ctype, data)
        direct = None
        if has_v and not opts.deep:
            direct = try_direct(src, size, vcodec, opts, tools, log, in_dur, bool(avi_problems))
            if direct and (trusted or direct.has_audio):
                return direct
            if direct:
                log("  ffmpeg had to guess the file format, so a deep scan will be tried as well.")

        log("Step 2: scanning the raw data for video frames and audio...")
        tmp = Path(tempfile.mkdtemp(prefix=".videofixer_", dir=str(out_dir)))
        salvaged = None
        try:
            sv = Salvager(src, buf, size, opts, ref, tmp, log)
            if ctype == "avi":
                result = sv.salvage_avi()
            elif ctype == "mp4":
                result = sv.salvage_mp4()
            else:
                result = None
            if result is None and ctype != "avi" and avi_resync(buf, 0, min(size, 4 << 20)) < min(size, 4 << 20):
                result = sv.salvage_avi()       # AVI data without a recognisable header
            if result is None and ctype != "mp4":
                result = sv.salvage_generic()
            if result:
                for n in result.notes:
                    log(f"  Note: {n}")
                salvaged = encode_salvaged(src, result, opts, tools, log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if direct and salvaged:
            if salvaged.duration >= 0.9 * direct.duration:
                _remove(direct.out)
                try:
                    salvaged.out = salvaged.out.replace(direct.out)   # take over the NAME_fixed name
                except OSError:
                    pass
                return salvaged
            _remove(salvaged.out)
        if direct or salvaged:
            return direct or salvaged

        if has_v and opts.deep:
            r = try_direct(src, size, vcodec, opts, tools, log, in_dur, bool(avi_problems))
            if r:
                return r
    return Result(src, False, message="Could not recover any playable video from this file.")


def diagnose(src: Path, tools: Tools, log: Log = _console_print) -> None:
    src = Path(src)
    log(f"=== {src} ===")
    size = src.stat().st_size
    log(f"Size: {fmt_size(size)} ({size} bytes)")
    if size == 0:
        log("The file is empty.")
        return
    with open(src, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        ctype = detect_container(buf, size)
        log(f"Format: {CONTAINER_NAMES[ctype]}")
        log(f"Zero bytes: {100 * filled_fraction(buf, size, 0):.1f}%")
        if ctype == "mp4":
            for o, s, t, h in read_atoms(buf, 0, size):
                log(f"  box '{t.decode('latin-1')}' at {o}, size {s}" + ("  (CUT OFF)" if o + s > size else ""))
            regions, moov, notes = mp4_layout(buf, size)
            for n in notes:
                log(f"  problem: {n}")
            if moov:
                v, a = parse_moov(buf, *moov)
                if v:
                    log(f"  video: {v.codec} {v.width}x{v.height} fps={v.fps} "
                        f"param-sets={'yes' if v.parameter_sets() else 'no'}")
                if a:
                    log(f"  audio: {a.describe}")
        elif ctype == "avi":
            hdr = parse_avi_header(buf, size)
            log(f"  header: {len(hdr['streams'])} stream(s), {hdr['us_per_frame']} us/frame")
            for i, st in enumerate(hdr["streams"]):
                log(f"  stream {i}: {st.get('type')} {st.get('handler')} codec={avi_stream_codec(st)} "
                    f"rate={st.get('rate')}/{st.get('scale')}")
            problems = avi_index_problems(buf, size)
            for problem in problems:
                log(f"  problem: {problem}")
            if not problems:
                log("  structure and index: OK")
    data, err = probe(tools, src)
    has_v, has_a, dur, vcodec = probe_summary(data)
    if data:
        log(f"ffprobe: video={vcodec} audio={'yes' if has_a else 'no'} duration={dur:.1f}s")
    if err.strip():
        log("ffprobe messages:\n" + last_lines(err, 6))


def collect_inputs(items) -> List[Path]:
    out, seen = [], set()
    for item in items:
        p = Path(item)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS and "_fixed" not in f.stem \
                        and not any(part.startswith(".videofixer_") for part in f.parts):
                    if f.resolve() not in seen:
                        seen.add(f.resolve())
                        out.append(f)
        elif p.is_file():
            if p.resolve() not in seen:
                seen.add(p.resolve())
                out.append(p)
    return out


def repair_many(files: List[Path], opts: Options, tools: Tools, log: Log) -> List[Result]:
    ref = load_reference(opts.reference, log) if opts.reference else Reference()
    results = []
    for f in files:
        try:
            r = repair_file(f, opts, tools, log, ref)
        except Exception as e:  # keep going with the other files
            r = Result(Path(f), False, message=f"Unexpected error: {e!r}")
        if r.skipped:
            log("SKIPPED: looks fine already")
        elif r.ok:
            log(f"FIXED -> {r.out}  ({fmt_time(r.duration)}, {'with' if r.has_audio else 'no'} sound, {r.method})")
            if r.message:
                log(f"  ({r.message})")
        else:
            log(f"NOT FIXED: {r.message}")
        results.append(r)
    return results


def summary_text(results: List[Result]) -> str:
    fixed = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    tried = len(results) - len(skipped)
    lines = [f"{len(fixed)} of {tried} video(s) repaired."
             + (f" {len(skipped)} looked fine and were skipped." if skipped else "")]
    for r in results:
        if r.skipped:
            lines.append("  SKIP  " + r.src.name + "  (looks fine)")
        else:
            lines.append(("  OK    " if r.ok else "  FAIL  ") + r.src.name
                         + (f"  ->  {r.out.name}" if r.ok else f"  ({r.message})"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def run_gui(initial=()) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError:
        _console_print("The window version needs tkinter. Run with file names instead, e.g.\n"
                       "  python videofixer.py BROKEN.AVI")
        return 1

    root = tk.Tk()
    root.title(f"VideoFixer {__version__} - repair broken camcorder videos")
    root.geometry("820x640")
    root.minsize(640, 520)
    msgs: "queue.Queue[tuple]" = queue.Queue()
    files: List[Path] = []

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, weight=1)
    bold = ("Segoe UI", 10, "bold") if os.name == "nt" else ("TkDefaultFont", 10, "bold")

    ttk.Label(main, text="1. Add the videos that won't play", font=bold).grid(row=0, column=0, sticky="w")
    lf = ttk.Frame(main)
    lf.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
    lf.columnconfigure(0, weight=1)
    lb = tk.Listbox(lf, height=7, selectmode="extended")
    lb.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(lf, orient="vertical", command=lb.yview)
    sb.grid(row=0, column=1, sticky="ns")
    lb.configure(yscrollcommand=sb.set)
    bf = ttk.Frame(lf)
    bf.grid(row=0, column=2, sticky="n", padx=(8, 0))

    def add(paths):
        for p in collect_inputs(paths):
            if p not in files:
                files.append(p)
                lb.insert("end", str(p))

    def add_files():
        add(filedialog.askopenfilenames(title="Choose broken videos", filetypes=[
            ("Videos", " ".join("*" + e for e in sorted(VIDEO_EXTS))), ("All files", "*.*")]))

    def add_folder():
        d = filedialog.askdirectory(title="Choose a folder (e.g. the SD card's DCIM folder)")
        if d:
            add([d])

    def remove():
        for i in reversed(lb.curselection()):
            lb.delete(i)
            del files[i]

    for text, cmd in (("Add files...", add_files), ("Add folder...", add_folder),
                      ("Remove", remove), ("Clear", lambda: (lb.delete(0, "end"), files.clear()))):
        ttk.Button(bf, text=text, command=cmd, width=14).pack(fill="x", pady=2)

    ttk.Label(main, text="2. Optional: a GOOD video from the same camera (helps with MP4/MOV files)",
              font=bold).grid(row=2, column=0, sticky="w")
    ref_var = tk.StringVar()
    rf = ttk.Frame(main)
    rf.grid(row=3, column=0, sticky="ew", pady=(4, 8))
    rf.columnconfigure(0, weight=1)
    ttk.Entry(rf, textvariable=ref_var).grid(row=0, column=0, sticky="ew")
    ttk.Button(rf, text="Browse...", command=lambda: ref_var.set(
        filedialog.askopenfilename(title="Choose a video that plays fine") or ref_var.get())
    ).grid(row=0, column=1, padx=(8, 0))

    ttk.Label(main, text="3. Save the fixed videos in", font=bold).grid(row=4, column=0, sticky="w")
    out_var = tk.StringVar(value=str(default_out_dir()))
    of = ttk.Frame(main)
    of.grid(row=5, column=0, sticky="ew", pady=(4, 8))
    of.columnconfigure(0, weight=1)
    ttk.Entry(of, textvariable=out_var).grid(row=0, column=0, sticky="ew")
    ttk.Button(of, text="Browse...", command=lambda: out_var.set(
        filedialog.askdirectory(title="Where should fixed videos go?") or out_var.get())
    ).grid(row=0, column=1, padx=(8, 0))

    opt = ttk.Frame(main)
    opt.grid(row=6, column=0, sticky="w")
    deep_var, copy_var, skip_var = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar(value=True)
    ttk.Checkbutton(opt, text="Skip videos that already look fine", variable=skip_var).pack(anchor="w")
    ttk.Checkbutton(opt, text="Deep scan (try this if a fixed video is too short)",
                    variable=deep_var).pack(anchor="w")
    ttk.Checkbutton(opt, text="Don't re-encode (faster, keeps original quality, may not play everywhere)",
                    variable=copy_var).pack(anchor="w")
    adv = ttk.Frame(opt)
    adv.pack(anchor="w", pady=(4, 0))
    fps_var, rate_var = tk.StringVar(), tk.StringVar()
    ttk.Label(adv, text="Only if a fixed video plays too fast/slow:  frame rate").pack(side="left")
    ttk.Entry(adv, textvariable=fps_var, width=7).pack(side="left", padx=4)
    ttk.Label(adv, text="audio rate (Hz)").pack(side="left", padx=(8, 0))
    ttk.Entry(adv, textvariable=rate_var, width=8).pack(side="left", padx=4)
    ttk.Label(adv, text="(leave empty = automatic)").pack(side="left")

    act = ttk.Frame(main)
    act.grid(row=7, column=0, sticky="ew", pady=8)
    act.columnconfigure(1, weight=1)
    go = ttk.Button(act, text="Fix videos")
    go.grid(row=0, column=0)
    bar = ttk.Progressbar(act, mode="determinate")
    bar.grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(act, text="Open output folder",
               command=lambda: open_folder(Path(out_var.get()))).grid(row=0, column=2)

    logbox = scrolledtext.ScrolledText(main, height=12, state="disabled", wrap="word")
    logbox.grid(row=8, column=0, sticky="nsew")
    main.rowconfigure(8, weight=1)
    main.rowconfigure(1, weight=0)

    def log_line(text):
        logbox.configure(state="normal")
        logbox.insert("end", text + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    tools = Tools()
    if not tools.ok:
        log_line(MISSING_FFMPEG)

    def worker(paths, opts):
        try:
            results = repair_many(paths, opts, tools, lambda m: msgs.put(("log", m)))
        except Exception as e:
            msgs.put(("log", f"Unexpected error: {e!r}"))
            results = []
        msgs.put(("done", results))

    def start():
        nonlocal tools
        if not files:
            messagebox.showinfo("VideoFixer", "Add one or more videos first.")
            return
        if not tools.ok:
            tools = Tools()
            if not tools.ok:
                messagebox.showerror("VideoFixer", MISSING_FFMPEG)
                return
        try:
            fps = float(fps_var.get()) if fps_var.get().strip() else None
            rate = int(rate_var.get()) if rate_var.get().strip() else None
        except ValueError:
            messagebox.showerror("VideoFixer", "Frame rate and audio rate must be numbers (e.g. 30 and 16000).")
            return
        opts = Options(out_dir=Path(out_var.get()) if out_var.get().strip() else None, fps=fps, audio_rate=rate,
                       reference=Path(ref_var.get()) if ref_var.get().strip() else None,
                       deep=deep_var.get(), copy=copy_var.get(), skip_healthy=skip_var.get())
        if opts.reference and not opts.reference.is_file():
            messagebox.showerror("VideoFixer", "The reference video doesn't exist.")
            return
        go.configure(state="disabled")
        done[0] = 0
        bar.configure(maximum=len(files), value=0)
        threading.Thread(target=worker, args=(list(files), opts), daemon=True).start()

    go.configure(command=start)
    done = [0]

    def poll():
        try:
            while True:
                kind, payload = msgs.get_nowait()
                if kind == "log":
                    log_line(payload)
                    if payload.startswith(("FIXED", "NOT FIXED", "SKIPPED")):
                        done[0] += 1
                        bar.configure(value=done[0])
                elif kind == "done":
                    go.configure(state="normal")
                    if payload:
                        text = summary_text(payload)
                        log_line("\n" + text)
                        if any(r.ok and not r.skipped for r in payload):
                            if messagebox.askyesno("VideoFixer", text + "\n\nOpen the output folder?"):
                                open_folder(Path(out_var.get()))
                        else:
                            messagebox.showwarning("VideoFixer", text)
        except queue.Empty:
            pass
        root.after(150, poll)

    add(initial)
    log_line("Tip: copy the videos from the SD card to your PC first, then add them here.\n"
             "The original files are never changed; fixed copies are saved as NAME_fixed.mp4.")
    root.after(150, poll)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="videofixer",
        description="Repair corrupted camcorder videos. Writes a new NAME_fixed.mp4; never changes the original.",
        epilog="With no files, a window opens. Example: python videofixer.py D:\\DCIM --out-dir C:\\Fixed")
    ap.add_argument("inputs", nargs="*", help="video files or folders to repair")
    ap.add_argument("-o", "--out-dir", help=f"where to save fixed videos (default: {default_out_dir()})")
    ap.add_argument("-r", "--reference", help="a working video from the same camera (needed for some MP4/MOV files)")
    ap.add_argument("--deep", action="store_true", help="skip the quick ffmpeg rebuild and scan the raw data first")
    ap.add_argument("--copy", action="store_true", help="don't re-encode the video (faster, original quality)")
    ap.add_argument("--fps", type=float, help="frame rate to use when it can't be read from the file")
    ap.add_argument("--audio-rate", type=int, help="audio sample rate to use if the sound plays at the wrong speed")
    ap.add_argument("--all", action="store_true",
                    help="also process videos in folders that already look fine (they are skipped otherwise)")
    ap.add_argument("--info", action="store_true", help="only show what is wrong with the files")
    ap.add_argument("--gui", action="store_true", help="open the window")
    ap.add_argument("--version", action="version", version=f"VideoFixer {__version__}")
    args = ap.parse_args(argv)

    windowed = getattr(sys, "frozen", False) and sys.stdout is None
    if args.gui or not args.inputs or windowed:
        return run_gui(args.inputs)

    tools = Tools()
    if not tools.ok:
        _console_print(MISSING_FFMPEG)
        return 2
    files = collect_inputs(args.inputs)
    if not files:
        _console_print("No video files found in: " + ", ".join(args.inputs))
        return 1
    if args.info:
        for f in files:
            diagnose(f, tools)
        return 0
    opts = Options(out_dir=Path(args.out_dir) if args.out_dir else None,
                   reference=Path(args.reference) if args.reference else None,
                   deep=args.deep, copy=args.copy, fps=args.fps, audio_rate=args.audio_rate,
                   skip_healthy=not args.all and any(Path(i).is_dir() for i in args.inputs))
    if opts.reference and not opts.reference.is_file():
        _console_print(f"Reference video not found: {opts.reference}")
        return 1
    results = repair_many(files, opts, tools, _console_print)
    _console_print("\n" + summary_text(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
