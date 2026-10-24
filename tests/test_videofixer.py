"""End-to-end tests: make videos with ffmpeg, break them the way camcorders do, repair them.

Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import videofixer as vf  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
DURATION = 12


def make(path, video_args, audio_args, extra=(), duration=DURATION, size="640x360", rate=30):
    cmd = [FFMPEG, "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}",
           "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=22050:duration={duration}"]
    cmd += list(video_args) + list(audio_args) + list(extra) + [str(path)]
    subprocess.run(cmd, check=True)
    return path


def truncate(path, fraction):
    data = Path(path).read_bytes()
    Path(path).write_bytes(data[:int(len(data) * fraction)])


def zero_range(path, start, length):
    with open(path, "r+b") as f:
        f.seek(start)
        f.write(b"\0" * length)


def probe_streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,duration", "-of", "json", str(path)],
                         capture_output=True, check=True).stdout
    data = json.loads(out)
    streams = {s["codec_type"]: float(s.get("duration", 0)) for s in data.get("streams", [])}
    return float(data["format"]["duration"]), streams


def decode_errors(path):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True)
    return len(r.stderr.decode(errors="replace").strip().splitlines())


@unittest.skipUnless(FFMPEG and shutil.which("ffprobe"), "ffmpeg is required")
class RepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="videofixer_test_"))
        cls.tools = vf.Tools()

    @classmethod
    def tearDownClass(cls):
        if not os.environ.get("KEEP_TEST_FILES"):
            shutil.rmtree(cls.tmp, ignore_errors=True)
        else:
            print("test files kept in", cls.tmp)

    def repair(self, src, min_seconds, audio=True, **opts):
        out_dir = self.tmp / "out"
        log = []
        r = vf.repair_file(src, vf.Options(out_dir=out_dir, **opts), self.tools, log.append)
        text = "\n".join(log)
        self.assertTrue(r.ok, f"repair failed: {r.message}\n{text}")
        dur, streams = probe_streams(r.out)
        self.assertIn("video", streams, text)
        self.assertGreaterEqual(dur, min_seconds, text)
        self.assertLessEqual(dur, DURATION + 1, text)
        if audio:
            self.assertIn("audio", streams, text)
            # sound and picture must stay in sync
            if streams["audio"] and streams["video"]:      # AVI output reports no per-stream duration
                self.assertAlmostEqual(streams["audio"], streams["video"], delta=0.6, msg=text)
        self.assertLess(decode_errors(r.out), 5, text)
        return r, text

    # --- MP4 / MOV with the index (moov) missing --------------------------------------------
    def test_mp4_h264_inband_headers_truncated(self):
        src = make(self.tmp / "inband.mp4", ["-c:v", "libx264", "-x264-params", "repeat-headers=1"],
                   ["-c:a", "aac"])
        truncate(src, 0.85)
        r, log = self.repair(src, DURATION * 0.7, audio=False)
        self.assertIn("rebuilt from raw data", r.method)

    def test_mp4_h264_needs_reference(self):
        src = make(self.tmp / "noref.mp4", ["-c:v", "libx264"], ["-c:a", "aac"])
        good = make(self.tmp / "good.mp4", ["-c:v", "libx264"], ["-c:a", "aac"], duration=3)
        truncate(src, 0.85)
        r = vf.repair_file(src, vf.Options(out_dir=self.tmp / "out"), self.tools, lambda m: None)
        self.assertFalse(r.ok)
        self.repair(src, DURATION * 0.7, audio=False, reference=good)

    def test_mov_h264_pcm_with_reference(self):
        args = (["-c:v", "libx264", "-g", "30"], ["-c:a", "pcm_s16le"])
        src = make(self.tmp / "pcm.mov", *args)
        good = make(self.tmp / "good_pcm.mov", *args, duration=3)
        truncate(src, 0.8)
        r, log = self.repair(src, DURATION * 0.65, reference=good)
        self.assertIn("audio", log)

    def test_mov_h264_big_endian_pcm(self):
        args = (["-c:v", "libx264"], ["-c:a", "pcm_s16be"])
        src = make(self.tmp / "twos.mov", *args)
        good = make(self.tmp / "good_twos.mov", *args, duration=3)
        truncate(src, 0.8)
        self.repair(src, DURATION * 0.65, reference=good)

    def test_mp4_hevc_inband_truncated(self):
        src = make(self.tmp / "hevc.mp4", ["-c:v", "libx265", "-x265-params", "repeat-headers=1:log-level=error",
                                           "-tag:v", "hev1"], ["-c:a", "aac"])
        truncate(src, 0.85)
        self.repair(src, DURATION * 0.6, audio=False)

    def test_mov_h264_multi_slice_pcm(self):
        args = (["-c:v", "libx264", "-x264-params", "slices=4"], ["-c:a", "pcm_s16le"])
        src = make(self.tmp / "slices.mov", *args)
        good = make(self.tmp / "good_slices.mov", *args, duration=3)
        truncate(src, 0.8)
        self.repair(src, DURATION * 0.65, reference=good)

    def test_mov_hevc_pcm_with_reference(self):
        args = (["-c:v", "libx265", "-x265-params", "log-level=error", "-tag:v", "hvc1"], ["-c:a", "pcm_s16le"])
        src = make(self.tmp / "hevc_pcm.mov", *args)
        good = make(self.tmp / "good_hevc_pcm.mov", *args, duration=3)
        truncate(src, 0.8)
        self.repair(src, DURATION * 0.6, reference=good)

    def test_mov_mjpeg_pcm_truncated(self):
        args = (["-c:v", "mjpeg", "-q:v", "5"], ["-c:a", "pcm_s16le"])
        src = make(self.tmp / "mjpeg.mov", *args)
        good = make(self.tmp / "good_mjpeg.mov", *args, duration=3)
        truncate(src, 0.8)
        self.repair(src, DURATION * 0.7, reference=good)

    def test_mp4_header_wiped(self):
        src = make(self.tmp / "wiped.mp4", ["-c:v", "libx264", "-g", "15", "-x264-params", "repeat-headers=1"],
                   ["-an"])
        truncate(src, 0.9)
        zero_range(src, 0, 4096)
        self.repair(src, DURATION * 0.7, audio=False)

    def test_mp4_corrupt_middle_with_index(self):
        src = make(self.tmp / "middle.mp4", ["-c:v", "libx264"], ["-c:a", "aac"],
                   ["-movflags", "+faststart"])
        size = src.stat().st_size
        with open(src, "r+b") as f:
            f.seek(size // 2)
            f.write(os.urandom(20000))
        r, _ = self.repair(src, DURATION * 0.8)
        self.assertEqual(r.method, "tolerant rebuild")

    # --- AVI (typical for cheap camcorders: Motion-JPEG + PCM) ------------------------------
    def test_avi_mjpeg_truncated(self):
        src = make(self.tmp / "trunc.avi", ["-c:v", "mjpeg", "-q:v", "5"], ["-c:a", "pcm_s16le"])
        truncate(src, 0.8)
        self.repair(src, DURATION * 0.7)

    def test_avi_mjpeg_header_wiped(self):
        src = make(self.tmp / "nohdr.avi", ["-c:v", "mjpeg", "-q:v", "5"], ["-c:a", "pcm_s16le"])
        truncate(src, 0.8)
        zero_range(src, 0, 4096)   # header never written / overwritten
        r, log = self.repair(src, DURATION * 0.7)
        self.assertIn("rebuilt from raw data", r.method)
        self.assertIn("22050", log)    # audio rate correctly estimated

    def test_avi_mjpeg_header_wiped_with_reference(self):
        args = (["-c:v", "mjpeg", "-q:v", "5", "-r", "25"], ["-c:a", "pcm_s16le"])
        src = make(self.tmp / "nohdr_ref.avi", *args)
        good = make(self.tmp / "good.avi", *args, duration=2)
        zero_range(src, 0, 4096)
        r, log = self.repair(src, DURATION * 0.9, reference=good)
        self.assertIn("frame rate 25", log)

    def test_avi_h264_header_wiped(self):
        src = make(self.tmp / "h264.avi", ["-c:v", "libx264", "-bf", "0"], ["-c:a", "pcm_s16le"])
        truncate(src, 0.85)
        zero_range(src, 0, 4096)
        self.repair(src, DURATION * 0.7)

    def test_copy_mode(self):
        src = make(self.tmp / "copy.avi", ["-c:v", "mjpeg"], ["-c:a", "pcm_s16le"])
        zero_range(src, 0, 4096)
        r, _ = self.repair(src, DURATION * 0.9, copy=True)
        self.assertEqual(r.out.suffix, ".avi")

    # --- hopeless files ----------------------------------------------------------------------
    def test_all_zero_file(self):
        src = self.tmp / "zeros.mp4"
        src.write_bytes(b"\0" * 300000)
        r = vf.repair_file(src, vf.Options(out_dir=self.tmp / "out"), self.tools, lambda m: None)
        self.assertFalse(r.ok)
        self.assertIn("zeros", r.message)

    def test_random_file(self):
        src = self.tmp / "random.avi"
        src.write_bytes(os.urandom(2_000_000))
        r = vf.repair_file(src, vf.Options(out_dir=self.tmp / "out"), self.tools, lambda m: None)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
