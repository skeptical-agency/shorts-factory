"""Tool handlers for Shorts Factory.

Rules: every handler takes (args: dict, **kwargs), never raises, and always
returns a JSON string. Engines are imported lazily so the plugin loads fast.
Zero credentials, zero network except the optional one-time Bebas Neue font
download. Requires ffmpeg/ffprobe on the machine (sf_status checks).
"""

import json
import os
from pathlib import Path

_CTX = None


def bind_ctx(ctx):
    global _CTX
    _CTX = ctx


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # documented public API

        return Path(plugin_data_dir("shorts-factory"))
    except Exception:
        home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        p = Path(home) / "plugin-data" / "shorts-factory"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra})


def _ok(**payload) -> str:
    return json.dumps(payload, default=str)


def _safe(fn, *a, **kw) -> str:
    from . import ffutil

    bins = ffutil.binaries()
    if not bins.get("ready") and fn.__name__ != "status":
        return _err(bins.get("hint", "ffmpeg/ffprobe not found"), binaries=bins)
    try:
        return fn(*a, **kw)
    except Exception as e:  # never raise into the host
        return _err(f"{type(e).__name__}: {str(e)[:400]}")


def _cfg(key: str, default=""):
    try:
        if _CTX is not None and hasattr(_CTX, "get_config"):
            v = _CTX.get_config(key, default)
            return v if v not in (None, "") else default
    except Exception:
        pass
    return default


# ---------------------------------------------------------------- handlers

def status(args: dict, **kwargs) -> str:
    def _run():
        from . import ffutil

        bins = ffutil.binaries()
        deps = {}
        for mod, pipname in (("PIL", "pillow"), ("numpy", "numpy"),
                             ("faster_whisper", "faster-whisper")):
            try:
                __import__(mod)
                deps[pipname] = True
            except Exception:
                deps[pipname] = False
        font = None
        try:
            from . import engine_video

            font = str(engine_video._resolve_font(_cfg("caption_font", ""), _data_dir()))
        except Exception as e:
            font = f"unavailable: {str(e)[:120]}"
        return _ok(ready=bins.get("ready", False), binaries=bins,
                   python_deps=deps, caption_font=font,
                   whisper_note=("faster-whisper missing: sf_captions still works with an .srt or word-timing JSON — pass timings=, or pip install faster-whisper to transcribe in-plugin."
                                 if not deps["faster-whisper"] else "in-plugin transcription available"),
                   hint=None if bins.get("ready") else bins.get("hint"))

    return _safe(_run)


def loudness(args: dict, **kwargs) -> str:
    def _run():
        from . import engine_audio

        mode = (args.get("mode") or "").strip()
        if mode == "measure":
            f = args.get("file")
            if not f or not Path(f).expanduser().exists():
                return _err("measure needs an existing file")
            return _ok(file=f, **engine_audio.measure(str(Path(f).expanduser())))
        if mode == "normalize":
            f, out = args.get("file"), args.get("out")
            if not f or not out:
                return _err("normalize needs file and out")
            if not Path(f).expanduser().exists():
                return _err(f"file not found: {f}")
            return _ok(**engine_audio.normalize(str(Path(f).expanduser()), str(Path(out).expanduser()),
                                                target=args.get("target", "platform"),
                                                two_pass=bool(args.get("two_pass", True))))
        if mode == "duck":
            music, vo, out = args.get("music"), args.get("vo"), args.get("out")
            if not music or not vo or not out:
                return _err("duck needs music, vo and out")
            for p in (music, vo):
                if not Path(p).expanduser().exists():
                    return _err(f"file not found: {p}")
            return _ok(**engine_audio.duck(str(Path(music).expanduser()), str(Path(vo).expanduser()),
                                           str(Path(out).expanduser())))
        if mode == "tempo":
            f, out, factor = args.get("file"), args.get("out"), float(args.get("factor") or 0)
            if not f or not out or not factor:
                return _err("tempo needs file, out and factor")
            if not Path(f).expanduser().exists():
                return _err(f"file not found: {f}")
            return _ok(**engine_audio.tempo(str(Path(f).expanduser()), str(Path(out).expanduser()), factor))
        return _err("mode must be measure|normalize|duck|tempo")

    return _safe(_run)


def concat(args: dict, **kwargs) -> str:
    def _run():
        from . import engine_video

        segments = args.get("segments") or []
        out = args.get("out")
        if not segments or not out:
            return _err("concat needs segments[] and out")
        return _ok(**engine_video.safe_concat(
            segments, str(Path(out).expanduser()),
            width=int(args.get("width") or 1080),
            height=int(args.get("height") or 1920),
            fps=int(args.get("fps") or 30),
        ))

    return _safe(_run)


