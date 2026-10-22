from __future__ import annotations

import asyncio
from importlib.util import find_spec
import logging
import time
from typing import Any

try:
    from .. import config
except ImportError:
    import config


logger = logging.getLogger(__name__)

_gemini_models: dict[str, Any] = {}
_gemini_configured = False


class LocalLLM:
    """
    Thin provider wrapper for the unified LLM layer.

    Provider order:
    1. MLX/Qwen when available locally
    2. Gemini when configured in the existing repo
    3. caller fallback heuristics
    """

    MODEL_MAP = {
        "fast": "Qwen/Qwen3-0.6B",
        "standard": "Qwen/Qwen3-1.7B-Instruct",
        "quality": "Qwen/Qwen3-4B-Instruct",
    }

    def __init__(self, tier: str = "standard"):
        self.tier = tier if tier in self.MODEL_MAP else "standard"
        self._mlx_model: Any | None = None
        self._mlx_tokenizer: Any | None = None

    @staticmethod
    def _has_mlx() -> bool:
        return find_spec("mlx_lm") is not None

    @staticmethod
    def _has_gemini() -> bool:
        return bool(config.GEMINI_API_KEY and getattr(config, "HAS_GEMINI_SDK", False))

    def preferred_provider(self) -> str | None:
        if self._has_mlx():
            return "mlx"
        if self._has_gemini():
            return "gemini"
        return None

    def status(self) -> dict[str, Any]:
        provider = self.preferred_provider()
        return {
            "available": provider is not None,
            "provider": provider or "heuristic",
            "tier": self.tier,
            "mlx_available": self._has_mlx(),
            "gemini_available": self._has_gemini(),
            "model_name": self.MODEL_MAP.get(self.tier, self.MODEL_MAP["standard"])
            if provider == "mlx"
            else config.ENRICHMENT_MODEL
            if provider == "gemini"
            else None,
        }

    def _load_mlx(self) -> None:
        if self._mlx_model is not None and self._mlx_tokenizer is not None:
            return
        from mlx_lm import load

        self._mlx_model, self._mlx_tokenizer = load(self.MODEL_MAP[self.tier])

    def _generate_with_mlx(self, prompt: str, max_tokens: int, temperature: float) -> str:
        self._load_mlx()
        from mlx_lm import generate

        return str(
            generate(
                self._mlx_model,
                self._mlx_tokenizer,
                prompt=prompt,
                max_tokens=max_tokens,
                temp=temperature,
                verbose=False,
            )
        ).strip()

    def _get_gemini_model(self) -> Any:
        global _gemini_configured

        import google.generativeai as genai

        if not _gemini_configured:
            genai.configure(api_key=config.GEMINI_API_KEY)
            _gemini_configured = True

        cache_key = f"{self.tier}:{config.ENRICHMENT_MODEL}"
        model = _gemini_models.get(cache_key)
        if model is None:
            model = genai.GenerativeModel(config.ENRICHMENT_MODEL)
            _gemini_models[cache_key] = model
        return model

    def _generate_with_gemini(self, prompt: str, max_tokens: int, temperature: float) -> str:
        model = self._get_gemini_model()

        # Gemini 2.5 Flash has a built-in "thinking" pass that consumes tokens
        # from max_output_tokens before writing the visible response.
        # We give a large budget (caller's max_tokens × 3, min 4096) so thinking
        # never starves the actual answer.
        output_budget = max(max_tokens * 3, 4096)

        last_exc: Exception | None = None
        for attempt in range(3):  # initial + 2 retries
            try:
                response = model.generate_content(
                    prompt,
                    generation_config={
                        "temperature": temperature,
                        "max_output_tokens": output_budget,
                    },
                )

                # Safely extract text — .text raises ValueError if content was blocked.
                try:
                    text = (response.text or "").strip()
                except (ValueError, AttributeError):
                    # Blocked or partial — try to salvage from raw parts
                    candidates = getattr(response, "candidates", [])
                    if candidates:
                        parts = getattr(getattr(candidates[0], "content", None), "parts", [])
                        text = " ".join(
                            getattr(p, "text", "") for p in parts if getattr(p, "text", "")
                        ).strip()
                    else:
                        text = ""

                if not text:
                    raise ValueError("Gemini returned an empty response")
                return text
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    wait = 0.5 * (3 ** attempt)  # 0.5s, 1.5s
                    logger.warning(
                        "Gemini attempt %d failed (%s), retrying in %.1fs",
                        attempt + 1, exc, wait,
                    )
                    time.sleep(wait)
        raise RuntimeError(f"Gemini failed after 3 attempts: {last_exc}") from last_exc

    def _sync_generate(self, prompt: str, max_tokens: int, temperature: float) -> str:
        provider = self.preferred_provider()
        if provider == "mlx":
            try:
                return self._generate_with_mlx(prompt, max_tokens, temperature)
            except Exception as exc:
                logger.warning("MLX generation failed, falling back: %s", exc)
                if not self._has_gemini():
                    raise

        if self._has_gemini():
            return self._generate_with_gemini(prompt, max_tokens, temperature)

        raise RuntimeError("No LLM provider is configured")

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._sync_generate,
            prompt,
            max_tokens,
            temperature,
        )
