"""Text to speech for Real Routes announcements, run by BeamDeck with Piper's own Python (the piper venv).

One JSON request per line on stdin: {"id": 3, "text": "...", "voice": 92, "out": "/path/3.wav", "rate": 1.0}
One JSON reply per line on stdout:  {"id": 3, "ok": true} or {"id": 3, "ok": false, "error": "..."}
The voice model is loaded once, so each line takes a fraction of a second.

    python tts_worker.py <model.onnx>
"""
import json
import sys
import wave

from piper import PiperVoice, SynthesisConfig


def main():
    voice = PiperVoice.load(sys.argv[1])
    speakers = int(voice.config.num_speakers or 1)
    print(json.dumps({"ready": True, "speakers": speakers}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            cfg = SynthesisConfig(speaker_id=int(req.get("voice", 0)) % speakers if speakers > 1 else None,
                                  length_scale=1.0 / max(0.5, float(req.get("rate", 1.0))))
            with wave.open(req["out"], "wb") as w:
                voice.synthesize_wav(str(req["text"]), w, syn_config=cfg)
            print(json.dumps({"id": req.get("id"), "ok": True}), flush=True)
        except Exception as e:  # one bad line must not stop the worker
            print(json.dumps({"id": req.get("id"), "ok": False, "error": str(e)}), flush=True)


if __name__ == "__main__":
    main()
