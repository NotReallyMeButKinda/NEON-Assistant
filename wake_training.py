"""
wake_training.py -- teach the wake-word detector a new phrase, on this PC.

How a phrase is learned (see wakeword.py for how it is then detected):

  1. Examples of the phrase. Every installed Piper voice says it at several speeds and with
     different amounts of variation; your own recordings (Settings > Listening > Train a wake word)
     are added on top and count for more, because they are what the detector will actually hear.
  2. Examples of what it is NOT: near misses built from the phrase itself ("hey nova" -> "hey",
     "nova", "hey now", "hey anna"...), everyday commands, noise, and optionally a recording of your
     room.
  3. Every example is placed in a couple of seconds of noise and run through the same feature stream
     the detector uses (wakeword.FeatureStream), and a small network (1536 -> 32 -> 1) is trained to
     tell the two apart. A part of the data is held back to report how well it did.

The result is saved as <data>/wakeword/custom/<phrase>.npz (a few hundred KB). Training takes one to four
minutes on a typical CPU (more voices = longer, and better) and needs no internet once the shared feature
models are downloaded.

Measured here (trained without one voice, then tested on that voice, a man's, with 36 s of other speech):
"hey nova" woke 2/3 times with 0 false triggers from the four standard voices; "hey dan", a shorter phrase,
went from 1/3 to 3/3 (1 false trigger) once MANY_SPEAKERS_VOICE was added. Your own recordings help most.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

import neon_log
import wakeword

log = neon_log.get("wake_training")

RATE = 16000
CLIP_SECONDS = 2.2                   # each example sits in this much audio
SPEEDS = (0.8, 0.95, 1.1, 1.3)       # Piper length_scale: > 1 is slower
VARIATION = (0.45, 0.9)              # Piper noise_scale: how much each take differs
HIDDEN = 32
EPOCHS = 400
PITCHES = (0.82, 1.18)              # extra copies of each generated clip, lower and higher (more "speakers")
HARD_ROUNDS = 1                     # retrain once with the negatives the first model liked, counted 4x
MIN_RECORDINGS_WITHOUT_VOICES = 5
MANY_SPEAKERS_VOICE = "en_US-libritts_r-medium"   # one Piper model, ~900 different speakers (78 MB)
SPEAKERS_PER_VOICE = 16                            # how many of a multi-speaker model's speakers to use

EVERYDAY = ["what time is it", "play some music", "open the browser", "turn it down", "hello everyone",
            "have a nice day", "i know that", "no way", "so what", "okay google", "hey siri", "alexa",
            "hey jarvis", "what's the weather", "set a timer", "thank you", "good morning", "hey there",
            "hey you", "hey man", "hey dave", "hey anna", "hey donna", "hey now", "hey no", "a nova scotia",
            "novel idea", "hey dan", "hey nova", "hey stan", "hey jan", "dan", "nova", "hey"]
SOUNDS_LIKE = {"nova": ["novel", "no va", "over", "nola", "noah"], "dan": ["dad", "stan", "dance", "then", "done"],
               "hey": ["hay", "a", "they", "day"]}


class TrainingError(Exception):
    pass


# ---------------------------------------------------------------------------
# Example audio
# ---------------------------------------------------------------------------

def confusables(phrase: str) -> list[str]:
    """Near misses of `phrase`: its words alone, one word swapped for a similar one, other names
    after the same greeting. They teach the detector that half the phrase isn't the phrase."""
    words = phrase.lower().split()
    out: list[str] = []
    if len(words) > 1:
        out += words                                            # "hey", "nova"
        out.append(" ".join(words[:-1]))
        out.append(" ".join(words[1:]))
    for i, word in enumerate(words):
        for alt in SOUNDS_LIKE.get(word, []):
            out.append(" ".join(words[:i] + [alt] + words[i + 1:]))
    if words and words[0] in ("hey", "hi", "okay", "ok"):
        for name in ("man", "anna", "dave", "you", "there", "now", "siri"):
            out.append(f"{words[0]} {name}")
    target = " ".join(words)
    seen, unique = set(), []
    for text in out + EVERYDAY:
        text = text.strip()
        if text and text != target and text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


