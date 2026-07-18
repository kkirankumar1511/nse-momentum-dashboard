"""
LLM provider abstraction — open-source models first, paid APIs optional.

Supported providers (set LLM_PROVIDER in .env):

  ollama      Local models on your own machine. FREE, PRIVATE, no rate limits.
              Your filings never leave your computer. Best default.
              Setup: https://ollama.com  ->  ollama pull qwen2.5:14b-instruct

  llamacpp    Local llama.cpp server (OpenAI-compatible /v1). FREE, PRIVATE.
  vllm        Local/self-hosted vLLM server. FREE, fast batching, needs a GPU.

  groq        Hosted open-weights (Llama 3.3 70B, Qwen). Generous free tier,
              very fast. Data leaves your machine. https://console.groq.com

  openrouter  Router with genuinely free open models (look for ":free" ids).
              https://openrouter.ai

  together    Hosted open models, cheap paid. https://together.ai
  hf          HuggingFace Inference API.
  openai_compat  Any other OpenAI-compatible endpoint (set LLM_BASE_URL).
  anthropic   Paid fallback, kept only for comparison.

=== WHY OPEN MODELS ARE FINE FOR THIS JOB ===

The deterministic layer (nse_api red flags, xbrl_parser earnings quality)
already owns every safety-critical check. The LLM only does judgement and
synthesis, and it is contractually forbidden from overruling a hard flag. So
a weaker model degrades nuance, not safety. That's what makes a 14B model on
your laptop a reasonable choice here where it wouldn't be for, say, medical
triage.

=== WHAT ACTUALLY MATTERS WHEN PICKING A MODEL ===

1. CONTEXT LENGTH. This is the binding constraint, not intelligence. A full
   evidence prompt (12 quarters of XBRL + announcements + two annual reports'
   worth of extracted sections) runs 20k-40k tokens. Models with 8k context
   (Gemma 2, older Llama) CANNOT do this in one pass — this module falls back
   to map-reduce chunking for them, which is slower and loses cross-section
   reasoning. Prefer 32k+ context: Qwen2.5 (128k), Llama 3.1/3.3 (128k),
   Mistral Nemo (128k).

2. JSON RELIABILITY. Small models break JSON constantly. This module enforces
   it three ways: native format=json where supported, a repair pass, and
   schema validation with retries. Never trust raw output.

3. SIZE vs HARDWARE. Rough guide for local:
     qwen2.5:7b-instruct   ~5GB RAM   usable, shallow reasoning
     qwen2.5:14b-instruct  ~9GB RAM   good balance — RECOMMENDED DEFAULT
     qwen2.5:32b-instruct  ~20GB RAM  noticeably better on financial nuance
     llama3.3:70b          ~40GB RAM  best local, needs serious hardware
   On 16GB RAM use 14b. On 8GB use 7b and expect thinner analysis.
"""

from __future__ import annotations

import json
import os
import re
import time

import requests

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
MODEL = os.getenv("LLM_MODEL", "")
BASE_URL = os.getenv("LLM_BASE_URL", "")
API_KEY = os.getenv("LLM_API_KEY", "")
TIMEOUT = int(os.getenv("LLM_TIMEOUT", "300"))

# Hosted free tiers sometimes report a genuine wait of many minutes when a
# DAILY quota (not just per-minute) is exhausted. Sleeping through that would
# freeze an interactive dashboard for no benefit — better to fail fast with a
# clear reason than hang. Retries for waits under this cap still happen
# automatically.
MAX_RATE_LIMIT_WAIT = float(os.getenv("LLM_MAX_RATE_LIMIT_WAIT", "30"))

DEFAULTS = {
    # provider:     (default model,                     base url,                                   ctx)
    "ollama":       ("qwen2.5:14b-instruct",            "http://localhost:11434",                   32768),
    "llamacpp":     ("local-model",                     "http://localhost:8080/v1",                 32768),
    "vllm":         ("Qwen/Qwen2.5-14B-Instruct",       "http://localhost:8000/v1",                 32768),
    # ctx capped well below the model's real 128k window: Groq's free tier
    # throttles by tokens-PER-MINUTE (~6k-12k TPM for llama-3.3-70b-versatile),
    # not by context length, and rejects oversized requests with a 413 rather
    # than truncating. Keeping this low forces map_reduce_json's chunking path
    # for anything but small requests. Raise via LLM_CONTEXT if on a paid tier.
    "groq":         ("llama-3.3-70b-versatile",         "https://api.groq.com/openai/v1",           6000),
    "openrouter":   ("qwen/qwen-2.5-72b-instruct:free", "https://openrouter.ai/api/v1",             32768),
    "together":     ("Qwen/Qwen2.5-72B-Instruct-Turbo", "https://api.together.xyz/v1",              32768),
    "hf":           ("Qwen/Qwen2.5-72B-Instruct",       "https://api-inference.huggingface.co/v1",  16384),
    "openai_compat": ("local-model",                    "http://localhost:8000/v1",                 32768),
    "anthropic":    ("claude-sonnet-4-6",               "",                                         200000),
}


