#!/bin/bash
# Installs an offline speech-to-text command: `transcribe <audio or video file> [out.txt]`.
# Uses Whisper small.en (int8) through sherpa-onnx, with Silero VAD to split long recordings.
# Paste this whole file into the cloud environment's Setup script so every new session has it.
set -euo pipefail

DIR=/opt/transcriber
MODEL=sherpa-onnx-whisper-small.en
REL=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models

pip install -q --break-system-packages sherpa-onnx numpy
command -v ffmpeg >/dev/null || { apt-get update -q && apt-get install -y -q ffmpeg; }

mkdir -p "$DIR"
if [ ! -f "$DIR/$MODEL/small.en-encoder.int8.onnx" ]; then
  curl -fsSL "$REL/$MODEL.tar.bz2" | tar xj -C "$DIR"
  # Only the int8 weights are used; the full-precision copies are ~1GB of dead weight.
  rm -f "$DIR/$MODEL/small.en-encoder.onnx" "$DIR/$MODEL/small.en-decoder.onnx"
fi
[ -f "$DIR/silero_vad.onnx" ] || curl -fsSL -o "$DIR/silero_vad.onnx" "$REL/silero_vad.onnx"

cat > "$DIR/transcribe.py" <<'PY'
"""Transcribe an audio/video file offline. Usage: transcribe <input> [output.txt]"""
import os
import subprocess
import sys

import numpy as np
import sherpa_onnx

DIR = "/opt/transcriber"
MODEL = f"{DIR}/sherpa-onnx-whisper-small.en/small.en"
SR = 16000


def load_audio(path):
    pcm = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def main(src, dst):
    recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=f"{MODEL}-encoder.int8.onnx",
        decoder=f"{MODEL}-decoder.int8.onnx",
        tokens=f"{MODEL}-tokens.txt",
        language="en",
        task="transcribe",
        num_threads=os.cpu_count() or 4,
    )
    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = f"{DIR}/silero_vad.onnx"
    vad_cfg.silero_vad.min_silence_duration = 0.4
    vad_cfg.silero_vad.max_speech_duration = 25  # Whisper decodes 30s windows
    vad_cfg.sample_rate = SR
    vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=120)

    audio = load_audio(src)
    print(f"{len(audio) / SR / 60:.1f} min of audio -> {dst}", file=sys.stderr, flush=True)

    lines = []

    def drain():
        while not vad.empty():
            seg = vad.front
            stream = recognizer.create_stream()
            stream.accept_waveform(SR, seg.samples)
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                start = seg.start / SR
                lines.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
                print(lines[-1], flush=True)
            vad.pop()

    window = vad_cfg.silero_vad.window_size
    for i in range(0, len(audio), window):
        vad.accept_waveform(audio[i:i + window])
        drain()
    vad.flush()
    drain()

    with open(dst, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    src = sys.argv[1]
    main(src, sys.argv[2] if len(sys.argv) == 3 else os.path.splitext(src)[0] + ".txt")
PY

cat > /usr/local/bin/transcribe <<'SH'
#!/bin/sh
exec python3 -I /opt/transcriber/transcribe.py "$@"
SH
chmod +x /usr/local/bin/transcribe
echo "transcriber ready: transcribe <file> [out.txt]"
