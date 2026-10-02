"""Local model wrappers (Apple Silicon, MLX). Models download from Hugging Face on first use.

Model choice (highest priority first):
  --model <hf id> / REEL_WATCHER_VLM   exact vision-language model repo
  --model-size small|large / REEL_WATCHER_MODEL
  auto: small if total RAM < 32 GB, else large
REEL_WATCHER_WHISPER overrides the whisper repo.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

SIZES = {
    "large": ("mlx-community/Qwen3.6-35B-A3B-4bit", "mlx-community/whisper-large-v3-turbo"),
    "small": ("mlx-community/Qwen2.5-VL-7B-Instruct-4bit", "mlx-community/whisper-small-mlx"),
}
SMALL_RAM_LIMIT = 32 * 1024**3  # below this, auto picks "small"


def total_ram_bytes() -> int:
    try:
        return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
    except Exception:
        return 0


def select_models(size=None, model=None, env=None, ram_bytes=None):
    """Return (vlm_repo, whisper_repo, size_used). No downloads, no model loading."""
    env = os.environ if env is None else env
    size = size or env.get("REEL_WATCHER_MODEL") or "auto"
    if size == "auto":
        ram = total_ram_bytes() if ram_bytes is None else ram_bytes
        size = "small" if ram < SMALL_RAM_LIMIT else "large"
    if size not in SIZES:
        raise ValueError(f"model size must be small, large or auto, got {size!r}")
    vlm, whisper = SIZES[size]
    vlm = model or env.get("REEL_WATCHER_VLM") or vlm
    whisper = env.get("REEL_WATCHER_WHISPER") or whisper
    return vlm, whisper, size


VISION_MODEL, WHISPER_MODEL, _ = select_models()


def configure(size=None, model=None):
    """Set the module-wide model choice (called once from the CLI before models load)."""
    global VISION_MODEL, WHISPER_MODEL
    VISION_MODEL, WHISPER_MODEL, used = select_models(size, model)
    return used

RETRY_HINT = (
    "\n\nIMPORTANT: output must be short, valid JSON. Keep every text field under 200 characters: "
    "summarize visible text, never copy long passages, code, numbers or grids."
)


def parse_json(text: str):
    """Extract the outermost JSON object from model output, or None."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None


class Vision:
    """Loads the VLM once; `ask(prompt, image_path)` returns parsed JSON (or a parse_error dict)."""

    def __init__(self):
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        self.model, self.processor = load(VISION_MODEL)
        self.config = load_config(VISION_MODEL)

    def ask(self, prompt, image=None, max_tokens=500):
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        images = [image] if image else []
        # Second attempt uses a stricter prompt: busy screens can make the model transcribe text until it runs out of tokens.
        attempts = [(prompt, max_tokens, None), (prompt + RETRY_HINT, max_tokens * 2, 1.15)]
        out = ""
        for text, limit, penalty in attempts:
            formatted = apply_chat_template(
                self.processor, self.config, text, num_images=len(images), enable_thinking=False
            )
            kwargs = {"repetition_penalty": penalty} if penalty else {}
            out = generate(
                self.model, self.processor, formatted, image=images or None,
                max_tokens=limit, temperature=0.0, verbose=False, **kwargs,
            )
            parsed = parse_json(getattr(out, "text", out))
            if parsed is not None:
                return parsed
        return {"parse_error": True, "raw": str(getattr(out, "text", out))[:1000]}
