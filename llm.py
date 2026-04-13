"""Local LLM integration via Ollama.

Model recommendations by hardware:
  - NVIDIA GPU (CUDA) or Apple Silicon (MPS): qwen3.5:9b  (~5 GB)
  - CPU only:                                 qwen2.5:3b  (~2 GB, runs on 8 GB RAM)

Uses ``POST /api/chat`` first (current Ollama default), then ``POST /api/generate`` if the
server returns 404 (some setups / versions). Override base URL with env ``OLLAMA_BASE`` or
``OLLAMA_HOST`` (see `.env.example`).
"""

import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3.5:9b"
CPU_MODEL = "qwen2.5:3b"


def ollama_base() -> str:
    """Base URL for Ollama HTTP API (no trailing slash)."""
    explicit = (os.environ.get("OLLAMA_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = (os.environ.get("OLLAMA_HOST") or "").strip()
    if host:
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        return f"http://{host}".rstrip("/")
    return "http://127.0.0.1:11434"


# Backwards compatibility for imports of OLLAMA_BASE
OLLAMA_BASE = ollama_base()


def detect_hardware() -> str:
    """Return 'cuda', 'mps', or 'cpu'."""
    # Try torch first (most reliable)
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except ImportError:
        pass
    # Fallback: nvidia-smi
    try:
        subprocess.run(["nvidia-smi"], capture_output=True, check=True)
        return "cuda"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Apple Silicon
    if platform.machine() in ("arm64", "aarch64") and platform.system() == "Darwin":
        return "mps"
    return "cpu"


def recommend_model() -> str:
    """Return the best Ollama model for the current hardware."""
    hw = detect_hardware()
    if hw in ("cuda", "mps"):
        logger.info(f"Hardware detected: {hw} — using {DEFAULT_MODEL}")
        return DEFAULT_MODEL
    else:
        logger.info(f"Hardware detected: cpu — using lighter model {CPU_MODEL}")
        return CPU_MODEL


def check_ollama_available() -> bool:
    """Check that Ollama (or compatible) exposes at least one generation endpoint."""
    base = ollama_base()
    try:
        v = requests.get(f"{base}/api/version", timeout=5)
        if v.status_code == 200:
            return True
        t = requests.get(f"{base}/api/tags", timeout=5)
        if t.status_code != 200:
            return False
        # Something answers /api/tags — ensure a generation route exists (not a stub)
        probes = (
            (
                "/api/chat",
                {
                    "model": "__probe__",
                    "messages": [{"role": "user", "content": "."}],
                    "stream": False,
                },
            ),
            ("/api/generate", {"model": "__probe__", "prompt": ".", "stream": False}),
            (
                "/v1/chat/completions",
                {
                    "model": "__probe__",
                    "messages": [{"role": "user", "content": "."}],
                    "stream": False,
                },
            ),
        )
        for path, payload in probes:
            p = requests.post(f"{base}{path}", json=payload, timeout=8)
            if p.status_code != 404:
                return True
        return False
    except requests.ConnectionError:
        return False
    except requests.RequestException:
        return False


def list_models() -> list:
    """List available Ollama models."""
    base = ollama_base()
    try:
        r = requests.get(f"{base}/api/tags", timeout=5)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


def _parse_ollama_body(data: dict) -> str:
    """Normalize chat vs generate JSON response."""
    if not data:
        return ""
    if "message" in data:
        msg = data.get("message") or {}
        return (msg.get("content") or "").strip()
    return (data.get("response") or "").strip()


def _parse_openai_compat(data: dict) -> str:
    """OpenAI-style /v1/chat/completions response."""
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()
    except (IndexError, TypeError, AttributeError):
        return ""


def generate(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: int = 300,
) -> str:
    """Generate text using Ollama.

    Args:
        prompt: The user prompt.
        system: System prompt for context/instructions.
        model: Ollama model name.
        temperature: Sampling temperature (lower = more deterministic).
        max_tokens: Maximum tokens to generate.
        timeout: Request timeout in seconds.

    Returns:
        Generated text string.
    """
    base = ollama_base()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    chat_payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    gen_payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if system:
        gen_payload["system"] = system

    logger.info("LLM generate: model=%s, prompt_len=%d, base=%s", model, len(prompt), base)
    start = time.time()

    oa_messages = []
    if system:
        oa_messages.append({"role": "system", "content": system})
    oa_messages.append({"role": "user", "content": prompt})
    openai_payload = {
        "model": model,
        "messages": oa_messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        r = requests.post(f"{base}/api/chat", json=chat_payload, timeout=timeout)
        if r.status_code == 404:
            logger.info("Ollama /api/chat returned 404; trying /api/generate")
            r = requests.post(f"{base}/api/generate", json=gen_payload, timeout=timeout)
        if r.status_code == 404:
            logger.info("Ollama /api/generate returned 404; trying OpenAI-compatible /v1/chat/completions")
            r = requests.post(
                f"{base}/v1/chat/completions",
                json=openai_payload,
                timeout=timeout,
            )
        if r.status_code == 404:
            raise RuntimeError(
                "No Ollama LLM routes on this URL (/api/chat, /api/generate, /v1/chat/completions "
                "all returned 404). Another process may be bound to this port, or it is not Ollama. "
                "Run: lsof -iTCP:11434 -sTCP:LISTEN  then stop the impostor or set OLLAMA_BASE to "
                "your real Ollama. If Ollama is correct: ollama pull <model> (your profile "
                "pipeline.ollama_model)."
            )
        r.raise_for_status()
        result = r.json()
        if "choices" in result:
            text = _parse_openai_compat(result)
        else:
            text = _parse_ollama_body(result)
        elapsed = time.time() - start
        logger.info("LLM response: %d chars in %.1fs", len(text), elapsed)
        return text.strip()
    except requests.Timeout:
        logger.error("LLM request timed out after %ds", timeout)
        raise
    except requests.HTTPError as e:
        logger.error(
            "LLM HTTP error: %s. Ollama base: %s — run `ollama serve` and `ollama pull %s`. "
            "If Ollama is not on localhost, set OLLAMA_BASE (or OLLAMA_HOST) in .env.",
            e,
            base,
            model,
        )
        raise
    except requests.RequestException as e:
        logger.error("LLM request failed: %s", e)
        raise


def generate_structured(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Generate structured JSON output from LLM.

    The prompt should instruct the model to return valid JSON.
    Attempts to parse the response as JSON, with retry on failure.
    """
    json_system = system + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation, no code fences."

    for attempt in range(3):
        text = generate(
            prompt=prompt,
            system=json_system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

        # Strip markdown code fences if present
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first and last lines (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse failed (attempt %d): %s", attempt + 1, e)
            if attempt < 2:
                prompt = (
                    f"Your previous response was not valid JSON. Error: {e}\n"
                    f"Please try again. Return ONLY valid JSON.\n\n"
                    f"Original request:\n{prompt}"
                )

    logger.error("Failed to get valid JSON after 3 attempts")
    return {}


def generate_latex(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    timeout: int = 300,
) -> str:
    """Generate LaTeX content, stripping any markdown fences."""
    text = generate(
        prompt=prompt,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )

    # Strip markdown code fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```latex or ```) and last line (```)
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    return cleaned.strip()
