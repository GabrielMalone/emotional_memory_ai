import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
from pathlib import Path
import time
import threading


class AudioRecorder:
    def __init__(
        self,
        sample_rate=16000,
        output_dir="recordings",
        silence_duration=0.8,
        silence_threshold=0.005,
        max_duration=15.0,
    ):
        self.sample_rate = sample_rate
        self.silence_duration = silence_duration
        self.silence_threshold = silence_threshold
        self.max_duration = max_duration

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def record(self, filename=None) -> Path:
        if filename is None:
            filename = f"mic_{int(time.time())}.wav"

        output_path = self.output_dir / filename

        frames = []
        silence_start = None
        has_spoken = False
        start_time = time.time()

        done = threading.Event()

        def callback(indata, frames_count, time_info, status):
            nonlocal silence_start, has_spoken

            frames.append(indata.copy())
            amp = np.abs(indata).mean()
            now = time.time()

            if amp > self.silence_threshold:
                has_spoken = True
                silence_start = None
            else:
                if has_spoken and silence_start is None:
                    silence_start = now

            if has_spoken and silence_start and (now - silence_start) > self.silence_duration:
                done.set()
                raise sd.CallbackStop()

            if (now - start_time) > self.max_duration:
                done.set()
                raise sd.CallbackStop()

        print("Listening… speak, then pause.")

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=int(self.sample_rate * 0.03),
            callback=callback,
        ):
            done.wait()

        audio = np.concatenate(frames, axis=0)
        write(output_path, self.sample_rate, audio)

        return output_path