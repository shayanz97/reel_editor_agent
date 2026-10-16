import wave
from pathlib import Path

from reels.domain.words import RawWord
from reels.pipeline.bench import format_report, run_benchmark
from reels.transcribe.base import FakeEngine


def _wav(path: Path, seconds: float = 2.0) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def test_benchmark_runs_engines_and_formats_report(tmp_path: Path) -> None:
    a = FakeEngine(
        [RawWord(text="Heute", start=0, end=0.4), RawWord(text="Docker.", start=0.5, end=1)]
    )
    b = FakeEngine(
        [RawWord(text="heute", start=0.05, end=0.4), RawWord(text="Doker", start=0.5, end=1.1)]
    )
    a.name, b.name = "whisperx", "mlx"
    result = run_benchmark(
        "clip.mp4",
        _wav(tmp_path / "a.wav"),
        {"whisperx": a, "mlx": b},
        runs=2,
        hint="Fachbegriffe: Docker.",
    )
    assert len(a.calls) == 2 and a.calls[0][2] == "Fachbegriffe: Docker."
    assert result.audio_seconds == 2.0
    assert result.comparison is not None and result.comparison.matched == 1
    report = format_report(result)
    assert "| whisperx | fake |" in report
    assert "| Docker. | Doker |" in report
    assert "Audiolänge: 2.0 s" in report


def test_single_engine_has_no_comparison(tmp_path: Path) -> None:
    result = run_benchmark("x", _wav(tmp_path / "a.wav"), {"fake": FakeEngine([])})
    assert result.comparison is None
    assert "Vergleich" not in format_report(result)