def installed_voices() -> list[Path]:
    import tts
    return [tts.VOICES_DIR / f"{name}.onnx" for name in tts.list_installed_voices()]


def resample(audio: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE:
        return audio.astype(np.float32)
    count = int(len(audio) * RATE / rate)
    return np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio).astype(np.float32)


class Synth:
    """The installed Piper voices, loaded once, saying things at 16 kHz. A multi-speaker model (like
    MANY_SPEAKERS_VOICE) contributes SPEAKERS_PER_VOICE different people instead of one."""

    def __init__(self, voice_files: list[Path], cache_dir: Path | None = None):
        from piper import PiperVoice
        self.files = [Path(p) for p in voice_files]
        self.voices = [PiperVoice.load(str(path)) for path in self.files]
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_hits = 0

    def _cache_path(self, index: int, text: str, speaker, speed: float, noise: float) -> Path | None:
        """Where one synthesized take is kept, keyed by everything that shapes it (the voice file's size
        and date included, so an updated voice is never mixed up with the old one)."""
        if self.cache_dir is None:
            return None
        try:
            stat = self.files[index].stat()
        except OSError:
            return None
        key = f"{self.files[index].name}|{stat.st_size}|{stat.st_mtime_ns}|{speaker}|{speed}|{noise}|{RATE}|{text}"
        return self.cache_dir / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.npy"

    @property
    def speakers(self) -> int:
        return sum(min(SPEAKERS_PER_VOICE, v.config.num_speakers) if v.config.num_speakers > 1 else 1
                   for v in self.voices)

    def _takes(self, voice, speeds, variation):
        """(speaker id, speed, variation) for every take of one voice."""
        count = voice.config.num_speakers
        if count <= 1:
            return [(None, s, n) for s in speeds for n in variation]
        ids = np.linspace(0, count - 1, SPEAKERS_PER_VOICE).astype(int)
        combos = [(s, n) for s in speeds for n in variation]
        return [(int(sid), *combos[i % len(combos)]) for i, sid in enumerate(ids)]    # one take per speaker

    def say(self, text: str, speeds=SPEEDS, variation=VARIATION) -> list[np.ndarray]:
        from piper import SynthesisConfig
        clips = []
        for index, voice in enumerate(self.voices):
            for speaker, speed, noise in self._takes(voice, speeds, variation):
                cached = self._cache_path(index, text, speaker, speed, noise)
                if cached is not None and cached.exists():
                    try:
                        clips.append(np.load(cached).astype(np.float32))    # retraining: no synthesis at all
                        self.cache_hits += 1
                        continue
                    except (OSError, ValueError):
                        pass
                config = SynthesisConfig(speaker_id=speaker, length_scale=speed, noise_scale=noise, noise_w_scale=noise)
                chunks = [c.audio_int16_array for c in voice.synthesize(text, config)]
                if chunks:
                    clip = resample(np.concatenate(chunks).astype(np.float32), voice.config.sample_rate)
                    clips.append(clip)
                    if cached is not None:
                        _save_clip(cached, clip)
        return clips


SYNTH_CACHE_MB = 400                                  # the synthesized-speech cache is trimmed to this


def _save_clip(path: Path, clip: np.ndarray) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".part.npy")
        np.save(tmp, np.clip(clip, -32768, 32767).astype(np.int16))      # int16: half the size, no loss
        tmp.replace(path)
    except OSError:
        pass                                          # a full disk only costs the speed-up


