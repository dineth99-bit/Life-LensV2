"""Serve LifeLens and pass reading and explanations to a model.

The coverage math stays in the page. The model only reads words and explains.
Which model answers is set in llm.config.json, not in the page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
CONFIG_FILE = ROOT / "llm.config.json"


def load_config() -> dict:
    """llm.config.json is the only switch. Edit backend to "local" or "modal"."""
    defaults = {
        "backend": "local",
        "local": {
            "url": "http://127.0.0.1:8000/v1/chat/completions",
            "model": "Qwen/Qwen3.5-9B",
            "key": "",
        },
        "modal": {
            "url": "",
            "model": "Qwen/Qwen3.5-9B",
            "key": "",
        },
    }
    if not CONFIG_FILE.is_file():
        return defaults
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    for kind in ("local", "modal"):
        block = data.get(kind)
        if isinstance(block, dict):
            defaults[kind].update({k: block.get(k, defaults[kind][k]) for k in ("url", "model", "key")})
    if data.get("backend") in ("local", "modal"):
        defaults["backend"] = data["backend"]
    return defaults


def active_spec() -> dict:
    cfg = load_config()
    kind = cfg["backend"]
    block = cfg[kind]
    return {
        "id": kind,
        "label": "Modal" if kind == "modal" else "Local",
        "url": str(block.get("url") or "").strip(),
        "model": str(block.get("model") or "Qwen/Qwen3.5-9B").strip(),
        "key": str(block.get("key") or "").strip(),
    }


app = FastAPI(title="LifeLens V2")


class CompleteIn(BaseModel):
    prompt: str | None = None
    messages: list[dict] = Field(default_factory=list)
    json: bool = False


def _extract_json(text: str) -> dict | list | None:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _models_url(chat_url: str) -> str:
    suffix = "/chat/completions"
    if chat_url.endswith(suffix):
        return chat_url[: -len(suffix)] + "/models"
    return chat_url


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: "Bearer ***" if name.lower() == "authorization" else value
        for name, value in headers.items()
    }


def _probe(spec: dict) -> bool:
    if not spec["url"]:
        return False
    headers = {"Authorization": "Bearer " + spec["key"]} if spec["key"] else {}
    try:
        response = httpx.get(_models_url(spec["url"]), headers=headers, timeout=4)
        return response.status_code < 500
    except Exception:
        return False


@app.get("/api/backends")
def list_backends() -> dict:
    spec = active_spec()
    return {"active": spec["id"], "label": spec["label"], "model": spec["model"], "ok": _probe(spec)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(DIST / "index.html")


@app.get("/{asset_path:path}")
def asset(asset_path: str) -> FileResponse:
    target = (DIST / asset_path).resolve()
    if not str(target).startswith(str(DIST.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


@app.post("/api/complete")
def complete(body: CompleteIn) -> dict:
    spec = active_spec()
    if not spec["url"]:
        raise HTTPException(
            status_code=400,
            detail="The selected model has no endpoint. Set it in llm.config.json.",
        )
    messages = body.messages or [{"role": "user", "content": body.prompt or ""}]
    if body.json:
        instruction = "Reply with one JSON object only. No markdown and no explanation."
        if messages and messages[0].get("role") == "system":
            messages = [
                {"role": "system", "content": f"{instruction}\n\n{messages[0].get('content', '')}"},
                *messages[1:],
            ]
        else:
            messages = [{"role": "system", "content": instruction}, *messages]
    headers = {"Authorization": "Bearer " + spec["key"]} if spec["key"] else {}
    request_body = {
        "model": spec["model"],
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 2048,
        "top_p": 0.9,
        "stream": False,
        "reasoning_effort": "none",
    }
    # print(f"[LLM request] POST {spec['url']}", flush=True)
    # print(f"[LLM headers] {_safe_headers(headers)}", flush=True)
    # print(f"[LLM body] {json.dumps(request_body, ensure_ascii=False)}", flush=True)
    try:
        response = httpx.post(
            spec["url"],
            headers=headers,
            json=request_body,
            timeout=120,
        )
        print(f"[LLM response] HTTP {response.status_code}", flush=True)
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{spec['label']}: {exc}") from exc
    payload = {"ok": True, "text": text, "backend": spec["id"], "model": spec["model"], "label": spec["label"]}
    if body.json:
        payload["json"] = _extract_json(text) or {}
    return payload


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3020, log_level="info")