def _cfg():
    model, url, ctx = DEFAULTS.get(PROVIDER, DEFAULTS["ollama"])
    return (MODEL or model, BASE_URL or url, ctx)


def context_limit() -> int:
    """Approximate usable context in tokens (leaves room for the response)."""
    try:
        return int(os.getenv("LLM_CONTEXT", "0")) or _cfg()[2]
    except ValueError:
        return _cfg()[2]


def describe() -> str:
    model, url, ctx = _cfg()
    where = "local (private, free)" if any(
        h in url for h in ("localhost", "127.0.0.1")) else "hosted"
    return f"{PROVIDER}:{model} [{where}, ~{ctx // 1000}k ctx]"


def is_available() -> tuple[bool, str]:
    """Check the configured provider is reachable. Returns (ok, message)."""
    model, url, _ = _cfg()
    if PROVIDER == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY", "")
        return (bool(key), "ANTHROPIC_API_KEY set" if key else "no ANTHROPIC_API_KEY")
    if PROVIDER == "ollama":
        try:
            r = requests.get(f"{url}/api/tags", timeout=5)
            tags = [m["name"] for m in r.json().get("models", [])]
            if not tags:
                return (False, "Ollama running but no models. Run: "
                               f"ollama pull {model}")
            hit = any(t.split(":")[0] == model.split(":")[0] for t in tags)
            if not hit:
                return (False, f"Model '{model}' not pulled. Run: "
                               f"ollama pull {model}\nAvailable: {', '.join(tags)}")
            return (True, f"Ollama ready with {model}")
        except requests.RequestException:
            return (False, "Ollama not reachable at "
                           f"{url}. Install from https://ollama.com then run "
                           f"`ollama pull {model}`")
    # hosted OpenAI-compatible
    if not API_KEY and PROVIDER in ("groq", "openrouter", "together", "hf"):
        return (False, f"Set LLM_API_KEY for {PROVIDER}")
    return (True, f"{PROVIDER} configured")


# ---------------------------------------------------------------------------
# JSON hardening — small models break JSON constantly
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = re.sub(r"```(?:json)?\s*", "", text)
    return text.replace("```", "").strip()


def _brace_groups(text: str) -> list[str]:
    """Every balanced top-level {...} group, in order.

    Models emit prose containing braces ("use {placeholder} here") before the
    real object, so we can't just take the first '{' — we collect all
    candidates and let the caller pick the one that actually parses.
    """
    groups, depth, start, in_str, esc = [], 0, None, False, False
    for i, c in enumerate(text):
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(text[start:i + 1])
                start = None
    return groups


def _extract_json(text: str) -> str | None:
    """First balanced brace group that is (or repairs into) valid JSON.
    Prefers the largest, since the real payload is usually the biggest object."""
    text = _strip_fences(text)
    candidates = _brace_groups(text)
    if not candidates:
        return None
    valid = []
    for g in candidates:
        for attempt in (g, _repair_json(g)):
            try:
                if isinstance(json.loads(attempt), dict):
                    valid.append(g)
                    break
            except json.JSONDecodeError:
                continue
    return max(valid, key=len) if valid else max(candidates, key=len)


def _repair_json(s: str) -> str:
    """Fix the mistakes small models actually make."""
    s = re.sub(r",\s*([}\]])", r"\1", s)          # trailing commas
    s = re.sub(r"//[^\n]*", "", s)                 # // comments
    s = re.sub(r"([{,]\s*)'([^']+)'(\s*:)", r'\1"\2"\3', s)   # 'key':
    s = re.sub(r"(:\s*)'([^']*)'(\s*[,}])", r'\1"\2"\3', s)   # : 'value'
    s = s.replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\bNone\b", "null", s)
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    return s


def parse_json_response(text: str) -> dict | None:
    raw = _extract_json(text)
    if not raw:
        return None
    for candidate in (raw, _repair_json(raw)):
        try:
            out = json.loads(candidate)
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def coerce_schema(obj: dict, schema: dict) -> dict:
    """Fill missing keys with defaults and clamp enums. Small models omit
    fields and invent enum values; we normalize rather than reject."""
    out = {}
    for key, spec in schema.items():
        val = obj.get(key)
        kind = spec.get("type", "str")
        if kind == "enum":
            allowed = spec["values"]
            if isinstance(val, str):
                match = next((a for a in allowed
                              if a.lower() == val.strip().lower()), None)
                if match is None:
                    match = next((a for a in allowed
                                  if a.lower() in val.strip().lower()), None)
                val = match
            out[key] = val if val in allowed else spec.get("default", allowed[-1])
        elif kind == "list":
            if isinstance(val, str):
                val = [val]
            out[key] = [str(x) for x in val] if isinstance(val, list) else []
        else:
            out[key] = str(val) if val is not None else spec.get("default", "")
    return out


