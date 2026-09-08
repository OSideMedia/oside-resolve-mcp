# Fixture package — the media files are empty on purpose

Every `.mp4` and `.mp3` here is **0 bytes**. They will not play, and that is not
a mistake or a broken export.

The test suite is hermetic: it never decodes media. It needs these files to
**exist** (so the on-disk checks are true) and to have the **right basenames**
(Resolve matches clips by basename alone). Everything Resolve-shaped runs
against a fake Resolve that supplies synthetic clip durations, so real footage
would add nothing a test can observe — while adding hundreds of megabytes to git
history forever.

**Do not replace them with real media.** `test_dry_run_offline_plan` asserts
`plan["clips"][1]["startFrame"] is None` *because* `ffprobe` cannot size an empty
file — it is proving the offline planner degrades honestly instead of inventing a
number. Real clips turn that test red.

If you need real media for a live walk against Resolve, generate it outside the
repo. Note the audio: several Resolve behaviours only reproduce when a video
clip carries its own audio stream, because that is what fills A1.

```bash
ffmpeg -f lavfi -i testsrc=size=1280x720:rate=24:duration=5 \
       -f lavfi -i sine=frequency=220:duration=5:sample_rate=48000 \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest shot.mp4
```
