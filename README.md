# VideoFixer

Repairs video files from cheap camcorders, dashcams and action cameras that **won't play
or show no thumbnail** in Windows. You give it a broken file; it writes a new, fixed copy
(`NAME_fixed.mp4`) that plays everywhere. **The original file is never changed.**

## Why the videos break

A camcorder writes the picture and sound while recording, but it writes the file's
"table of contents" (the *index*) only when you press stop. If the battery dies, the card
fills up, or the camera is switched off or crashes mid-recording, the index is never
written. The video is still on the card, but Windows can't find it, so the file won't play
and gets no thumbnail.

VideoFixer works in two steps:

1. **Quick rebuild**: ffmpeg re-reads the file while ignoring errors. This fixes damaged
   indexes and bad sections.
2. **Deep scan**: if step 1 fails, VideoFixer reads the raw bytes, picks out every video
   frame (H.264, H.265/HEVC or Motion-JPEG) and the sound, and builds a new file from them.
   This recovers videos whose index is missing completely.

It handles `.AVI`, `.MP4` and `.MOV`, the formats these cameras use.

---

## Setup on Windows 11 (one time)

**Option A: ready-made program (nothing to install)**

1. On GitHub, open this repository's **Actions** tab, click the latest **Build Windows app** run,
   and download **VideoFixer-Windows** at the bottom of the page.
2. Unzip it anywhere, for example your Desktop. It contains `VideoFixer.exe` plus
   `ffmpeg.exe` and `ffprobe.exe`. Keep all three in the same folder.
3. Double-click `VideoFixer.exe`. If Windows SmartScreen warns you, click
   *More info → Run anyway*. The program isn't code-signed, which is normal for small
   free tools.

**Option B: run from the source code**

1. Download this repository (**Code → Download ZIP**) and unzip it.
2. Double-click **`Install-Requirements.bat`**. It installs Python and FFmpeg (both free)
   using Windows' built-in `winget`.
3. Double-click **`FixVideo.bat`** to start VideoFixer.

## How to use it

1. **Copy the videos from the SD card to your PC first** (for example into
   `Videos\Camcorder`). Work on the copies and leave the card alone; if the card itself is
   faulty, writing to it can make things worse.
2. Start VideoFixer, click **Add files…** (or **Add folder…**) and choose the videos
   that won't play. You can also drag files onto `FixVideo.bat` or `VideoFixer.exe`.
3. *Optional, but important for MP4/MOV files:* under **2.**, pick one **good** video
   (one that plays) recorded with **the same camera at the same resolution**. Some cameras
   store the decoder settings only in the index; the good video supplies them, along with
   the frame rate and sound format.
4. Click **Fix videos**. The repaired copies go to `Videos\VideoFixer` (you can change
   this). Click **Open output folder** when it's done.

Videos that already look fine are skipped automatically (you can untick that).

### If a video still isn't right

| Problem | What to try |
|---|---|
| A video is skipped as "looks fine" but still won't play | Untick **Skip videos that already look fine** and run again; it then gets rebuilt anyway. |
| "decoder settings (SPS/PPS) are missing" | Choose a **good video from the same camera** (step 3) and run again. |
| Fixed video is much shorter than expected | Tick **Deep scan** and run again. |
| Plays too fast or too slow | Type the camera's frame rate (usually 30, 25 or 60) in **frame rate**. |
| Sound is too high/low pitched, or noise | Type the audio rate (try 8000, 16000, 22050, 32000, 44100 or 48000). |
| "The file contains only zeros" | The video data isn't in the file at all. The card's file table points at empty space. A card-recovery tool such as [PhotoRec](https://www.cgsecurity.org/wiki/PhotoRec) (free) may find the lost video on the card itself. |
| MP4 fixed but silent | That camera records AAC sound, which can't be rebuilt without the index. The picture is still recovered. [untrunc](https://github.com/anthwlock/untrunc) may also recover the sound. |

The text box in the window shows exactly what was found and fixed in each file.

---

## Command line

```
python videofixer.py BROKEN.AVI                     fix one file
python videofixer.py D:\DCIM -o C:\Fixed            fix every video in a folder
python videofixer.py CLIP.MP4 -r GOOD.MP4           use a good video from the same camera
python videofixer.py CLIP.AVI --info                only show what's wrong with the file
python videofixer.py --help                         all options
```

Options: `--deep` (scan raw data first), `--copy` (don't re-encode: faster and lossless,
but the result may not play everywhere), `--fps N`, `--audio-rate N`, `--all` (don't skip
files that look fine).

## How the deep scan works

* **AVI**: walks the `00dc` / `01wb` chunks directly, even when the header and `idx1` index
  are missing. Empty "repeat previous frame" chunks are kept, so sound and picture stay in
  sync. Frame rate and audio format come from the header, a reference video, or are
  estimated (the audio rate is worked out from how much sound there is per frame).
* **MP4/MOV**: finds the media data (`mdat`) and splits it into video frames and the audio
  chunks between them. Audio data can look like a video frame by accident, so each
  candidate frame is checked:
  * its slice header is parsed and must make sense;
  * its size must fit the frames around it and can't exceed the raw picture size;
  * for H.264, its `frame_num` must continue the numbering of the previous frame.

  When several candidates compete, the most convincing one wins. Uncompressed (PCM) audio
  is rebuilt with the fixed chunk size the camera uses, so it stays in sync.
* **Motion-JPEG**: frames are found by their JPEG markers, and the bytes in between are the
  audio.
* The recovered streams are re-encoded to H.264 + AAC MP4 for maximum compatibility.

## Tests

```
python -m unittest discover -s tests -v
```

The tests use ffmpeg to create videos, break them the way cameras do (cut-off recordings
with no index, wiped headers, damaged sections, all-zero files) and check that the repaired
file has the right length, has sound in sync, and decodes without errors.