def trim_synth_cache(folder: Path, limit_mb: float = SYNTH_CACHE_MB) -> None:
    """Oldest takes go first once the cache is bigger than `limit_mb`."""
    try:
        files = sorted(folder.glob("*.npy"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= limit_mb * 1024 * 1024:
                break
            total -= p.stat().st_size
            p.unlink()
    except OSError:
        pass


def shift_pitch(audio: np.ndarray, factor: float) -> np.ndarray:
    """Pitch and tempo scaled together by `factor` (> 1 = higher): a cheap way to get a different-sounding
    speaker out of the same voice. Plain resampling, so it also changes the length a little."""
    count = int(len(audio) / factor)
    return np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio).astype(np.float32)


def with_pitches(clips: list[np.ndarray]) -> list[np.ndarray]:
    return clips + [shift_pitch(c, f) for c in clips for f in PITCHES]


def trim(recording: np.ndarray) -> np.ndarray:
    """A recording with the silence around the speech cut off (by frame loudness)."""
    audio = np.asarray(recording, dtype=np.float32)
    frame = 320
    count = len(audio) // frame
    if count == 0:
        return audio
    energy = np.abs(audio[:count * frame]).reshape(count, frame).mean(axis=1)
    floor = np.percentile(energy, 20)
    loud = np.nonzero(energy > floor + 0.25 * (energy.max() - floor))[0]
    if len(loud) == 0:
        return audio
    start = max(0, (loud[0] - 3) * frame)
    end = min(len(audio), (loud[-1] + 4) * frame)
    return audio[start:end]


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def _place(clip: np.ndarray, rng: np.random.Generator, noise: float, background: np.ndarray | None,
           gain: float) -> tuple[np.ndarray, int]:
    """The clip inside CLIP_SECONDS of noise, ending 0.1-0.35 s before the end. Returns (audio, end sample)."""
    total = int(CLIP_SECONDS * RATE)
    if background is not None and len(background) > total:
        offset = int(rng.integers(0, len(background) - total))
        buf = background[offset:offset + total].astype(np.float32).copy()
    else:
        buf = np.zeros(total, dtype=np.float32)
    buf += rng.normal(0.0, noise, total).astype(np.float32)
    end = total - int(rng.uniform(0.1, 0.35) * RATE)
    piece = clip[-min(len(clip), end):]
    buf[end - len(piece):end] += piece * gain
    return np.clip(buf, -32768, 32767), end


def _embeddings(audio: np.ndarray, stream: wakeword.FeatureStream) -> np.ndarray:
    stream.reset()
    stream.feed(audio, keep_all=True)
    return np.array(stream.embeddings, dtype=np.float32)


def _windows(emb: np.ndarray, indices) -> list[np.ndarray]:
    w = wakeword.WINDOW
    return [emb[k - w + 1:k + 1].reshape(-1) for k in indices if w - 1 <= k < len(emb)]


def _positive_windows(emb: np.ndarray, end: int) -> list[np.ndarray]:
    first = -(-end // wakeword.FRAME) - 1          # the first embedding whose audio reaches the end of the phrase
    return _windows(emb, (first, first + 1, first + 2))


def _partial_windows(emb: np.ndarray, end: int, length: int) -> list[np.ndarray]:
    """Windows that end while the phrase is only half said: "hey no-" must not wake it."""
    first = -(-end // wakeword.FRAME) - 1
    half = max(1, int(length * 0.55) // wakeword.FRAME)
    return _windows(emb, range(first - half - 3, first - half + 1))


def _negative_windows(emb: np.ndarray, step: int = 2) -> list[np.ndarray]:
    return _windows(emb, range(wakeword.WINDOW - 1, len(emb), step))


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def _fit(x: np.ndarray, y: np.ndarray, weights: np.ndarray, rng: np.random.Generator, on_progress) -> dict:
    """Full-batch Adam on a 1-hidden-layer network with weighted cross-entropy."""
    mean = x.mean(axis=0)
    scale = x.std(axis=0) + 1e-3
    xn = (x - mean) / scale
    params = [rng.normal(0, 0.02, (x.shape[1], HIDDEN)).astype(np.float32), np.zeros(HIDDEN, np.float32),
              rng.normal(0, 0.1, HIDDEN).astype(np.float32), np.zeros(1, np.float32)]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    total = weights.sum()
    for epoch in range(1, EPOCHS + 1):
        w1, b1, w2, b2 = params
        hidden = np.maximum(0.0, xn @ w1 + b1)
        p = 1.0 / (1.0 + np.exp(-np.clip(hidden @ w2 + b2[0], -30, 30)))
        g = (p - y) * weights / total
        gh = np.outer(g, w2) * (hidden > 0)
        grads = [xn.T @ gh + 1e-4 * w1, gh.sum(axis=0), hidden.T @ g + 1e-4 * w2, np.array([g.sum()], np.float32)]
        for i, (param, grad) in enumerate(zip(params, grads)):
            m[i] = 0.9 * m[i] + 0.1 * grad
            v[i] = 0.999 * v[i] + 0.001 * grad * grad
            param -= (1e-3 * (m[i] / (1 - 0.9 ** epoch)) / (np.sqrt(v[i] / (1 - 0.999 ** epoch)) + 1e-8)).astype(np.float32)
        if epoch % 50 == 0:
            on_progress(0.75 + 0.2 * epoch / EPOCHS, "Training...")
    return {"w1": params[0], "b1": params[1], "w2": params[2], "b2": params[3],
            "mean": mean.astype(np.float32), "scale": scale.astype(np.float32)}


def train(phrase: str, recordings=(), background=(), directory: Path | None = None, on_progress=None,
          seed: int = 0, voices: list[Path] | None = None, synth: Synth | None = None) -> dict:
    """Trains and saves a detector for `phrase`. `recordings`: the user saying it (int16 16 kHz arrays);
    `background`: recordings of the room without it. Returns a report dict (path, recall, false_alarms...).
    Raises TrainingError when there isn't enough to learn from."""
    started = time.monotonic()
    on_progress = on_progress or (lambda _fraction, _text: None)
    phrase = " ".join(str(phrase).lower().split())
    name = wakeword.slug(phrase)
    if len(name) < 2:
        raise TrainingError("Type the phrase you want to use, like \"hey nova\".")
    folder = wakeword.ensure_feature_models(lambda text: on_progress(0.0, text), directory)
    rng = np.random.default_rng(seed)

    voice_files = installed_voices() if voices is None else voices
    recordings = [trim(r) for r in recordings if len(r) > RATE // 4]
    cache = (Path(directory) if directory else wakeword.models_dir()) / "synth_cache"
    if synth is None and voice_files:
        on_progress(0.02, "Loading voices...")
        synth = Synth(voice_files, cache_dir=cache)
    if synth is None and len(recordings) < MIN_RECORDINGS_WITHOUT_VOICES:
        raise TrainingError(f"Without a Piper voice to generate examples I need at least "
                            f"{MIN_RECORDINGS_WITHOUT_VOICES} recordings of you saying it.")

    positives: list[np.ndarray] = []
    negatives: list[np.ndarray] = []
    if synth is not None:
        on_progress(0.05, f"Generating \"{phrase}\" in {synth.speakers} voices...")
        positives = with_pitches(synth.say(phrase))
        near = confusables(phrase)
        for i, text in enumerate(near):
            negatives += with_pitches(synth.say(text, speeds=(0.9, 1.15), variation=(0.7,)))
            on_progress(0.08 + 0.3 * (i + 1) / len(near), "Generating examples of other words...")
    rooms = [np.asarray(b, dtype=np.float32) for b in background if len(b) > RATE]
    room = np.concatenate(rooms) if rooms else None

    # (audio, label, weight, end-of-phrase sample or None, phrase length)
    jobs: list[tuple[np.ndarray, int, float, int | None, int]] = []
    for clip in positives:
        for _ in range(2):
            audio, end = _place(clip, rng, rng.uniform(0, 300), room, rng.uniform(0.3, 1.2))
            jobs.append((audio, 1, 1.0, end, len(clip)))
    for clip in recordings:
        for _ in range(10):                                   # the user's own voice counts for more
            peak = float(np.abs(clip).max()) or 1.0
            gain = rng.uniform(0.5, 1.5) * min(1.0, 12000.0 / peak)
            audio, end = _place(shift_pitch(clip, rng.uniform(0.95, 1.05)), rng, rng.uniform(0, 200), room, gain)
            jobs.append((audio, 1, 3.0, end, len(clip)))
    for clip in negatives:
        audio, _end = _place(clip, rng, rng.uniform(0, 300), room, rng.uniform(0.3, 1.2))
        jobs.append((audio, 0, 1.0, None, 0))
    for _ in range(30):                                       # plain noise, from silence to loud hiss
        jobs.append((rng.normal(0, rng.uniform(1, 800), int(CLIP_SECONDS * RATE)).astype(np.float32), 0, 1.0, None, 0))
    if negatives:                                             # a few utterances back to back, like real talk
        for _ in range(40):
            picks = [negatives[i] for i in rng.integers(0, len(negatives), 3)]
            gap = [rng.normal(0, rng.uniform(5, 200), int(rng.uniform(0.05, 0.6) * RATE)).astype(np.float32)]
            jobs.append((np.concatenate([picks[0], *gap, picks[1], *gap, picks[2]]), 0, 1.0, None, 0))
    if room is not None:
        for start in range(0, len(room), RATE * 2):
            piece = room[start:start + RATE * 2 + RATE // 2]
            if len(piece) > RATE:
                jobs.append((piece, 0, 2.0, None, 0))

    streams: dict[int, wakeword.FeatureStream] = {}

    def features(job):
        import threading
        key = threading.get_ident()
        if key not in streams:
            streams[key] = wakeword.FeatureStream(folder)
        return _embeddings(job[0], streams[key])

    x, y, w = [], [], []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for job, emb in zip(jobs, pool.map(features, jobs)):
            _audio, label, weight, end, length = job
            if label:
                rows = _positive_windows(emb, end)
                partial = _partial_windows(emb, end, length)
                x += rows + partial
                y += [1] * len(rows) + [0] * len(partial)
                w += [weight] * len(rows) + [1.0] * len(partial)
            else:
                rows = _negative_windows(emb, step=1)
                x += rows
                y += [0] * len(rows)
                w += [weight] * len(rows)
            done += 1
            if done % 25 == 0:
                on_progress(0.4 + 0.35 * done / len(jobs), "Listening to the examples...")
    if not any(y):
        raise TrainingError("None of the examples could be used; try recording again, a little louder.")

    x_all = np.array(x, dtype=np.float32)
    y_all = np.array(y, dtype=np.float32)
    w_all = np.array(w, dtype=np.float32)
    order = rng.permutation(len(x_all))
    held = order[:len(order) // 7]
    kept = order[len(order) // 7:]
    positive_share = max(1e-3, float(y_all[kept].mean()))
    balance = np.where(y_all[kept] == 1, (1 - positive_share) / positive_share, 1.0) * w_all[kept]
    weights = _fit(x_all[kept], y_all[kept], balance.astype(np.float32), rng, on_progress)
    for _ in range(HARD_ROUNDS):
        # Hard negatives: whatever the first model mistook for the phrase is counted 4x in a retrain.
        scores = wakeword.Classifier(weights).score(x_all[kept])
        hard = (y_all[kept] == 0) & (scores >= 0.2)
        if not hard.any():
            break
        balance = balance * np.where(hard, 4.0, 1.0)
        weights = _fit(x_all[kept], y_all[kept], balance.astype(np.float32), rng, on_progress)

    scores = wakeword.Classifier(weights).score(x_all[held])
    is_pos = y_all[held] == 1
    recall = float((scores[is_pos] >= 0.5).mean()) if is_pos.any() else 0.0
    false_alarms = float((scores[~is_pos] >= 0.5).mean()) if (~is_pos).any() else 0.0
    meta = {"phrase": phrase, "created": datetime.now().isoformat(timespec="seconds"),
            "recordings": len(recordings), "synthetic": len(positives), "voices": synth.speakers if synth else 0,
            "recall": round(recall, 3), "false_alarms": round(false_alarms, 4)}
    path = wakeword.custom_path(wakeword.CUSTOM + name, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.npz")
    np.savez(tmp, meta=np.array(json.dumps(meta)), **weights)
    tmp.replace(path)
    trim_synth_cache(cache)
    seconds = time.monotonic() - started
    log.info("trained wake phrase %r in %.0f s (%d cached takes reused): %s", phrase, seconds,
             getattr(synth, "cache_hits", 0), meta)
    on_progress(1.0, "Done.")
    return {**meta, "path": str(path), "model": wakeword.CUSTOM + name, "seconds": round(seconds, 1)}


def describe(report: dict) -> str:
    """One line for the Settings window."""
    return (f"\"{report['phrase']}\" is ready: it recognised {report['recall']:.0%} of the held-back examples "
            f"and false-triggered on {report['false_alarms']:.1%} of the other sounds "
            f"({report['recordings']} of your recordings, {report['synthetic']} generated, "
            f"{report.get('seconds', 0):.0f} s).")