# ---------------------------------------------------------------------------
# Chat calls
# ---------------------------------------------------------------------------

def _chat_ollama(system: str, user: str, model: str, url: str,
                 json_mode: bool, max_tokens: int) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": max_tokens,
                    "num_ctx": context_limit()},
    }
    if json_mode:
        payload["format"] = "json"       # Ollama's native JSON grammar
    r = requests.post(f"{url}/api/chat", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]["content"]


class RateLimitError(Exception):
    """Provider returned 429. Carries how long the provider asked us to wait,
    since hosted free tiers (e.g. Groq) throttle per-minute and a short fixed
    backoff just re-hits the same window."""
    def __init__(self, retry_after: float, message: str):
        super().__init__(message)
        self.retry_after = retry_after


def _chat_openai_compat(system: str, user: str, model: str, url: str,
                        json_mode: bool, max_tokens: int) -> str:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if PROVIDER == "openrouter":
        headers["HTTP-Referer"] = "https://localhost"
        headers["X-Title"] = "NSE Momentum Dashboard"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(f"{url}/chat/completions", headers=headers,
                      json=payload, timeout=TIMEOUT)
    if r.status_code == 400 and json_mode:
        # some endpoints reject response_format — retry without it
        payload.pop("response_format")
        r = requests.post(f"{url}/chat/completions", headers=headers,
                          json=payload, timeout=TIMEOUT)
    if r.status_code == 429:
        retry_after = float(r.headers.get("Retry-After", 10))
        raise RateLimitError(retry_after,
                             f"429 rate limited by {PROVIDER}, "
                             f"retry after {retry_after}s")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _chat_anthropic(system: str, user: str, model: str,
                    max_tokens: int) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(model=model, max_tokens=max_tokens,
                                 system=system,
                                 messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in msg.content if b.type == "text")


def chat(system: str, user: str, json_mode: bool = True,
         max_tokens: int = 2000, retries: int = 2) -> str:
    """Single chat completion against the configured provider."""
    model, url, _ = _cfg()
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        print(f"[llm] attempt {attempt + 1}/{retries + 1}: "
              f"{PROVIDER}:{model} request sent...", flush=True)
        try:
            if PROVIDER == "ollama":
                result = _chat_ollama(system, user, model, url, json_mode, max_tokens)
            elif PROVIDER == "anthropic":
                result = _chat_anthropic(system, user, model, max_tokens)
            else:
                result = _chat_openai_compat(system, user, model, url, json_mode,
                                             max_tokens)
            print(f"[llm] attempt {attempt + 1}: response received in "
                  f"{time.time() - t0:.1f}s", flush=True)
            return result
        except Exception as e:
            last_err = e
            print(f"[llm] attempt {attempt + 1} failed after "
                  f"{time.time() - t0:.1f}s: {e}", flush=True)
            if isinstance(e, RateLimitError) and e.retry_after > MAX_RATE_LIMIT_WAIT:
                print(f"[llm] rate limit wants {e.retry_after:.0f}s, exceeds "
                      f"{MAX_RATE_LIMIT_WAIT:.0f}s cap — giving up rather than "
                      f"hang, likely a daily quota not a per-minute one",
                      flush=True)
                break
            if attempt < retries:
                wait = e.retry_after if isinstance(e, RateLimitError) else 2 ** attempt
                print(f"[llm] waiting {wait:.1f}s before retrying...", flush=True)
                time.sleep(wait)
    err = RuntimeError(f"LLM call failed after {retries + 1} attempts: {last_err}")
    err.retry_after = getattr(last_err, "retry_after", None)
    raise err


