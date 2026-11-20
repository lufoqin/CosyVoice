import io
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4

import numpy as np
import soundfile as sf

from cosyvoice.service.tables import cell_text, column, open_excel

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "pretrained_models" / "Fun-CosyVoice3-0.5B-2512"
PROMPT_DIR = ROOT / "static" / "audio_prompt"
RL_CHECKPOINT = "llm.rl.pt"
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"
LANGUAGES = {"zh": "zh", "en": "en", "中文": "zh", "英文": "en"}


def validate_prompt(audio):
    try:
        info = sf.info(audio)
    except (RuntimeError, ValueError) as exc:
        raise ValueError("无法读取参考音频，请上传可解码的 WAV、FLAC 或 MP3") from exc
    if info.samplerate < 16000 or not 0 < info.duration <= 30:
        raise ValueError("参考音频须至少为 16 kHz，时长须大于 0 秒且不超过 30 秒")


def stereo_wav(chunks, sample_rate):
    if not chunks:
        raise RuntimeError("模型没有返回音频")
    waveform = np.concatenate(chunks, axis=1)
    if waveform.shape[0] == 1:
        waveform = np.repeat(waveform, 2, axis=0)
    output = io.BytesIO()
    sf.write(output, waveform.T, sample_rate, format="WAV", subtype="PCM_16")
    return output.getvalue()


class Synthesis:
    def __init__(self, model_dir=None, prompt_dir=PROMPT_DIR):
        model_dir = Path(model_dir or os.environ.get("COSYVOICE_MODEL_DIR") or MODEL_DIR).resolve()
        checkpoint = model_dir / RL_CHECKPOINT
        if not checkpoint.is_file():
            raise FileNotFoundError(f"缺少 RL 权重：{checkpoint}")

        import torch

        require_cuda = os.environ.get("COSYVOICE_REQUIRE_CUDA") == "1"
        if require_cuda and not torch.cuda.is_available():
            raise RuntimeError("PyTorch 未检测到 CUDA")

        sys.path.insert(0, str(ROOT / "third_party" / "Matcha-TTS"))
        from cosyvoice.cli.cosyvoice import CosyVoice3

        self.model = CosyVoice3(str(model_dir), llm_checkpoint=RL_CHECKPOINT)
        providers = self.model.frontend.speech_tokenizer_session.get_providers()
        if require_cuda and "CUDAExecutionProvider" not in providers:
            raise RuntimeError("ONNX 音源编码器未启用 CUDAExecutionProvider")
        logging.info("Loaded RL checkpoint %s on %s (ONNX providers: %s)",
                     checkpoint, self.model.model.device, providers)
        self.lock = Lock()
        self.voices = {}
        self.load_voices(Path(prompt_dir).resolve())

    def load_voices(self, root):
        catalog = root / "voices.xlsx"
        if not catalog.is_file():
            logging.info("Local voice catalog not found: %s", catalog)
            return
        with open_excel(catalog) as workbook:
            rows = workbook.worksheets[0].values
            headers = [cell_text(value).strip() for value in next(rows, ())]
            indices = [column(headers, name, catalog.name) for name in ("文本", "路径", "语言")]
            for row_number, row in enumerate(rows, start=2):
                transcript, filename, language = (
                    cell_text(row[index]).strip() if index < len(row) else "" for index in indices
                )
                if not any((transcript, filename, language)):
                    continue
                try:
                    relative_path = Path(filename)
                    path = (root / relative_path).resolve()
                    if relative_path.is_absolute() or not path.is_relative_to(root):
                        raise ValueError("音源路径须为 static/audio_prompt 内的相对路径")
                    if not transcript or not path.is_file() or language.lower() not in LANGUAGES:
                        raise ValueError("请填写参考文本、有效的音频路径及中文或英文")
                    voice_id = path.relative_to(root).as_posix()
                    if voice_id in self.voices:
                        raise ValueError(f"音源路径重复：{voice_id}")
                    validate_prompt(path)
                    self.register_voice(transcript, path, f"local:{voice_id}")
                    self.voices[voice_id] = LANGUAGES[language.lower()]
                except (ValueError, OSError) as exc:
                    raise ValueError(f"{catalog.name} 第 {row_number} 行：{exc}") from exc

    def local_voices(self, language, voice_id=None):
        if voice_id:
            if voice_id not in self.voices:
                raise ValueError(f"找不到本地音源：{voice_id}")
            return [f"local:{voice_id}"]
        candidates = [f"local:{name}" for name, lang in self.voices.items() if lang == language]
        if not candidates:
            raise ValueError(f"没有 {language} 本地音源，请配置音源表或上传参考音频和对应文本")
        return candidates

    def register_voice(self, transcript, audio, key):
        self.model.add_zero_shot_spk(PROMPT_PREFIX + transcript, str(audio), key)

    @contextmanager
    def uploaded_voice(self, transcript, audio):
        key = f"upload:{uuid4()}"
        with self.lock:
            self.register_voice(transcript, audio, key)
        try:
            yield key
        finally:
            with self.lock:
                self.model.frontend.spk2info.pop(key, None)

    def wav(self, text, voice_key):
        with self.lock:
            outputs = self.model.inference_zero_shot(text.strip(), "", "", zero_shot_spk_id=voice_key)
            chunks = [output["tts_speech"].detach().cpu().numpy() for output in outputs]
        return stereo_wav(chunks, self.model.sample_rate)
