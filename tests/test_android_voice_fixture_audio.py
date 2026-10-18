"""The diagnostic WAV survives Uvicorn's actual SIGTERM re-raise."""
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import wave

import pytest


@pytest.mark.parametrize("exit_mode", ["normal", "lifespan_error", "sigterm"])
def test_fixture_exit_preserves_complete_readable_microphone_wav(tmp_path, exit_mode):
    script = Path(__file__).parents[1] / "scripts/android_voice_fixture.py"
    child = textwrap.dedent("""
        import asyncio, importlib.util, signal, sys, wave
        from pathlib import Path
        import uvicorn

        script, root, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
        sys.path.insert(0, str(script.parents[1] / 'src'))
        spec = importlib.util.spec_from_file_location('actual_fixture', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        audio = root / 'audio'
        audio.mkdir()
        for name in module.PHRASES:
            with wave.open(str(audio / (name + '.wav')), 'wb') as output:
                output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                output.writeframes(b'\\x01\\0' * 1200)
        fixture = module.Fixture(root / 'store', audio)
        # Deliberately use the actual incremental writer; its initial header
        # describes only the first chunk until normal Wave_write.close().
        fixture.mic.writeframesraw(b'\\x01\\0' * 304)
        fixture.mic.writeframesraw(b'\\x02\\0' * 16000)
        messages = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])

        async def receive():
            return next(messages)

        async def send(message):
            pass

        async def app(scope, receive, send):
            assert (await receive())['type'] == 'lifespan.startup'
            await send({'type': 'lifespan.startup.complete'})
            if mode == 'lifespan_error':
                raise RuntimeError('synthetic shutdown regression')
            if mode == 'sigterm':
                signal.raise_signal(signal.SIGTERM)
            assert (await receive())['type'] == 'lifespan.shutdown'
            await send({'type': 'lifespan.shutdown.complete'})

        server = uvicorn.Server(uvicorn.Config(app))
        try:
            # No listener/socket/provider. The installed Uvicorn signal manager
            # really re-raises SIGTERM after the lifespan coroutine returns.
            with server.capture_signals():
                asyncio.run(fixture.wrap(app)({'type': 'lifespan'}, receive, send))
        except RuntimeError:
            raise SystemExit(3)
        finally:
            fixture.close()
            (root / 'outer-finally-ran').write_text('yes')
    """)
    result = subprocess.run([sys.executable, "-B", "-c", child, str(script), str(tmp_path), exit_mode],
                            capture_output=True, text=True, timeout=10)
    expected_code = {"normal": 0, "lifespan_error": 3, "sigterm": -signal.SIGTERM}[exit_mode]
    assert result.returncode == expected_code, result.stderr
    if exit_mode == "sigterm":
        assert not (tmp_path / "outer-finally-ran").exists()
    expected = b"\x01\0" * 304 + b"\x02\0" * 16000
    with wave.open(str(tmp_path / "audio/decoded-microphone.wav"), "rb") as source:
        assert source.getnframes() == len(expected) // 2
        assert source.readframes(source.getnframes()) == expected
