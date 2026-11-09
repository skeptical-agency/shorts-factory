"""Audio engine: streaming loudness, music ducking, tempo.

Targets (production-proven on TN8/Skeptical shorts):
- Integrated -14 LUFS, true peak <= -2.0 dBTP (YouTube/most-platform standard;
  louder uploads get turned DOWN, quieter ones never get turned up).
- Speech elements inside another edit: -17 LUFS / -3 dBTP.
- Music-only beds under VO: ~-36 LUFS effective (loudnorm I=-18 THEN volume -18dB).

Two-pass loudnorm for accuracy; one-pass acceptable for speech when speed
matters. atempo for speed (preserves pitch) — NEVER asetrate (chipmunk).
"""

import json
import re
from pathlib import Path

from . import ffutil


def measure(path: str) -> dict:
    """ebur128 measurement: integrated loudness, LRA, true peak.

    Parses ONLY the final Summary block — per-frame log lines also contain
    'I: -70.0 LUFS' at startup and a naive first-match reads silence.
    """
    rc, out, err = ffutil.run(
        [ffutil.ffmpeg(), "-i", str(path), "-af", "ebur128=peak=1", "-f", "null", "-"],
        timeout=600,
    )
    blob = err or out
    idx = blob.rfind("Summary:")
    if idx == -1:
        raise RuntimeError(f"ebur128 Summary not found: {blob[-300:]}")
    summary = blob[idx:]
    m = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", summary)
    lra = re.search(r"LRA:\s*(-?[\d.]+)\s*LU", summary)
    tp = re.search(r"(?:TP|Peak):\s*(-?[\d.]+)\s*dBFS", summary)
    if not m:
        raise RuntimeError(f"ebur128 summary parse failed: {summary[-300:]}")
    result = {
        "integrated_lufs": float(m.group(1)),
        "lra_lu": float(lra.group(1)) if lra else None,
        "true_peak_dbfs": float(tp.group(1)) if tp else None,
    }
    if result["true_peak_dbfs"] is None:
        # Some builds omit the True peak section: fall back to astats sample peak.
        rc2, out2, err2 = ffutil.run(
            [ffutil.ffmpeg(), "-i", str(path), "-af", "astats", "-f", "null", "-"],
            timeout=600,
        )
        blob2 = (err2 or "") + (out2 or "")
        pk = re.search(r"Peak level dB:\s*(-?[\d.]+)", blob2)
        if pk:
            result["peak_dbfs_sample"] = float(pk.group(1))
    return result


def _loudnorm_json(path: str, i: float, tp: float, lra: float) -> dict:
    """Pass 1: loudnorm with print_format=json; parse the measured_ block."""
    rc, out, err = ffutil.run(
        [ffutil.ffmpeg(), "-i", str(path),
         "-af", f"loudnorm=I={i}:TP={tp}:LRA={lra}:print_format=json",
         "-f", "null", "-"],
        timeout=600,
    )
    blob = (err or "") + (out or "")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", blob, re.S)
    if not m:
        raise RuntimeError("loudnorm pass-1 JSON not found")
    return json.loads(m.group(0))


def normalize(path: str, out_path: str, target: str = "platform",
              two_pass: bool = True) -> dict:
    """Normalize to a loudness target preset. Returns before/after measurements."""
    presets = {
        "platform": {"I": -14, "TP": -2, "LRA": 11},   # YouTube/Spotify standard
        "speech": {"I": -17, "TP": -3, "LRA": 11},     # VO element inside an edit
        "bed": {"I": -18, "TP": -2, "LRA": 11},        # music bed pre-duck
    }
    if target not in presets:
        raise ValueError(f"target must be one of {sorted(presets)}")
    p = presets[target]
    before = measure(path)

    if two_pass:
        meas = _loudnorm_json(path, p["I"], p["TP"], p["LRA"])
        af = (f"loudnorm=I={p['I']}:TP={p['TP']}:LRA={p['LRA']}"
              f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
              f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
              f":offset={meas['target_offset']}:linear=true")
    else:
        af = f"loudnorm=I={p['I']}:TP={p['TP']}:LRA={p['LRA']}"

    rc, out, err = ffutil.run(
        [ffutil.ffmpeg(), "-y", "-i", str(path), "-af", af,
         "-ar", "48000", str(out_path)],
        timeout=900,
    )
    if rc != 0:
        raise RuntimeError(f"loudnorm failed: {err.strip()[-400:]}")
    after = measure(out_path)
    return {"target": target, "params": p, "before": before, "after": after,
            "out": str(out_path), "two_pass": two_pass}


def duck(music: str, vo: str, out_path: str,
         music_volume: float = 0.15, fade_out: float = 2.5,
         threshold: float = 0.06, ratio: int = 6) -> dict:
    """Music bed under voiceover: sidechain duck + amix(normalize=0) + limiter.

    amix's DEFAULT normalization quietly kills carefully set voice levels —
    normalize=0 is mandatory. Music gets a tail fade; never cut abruptly
    under the last word.
    """
    fc = (
        f"[0:a]volume={music_volume},apad[m];"
        f"[1:a]apad[vs];"
        f"[m][vs]sidechaincompress=threshold={threshold}:ratio={ratio}:attack=20:release=300[ducked];"
        f"[ducked][1:a]amix=inputs=2:duration=first:normalize=0,"
        f"afade=t=out:st={{TAIL}}:d={fade_out},alimiter=limit=0.95[a]"
    )
    dur = ffutil.stream_duration(vo, "a") or ffutil.probe(vo)["format"].get("duration")
    dur = float(dur or 0)
    tail = max(0.5, dur - fade_out - 0.2)
    fc = fc.replace("{TAIL}", f"{tail:.3f}")
    rc, out, err = ffutil.run(
        [ffutil.ffmpeg(), "-y", "-i", str(music), "-i", str(vo),
         "-filter_complex", fc, "-map", "[a]",
         "-t", f"{dur:.3f}", "-ar", "48000", "-ac", "2", str(out_path)],
        timeout=900,
    )
    if rc != 0:
        raise RuntimeError(f"duck failed: {err.strip()[-400:]}")
    return {"out": str(out_path), "vo_seconds": round(dur, 2),
            "music_volume": music_volume, "measured": measure(out_path)}


def tempo(path: str, out_path: str, factor: float) -> dict:
    """Speed change with pitch preserved (atempo cascade; NEVER asetrate)."""
    if not 0.5 <= factor <= 4.0:
        raise ValueError("factor must be within 0.5..4.0")
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    chain = ",".join(parts)
    rc, out, err = ffutil.run(
        [ffutil.ffmpeg(), "-y", "-i", str(path), "-filter:a", chain,
         "-ar", "48000", str(out_path)],
        timeout=600,
    )
    if rc != 0:
        raise RuntimeError(f"atempo failed: {err.strip()[-300:]}")
    return {"out": str(out_path), "factor": factor, "chain": chain}
