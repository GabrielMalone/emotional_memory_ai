import subprocess
import threading
import pyaudio


class StreamingMP3Player:
    def __init__(self):
        self.on_drain = None  # callback set by client

        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel", "quiet",
                "-i", "pipe:0",
                "-f", "s16le",
                "-ac", "1",
                "-ar", "22050",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

        self.audio = pyaudio.PyAudio()
        self.stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=22050,
            output=True,
        )

        self._closed = False
        self._play_thread = threading.Thread(
            target=self._play_loop, daemon=True
        )
        self._play_thread.start()

    # --------------------------------------------------
    # playback loop: runs until ffmpeg stdout closes
    # --------------------------------------------------
    def _play_loop(self):
        while True:
            data = self.proc.stdout.read(4096)
            if not data:
                break
            self.stream.write(data)

        # 🔔 audio fully drained here
        if self.on_drain:
            self.on_drain()

    # --------------------------------------------------
    # feed mp3 bytes (None = end-of-stream)
    # --------------------------------------------------
    def feed(self, mp3_bytes):
        if self._closed:
            return

        if mp3_bytes is None:
            self._closed = True
            try:
                self.proc.stdin.close()  # tells ffmpeg no more input
            except Exception:
                pass
            return

        try:
            self.proc.stdin.write(mp3_bytes)
            self.proc.stdin.flush()
        except Exception:
            pass

    # --------------------------------------------------
    # optional explicit close
    # --------------------------------------------------
    def close(self):
        if not self._closed:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self._closed = True

        try:
            self.proc.wait(timeout=1)
        except Exception:
            pass

        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass

        try:
            self.audio.terminate()
        except Exception:
            pass