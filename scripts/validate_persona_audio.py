"""Actual configured STT/TTS round trips; run with Channel's Python and PYTHONPATH.

Input phase synthesizes known speech then recognizes it for the Agent journey.
Output phase synthesizes real Agent replies and recognizes the generated audio,
including a long reply, to detect empty output or missing tail content.
No microphone, device playback, user account changes, or user recordings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path

from eidolon.livekit.agent.factory import SharedStageFactory
from eidolon.livekit.common.config import load_effective_config
from livekit.agents.voice.transcription.filters import filter_markdown


async def round_trip(factory, text, path, *, streaming=False):
    started = time.monotonic()
    frames = []
    rates = set()
    first_frame_ms = None

    async def audio_frames():
        if not streaming:
            async for frame in factory.tts.synthesize(text):
                yield frame
            return

        # AgentSession's TTS node uses the SDK Markdown filter and streams text
        # into the plugin; exercise that path as well as the one-shot boundary.
        async def chunks():
            for offset in range(0, len(text), 8):
                yield text[offset : offset + 8]
                await asyncio.sleep(0.02)

        stream = factory.tts.stream()

        async def feed():
            async for chunk in filter_markdown(chunks()):
                stream.push_text(chunk)
            stream.end_input()

        feeder = asyncio.create_task(feed())
        try:
            async for event in stream:
                yield event.frame
            await feeder
        finally:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
            await stream.aclose()

    # Detailed answers can contain minutes of speech; this batch validation
    # waits for EOF and must not impose a 60-second cap on the reply itself.
    async with asyncio.timeout(max(60, len(text) / 3 + 30)):
        async for frame in audio_frames():
            if first_frame_ms is None:
                first_frame_ms = round((time.monotonic() - started) * 1000)
            assert frame.num_channels == 1
            rates.add(frame.sample_rate)
            frames.append(bytes(frame.data))
    assert frames and rates == {16000}, (
        f"unexpected audio format: rates={rates} frames={len(frames)}"
    )
    pcm = b"".join(frames)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    # This is an omitted-tail check. Short clips are recognized in full; a
    # multi-minute reply uses its last 15 seconds instead of treating a whole
    # monologue as one STT request. Full-length ASR was separately attempted
    # and hit provider timeouts; tail verification does not certify the middle.
    tail_only = len(pcm) > 20 * 32000
    recognition_pcm = pcm[-15 * 32000 :] if tail_only else pcm
    async with asyncio.timeout(60):
        transcript = await factory.stt.recognize_streaming(recognition_pcm)

    def normalize(value):
        return "".join(c.lower() for c in value if c.isalnum())

    source, recognized = normalize(text), normalize(transcript)
    tail = source[-18:]
    # Fuzzy tail matching tolerates ASR wording while exposing an omitted ending.
    match = SequenceMatcher(None, tail, recognized, autojunk=False).find_longest_match()
    return {
        "text": text,
        "tts_path": "sdk_filtered_stream" if streaming else "one_shot",
        "transcript": transcript,
        "wav": str(path),
        "tts_first_frame_ms": first_frame_ms,
        "round_trip_ms": round((time.monotonic() - started) * 1000),
        "audio_ms": round(len(pcm) / 32),
        "frames": len(frames),
        "recognition_scope": "last_15_seconds" if tail_only else "full",
        "similarity": None
        if tail_only
        else round(SequenceMatcher(None, source, recognized, autojunk=False).ratio(), 3),
        "tail_match": round(match.size / max(len(tail), 1), 3),
        "nonempty": bool(transcript.strip()),
    }


async def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cfg = load_effective_config()
    factory = SharedStageFactory.components_from_config(cfg)
    cases = [
        ("speech-emotion", "今天工作有点累，我只是想说说，不需要建议。"),
        ("speech-detail", "请详细解释怎么准备自我介绍，分三个步骤，每步给一个例子。"),
    ]
    if args.phase == "output":
        rows = [json.loads(line) for line in Path(args.journeys).read_text().splitlines()]
        cases = [
            ("reply-" + row["kind"], row["reply"])
            for row in rows
            if row["preset"] == "gentle" and row["kind"] in {"emotion", "detail", "task"}
        ]
    results = []
    try:
        await factory.tts.warmup()
        for label, text in cases:
            row = {
                "case": label,
                **await round_trip(
                    factory, text, output / f"{label}.wav", streaming=args.streaming
                ),
            }
            results.append(row)
            (output / f"{args.phase}.json").write_text(
                json.dumps(
                    {
                        "stt": cfg.providers.stt_provider,
                        "tts": cfg.providers.tts_provider,
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print(
                json.dumps(
                    {k: v for k, v in row.items() if k not in {"text", "wav"}}, ensure_ascii=False
                ),
                flush=True,
            )
    finally:
        await factory.stt.shutdown()
        await factory.tts.shutdown()
    return 0 if all(row["nonempty"] and row["tail_match"] >= 0.4 for row in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["input", "output"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--journeys")
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Use the production SDK Markdown filter and streaming TTS input",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
