# openai_client.py
import os
import openai
from typing import Optional

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Configure models per environment
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")  # or "whisper-1"
SUMMARIZE_MODEL = os.getenv("OPENAI_SUMMARIZE_MODEL", "gpt-4o")  # pick your model

def transcribe_file(file_path: str, language: Optional[str]=None) -> str:
    """
    Transcribe audio file to text using OpenAI SDK.
    Returns plain transcript string.
    """
    # Use openai.Audio.transcribe if using SDK that supports it:
    with open(file_path, "rb") as f:
        # if you used older openai client use: openai.Audio.transcribe(...)
        res = openai.Audio.transcriptions.create(file=f, model=TRANSCRIBE_MODEL)
    # The exact structure depends on SDK version; adjust if needed.
    return res["text"] if isinstance(res, dict) and "text" in res else getattr(res, "text", str(res))

def summarize_text(text: str, instructions: str = "", max_tokens: int = 300, temperature: float = 0.2) -> str:
    """
    Return a short structured summary for `text`.
    """
    prompt = f"""You are a concise meeting summarizer.
Instructions: {instructions}

Text:
{text}

Return a JSON object with fields: summary, action_items (list), decisions (list).
Return only valid JSON.
"""
    resp = openai.ChatCompletion.create(
        model=SUMMARIZE_MODEL,
        messages=[{"role":"system","content":"You summarize meetings."},
                  {"role":"user","content":prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    # parse response content
    content = resp["choices"][0]["message"]["content"]
    return content.strip()