def chat_json(system: str, user: str, schema: dict | None = None,
              max_tokens: int = 2000, retries: int = 2) -> dict:
    """Chat that MUST return a dict. Retries with escalating strictness.

    Small open models fail JSON often enough that this matters more than the
    model choice. Strategy: native json mode -> brace extraction -> repair ->
    schema coercion -> retry with a blunter instruction.
    """
    strict_suffix = (
        "\n\nCRITICAL: Output ONLY a single valid JSON object. "
        "No markdown, no code fences, no explanation before or after. "
        "Start your response with { and end with }."
    )
    for attempt in range(retries + 1):
        prompt = user if attempt == 0 else user + strict_suffix
        try:
            raw = chat(system, prompt, json_mode=True, max_tokens=max_tokens,
                       retries=0)
        except Exception as e:
            wait = getattr(e, "retry_after", None)
            if attempt == retries or (wait and wait > MAX_RATE_LIMIT_WAIT):
                raise
            if wait:
                print(f"[llm] chat_json round {attempt + 1}/{retries + 1}: "
                      f"rate limited, waiting {wait:.1f}s...", flush=True)
                time.sleep(wait)
            continue
        parsed = parse_json_response(raw)
        if parsed:
            return coerce_schema(parsed, schema) if schema else parsed
        print(f"[llm] chat_json round {attempt + 1}/{retries + 1}: "
              f"response was not parseable JSON, retrying with stricter prompt",
              flush=True)
    raise ValueError("Model did not return parseable JSON after retries. "
                     "Try a larger model (e.g. qwen2.5:32b-instruct).")


# ---------------------------------------------------------------------------
# Long-context handling: map-reduce for small-context models
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """~4 chars/token for English; financial text with lots of numbers runs
    denser, so this is deliberately conservative."""
    return len(text) // 3


def fits_context(system: str, user: str, max_tokens: int = 2000) -> bool:
    budget = context_limit() - max_tokens - 500
    return estimate_tokens(system) + estimate_tokens(user) < budget


def chunk_text(text: str, chunk_tokens: int) -> list[str]:
    """Split on section boundaries where possible, else hard-split."""
    max_chars = chunk_tokens * 3
    if len(text) <= max_chars:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) + 2 > max_chars and buf:
            parts.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        parts.append(buf)
    out = []
    for p in parts:
        while len(p) > max_chars:
            out.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            out.append(p)
    return out


MAP_SYSTEM = (
    "You are summarizing one slice of a company's filings for a financial "
    "analyst. Extract ONLY facts material to whether earnings are real and "
    "durable: numbers, auditor language, related-party items, contingent "
    "liabilities, risks, catalysts. Quote specific figures and phrases. "
    "Do not speculate. Do not add narrative. If this slice contains nothing "
    "material, reply exactly: NOTHING MATERIAL."
)


def map_reduce_json(system: str, evidence: str, question: str,
                    schema: dict | None = None,
                    max_tokens: int = 2000, _depth: int = 0) -> dict:
    """For models whose context can't hold the full evidence: summarize each
    chunk, then reason over the summaries.

    This is a real degradation — cross-section reasoning ("the auditor's KAM
    matches the receivables spike in Q3") is weakened because the model never
    sees both at once. Prefer a 32k+ context model and avoid this path.
    """
    if fits_context(system, evidence + question, max_tokens):
        return chat_json(system, f"{evidence}\n\n{question}", schema, max_tokens)

    budget = max(context_limit() - max_tokens - 1000, 2000)
    chunks = chunk_text(evidence, budget // 2)
    notes = []
    for i, ch in enumerate(chunks):
        try:
            summary = chat(MAP_SYSTEM, f"Slice {i+1}/{len(chunks)}:\n\n{ch}",
                           json_mode=False, max_tokens=700)
            if "NOTHING MATERIAL" not in summary.upper():
                notes.append(f"[slice {i+1}] {summary.strip()}")
        except Exception as e:
            notes.append(f"[slice {i+1}] extraction failed: {e}")

    reduced = ("EVIDENCE (condensed from filings because the model's context "
               "is too small for the full documents — treat cross-section "
               "inferences with lower confidence):\n\n" + "\n\n".join(notes))

    # Many chunks -> many summaries -> the condensed notes can themselves
    # overflow a single request (this is what caused 413s once evidence grew
    # past ~10 chunks). Recurse through the same fits_context guard rather
    # than assuming one reduction pass is always enough; cap depth so a
    # pathological case fails loudly instead of looping.
    if _depth >= 3:
        reduced = reduced[:_chars_for_budget(max_tokens)]
        return chat_json(system, f"{reduced}\n\n{question}", schema, max_tokens)
    return map_reduce_json(system, reduced, question, schema, max_tokens,
                           _depth=_depth + 1)


def _chars_for_budget(max_tokens: int) -> int:
    return max(context_limit() - max_tokens - 500, 2000) * 3


if __name__ == "__main__":
    ok, msg = is_available()
    print(f"Provider: {describe()}")
    print(f"Status: {'OK' if ok else 'NOT READY'} — {msg}")
    if ok:
        print("\nTest call...")
        try:
            out = chat_json(
                "You are a test. Respond only with JSON.",
                'Return {"status": "working", "model_family": "<your family>"}')
            print(out)
        except Exception as e:
            print(f"Failed: {e}")
