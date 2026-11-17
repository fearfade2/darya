"""Local speech recognition/synthesis. Audio and transcripts stay in call memory."""
from __future__ import annotations

import asyncio
import gc
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


class CallUnavailable(ValueError):
    pass


class VoiceEngine:
    def __init__(self, directory: Path):
        self.root = directory / 'voice-models'
        self.whisper = None
        self.tts = None
        self.speaker = 'xenia'
        self.lock = threading.Lock()

    def check(self):
        files = ['silero/v5_6_ru.pt', 'whisper-base/config.json',
                 'whisper-base/model.bin', 'whisper-base/tokenizer.json']
        if not all((self.root / name).is_file() for name in files):
            raise CallUnavailable('Голосовые модели ещё не установлены на сервере')

    def load(self, recognition=True, synthesis=False):
        self.check()
        if recognition and self.whisper is None:
            from faster_whisper import WhisperModel
            self.whisper = WhisperModel(str(self.root / 'whisper-base'), device='cpu',
                                        compute_type='int8', cpu_threads=2, num_workers=1,
                                        local_files_only=True)
        if synthesis and self.tts is None:
            import torch
            torch.set_num_threads(2)
            self.tts = torch.package.PackageImporter(str(self.root / 'silero/v5_6_ru.pt')).load_pickle('tts_models','model')
            self.tts.to(torch.device('cpu'))

    def _prepare(self):
        with self.lock:
            self.load()

    async def prepare(self):
        await asyncio.to_thread(self._prepare)

    def _transcribe(self, pcm, language):
        import numpy as np
        with self.lock:
            self.load()
            samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
            segments, _ = self.whisper.transcribe(samples, language=None if language == 'auto' else language,
                beam_size=1, vad_filter=True, condition_on_previous_text=False,
                no_speech_threshold=0.65, hallucination_silence_threshold=1)
            return ' '.join(s.text.strip() for s in segments if s.avg_logprob >= -1.0).strip()[:3000]

    def _release(self):
        with self.lock:
            loaded = self.whisper is not None or self.tts is not None
            self.whisper = self.tts = None
            if loaded:
                gc.collect()
                # Return freed native inference arenas to Linux when supported.
                try:
                    import ctypes
                    trim = ctypes.CDLL(None).malloc_trim
                    trim(0)
                except (AttributeError, OSError):
                    pass

    async def release(self):
        # Wait for a cancelled worker's running inference before releasing it.
        await asyncio.to_thread(self._release)

    async def transcribe(self, pcm, language='ru'):
        return await asyncio.to_thread(self._transcribe, pcm, language)

    def _synthesize(self, text, speed, volume, max_seconds):
        import numpy as np
        import torch
        import subprocess
        with self.lock:
            self.load(recognition=False, synthesis=True)
            arrays = []
            samples_used = 0
            # Short sentences improve intonation and bound inference memory.
            pieces = re.findall(r'.{1,220}(?:[.!?;]\s+|$)|.{1,220}\s+|.{1,220}',text)
            with torch.inference_mode():
                for piece in pieces:
                    data = self.tts.apply_tts(text=piece.strip(),speaker=self.speaker,sample_rate=48000,
                                             put_accent=True,put_yo=True).numpy()
                    arrays.append(data)
                    samples_used += len(data)
                    if samples_used >= 48000 * max_seconds * speed:
                        break
            if not arrays:
                raise CallUnavailable('Синтез голоса вернул пустую запись')
            pcm = (np.clip(np.concatenate(arrays)*volume,-1,1)*32767).astype('<i2').tobytes()
            if speed != 1:
                # Change tempo while preserving the natural pitch of the voice.
                result = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','s16le',
                    '-ar','48000','-ac','1','-i','pipe:0','-af',f'atempo={speed}',
                    '-f','s16le','pipe:1'],input=pcm,capture_output=True,timeout=30)
                if result.returncode:
                    raise CallUnavailable('Не удалось изменить скорость голоса')
                pcm = result.stdout
            return pcm[:48000*2*max_seconds]

    async def synthesize(self, text, speed=1, volume=1, max_seconds=60):
        text = speech_text(text)
        if not text:
            raise CallUnavailable('Нет текста для озвучивания')
        return await asyncio.to_thread(self._synthesize, text, speed, volume, min(180, max_seconds))


def speech_text(text):
    text = re.sub(r'https?://\S+|[*_`#]', '', text)
    normal = {'прывет':'привет','харашо':'хорошо','харошо':'хорошо','щас':'сейчас',
              'ща':'сейчас','чо':'что','че':'что','пж':'пожалуйста','спс':'спасибо','ку':'привет'}
    text = re.sub(r'\b[а-яё]+\b',lambda m:normal.get(m[0].lower(),m[0]),text,flags=re.I)
    return re.sub(r'[^\w\s.,!?;:()\-«»—…]', '', text).strip()[:1000]


@dataclass
class Utterance:
    """Bounded per-speaker VAD with a small pre-roll; input is 16 kHz mono PCM."""
    chunks: list[bytes] = field(default_factory=list)
    preroll: deque = field(default_factory=lambda: deque(maxlen=8))
    duration: float = 0
    speech: float = 0
    silence: float = 0
    last_frame: float = 0

    def feed(self, pcm, now, settings):
        import numpy as np
        if not pcm or len(pcm) % 2 or len(pcm) > 32000 * 2:
            return None
        data = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        seconds = len(data) / 16000
        loud = float(np.sqrt(np.mean(data * data))) >= settings['call_vad_threshold']
        self.last_frame = now
        if not self.chunks and not loud:
            self.preroll.append(pcm)
            return None
        if not self.chunks:
            self.chunks.extend(self.preroll)
            self.duration = sum(map(len, self.preroll)) / 32000
            self.preroll.clear()
        self.chunks.append(pcm)
        self.duration += seconds
        self.speech += seconds if loud else 0
        self.silence = 0 if loud else self.silence + seconds
        if self.silence >= settings['call_silence_seconds'] or self.duration >= settings['call_utterance_seconds']:
            return self.finish(settings)
        return None

    def finish(self, settings):
        pcm = b''.join(self.chunks) if self.speech >= settings['call_min_speech_seconds'] else None
        self.chunks.clear()
        self.preroll.clear()
        self.duration = self.speech = self.silence = 0
        return pcm