def captions(args: dict, **kwargs) -> str:
    def _run():
        from . import engine_video, ffutil

        video, out = args.get("video"), args.get("out")
        if not video or not out:
            return _err("captions needs video and out")
        video_p = Path(video).expanduser()
        if not video_p.exists():
            return _err(f"video not found: {video}")
        info = ffutil.video_info(str(video_p))

        # 1) timings
        words = None
        timings = args.get("timings")
        if timings:
            tp = Path(timings).expanduser()
            if not tp.exists():
                return _err(f"timings file not found: {timings}")
            if tp.suffix.lower() == ".srt":
                words = engine_video.parse_srt(str(tp))
            else:
                words = engine_video.parse_words_json(str(tp))
        else:
            try:
                from faster_whisper import WhisperModel  # lazy, optional
            except Exception:
                return _err(
                    "no timings provided and faster-whisper is not installed. "
                    "Pass timings=<.srt or word-json path> (any STT that emits word timings works), "
                    "or pip install faster-whisper. Whisper supplies TIMING only — caption text "
                    "always comes from your script.")
            wav = str(Path(out).with_suffix(".audio.wav"))
            rc, _, err = ffutil.run([ffutil.ffmpeg(), "-y", "-v", "error", "-i", str(video_p),
                                     "-vn", "-ar", "16000", "-ac", "1", wav], timeout=600)
            if rc != 0:
                return _err(f"audio extract failed: {err.strip()[-200:]}")
            model = WhisperModel("small")
            segs, _info = model.transcribe(wav, word_timestamps=True)
            words = []
            for seg in segs:
                for w in (seg.words or []):
                    words.append({"word": (w.word or "").strip(), "start": float(w.start), "end": float(w.end)})
            Path(wav).unlink(missing_ok=True)

        if not words:
            return _err("timing source produced zero words")

        words = engine_video.merge_number_fragments(words)
        script = args.get("script")
        if script:
            words = engine_video.align_to_script(words, script)
        groups = engine_video.group_words(words, fps=info["fps"] or 30.0)
        if not groups:
            return _err("no caption groups produced")

        result = engine_video.burn_captions(
            str(video_p), str(Path(out).expanduser()), groups,
            width=info["width"], height=info["height"],
            accent_words=tuple(args.get("accent_words") or ()),
            accent_color=str(args.get("accent_color") or _cfg("accent_color", "#FFD400")),
            font_path=_cfg("caption_font", "") or None,
            data_dir=_data_dir(),
        )
        if args.get("verify", True):
            result["verification"] = engine_video.verify_captions(
                str(video_p), result["out"], groups, info["width"], info["height"])
        result["group_count"] = len(groups)
        return _ok(**result)

    return _safe(_run)


def footage_audit(args: dict, **kwargs) -> str:
    def _run():
        from . import engine_video

        video, out_dir = args.get("video"), args.get("out_dir")
        if not video or not out_dir:
            return _err("footage_audit needs video and out_dir")
        if not Path(video).expanduser().exists():
            return _err(f"video not found: {video}")
        return _ok(**engine_video.footage_audit(
            str(Path(video).expanduser()), str(Path(out_dir).expanduser()),
            coarse_fps=int(args.get("coarse_fps") or 1),
            windows=args.get("windows"),
            dark_threshold=float(args.get("dark_threshold") or 40.0),
        ))

    return _safe(_run)


def safezone(args: dict, **kwargs) -> str:
    def _run():
        from . import engine_video

        media, out_dir = args.get("media"), args.get("out_dir")
        if not media or not out_dir:
            return _err("safezone needs media and out_dir")
        if not Path(media).expanduser().exists():
            return _err(f"media not found: {media}")
        return _ok(**engine_video.safezone_check(
            str(Path(media).expanduser()), str(Path(out_dir).expanduser()),
            bboxes=args.get("bboxes"), frame_t=float(args.get("frame_t") or 1.0),
        ))

    return _safe(_run)


def motion_lint(args: dict, **kwargs) -> str:
    def _run():
        from . import engine_video

        source = args.get("source")
        if not source:
            return _err("motion_lint needs source (path or text)")
        is_text = bool(args.get("is_text"))
        if not is_text and not Path(source).expanduser().exists():
            return _err(f"source file not found: {source}")
        return _ok(**engine_video.motion_lint(
            source if is_text else str(Path(source).expanduser()), is_path=not is_text))

    return _safe(_run)


HANDLERS = {
    "sf_status": status,
    "sf_loudness": loudness,
    "sf_concat": concat,
    "sf_captions": captions,
    "sf_footage_audit": footage_audit,
    "sf_safezone": safezone,
    "sf_motion_lint": motion_lint,
}
