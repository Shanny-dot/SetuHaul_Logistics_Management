"""WhatsApp voice notes: listen with ElevenLabs speech-to-text, answer with ElevenLabs text-to-speech.

Core activity: turn a driver's voice note into text for the SAME engine, and its reply into a voice note.
Nothing here decides anything: the transcript goes through the rules, safety words, offer binding and the booking
guard exactly like a typed message, and the spoken reply is the already-guarded reply text.

API facts used (ElevenLabs API reference, checked 2026-11-27):
  speech-to-text  POST https://api.elevenlabs.io/v1/speech-to-text   header xi-api-key, multipart/form-data:
                  file=<audio>, model_id (examples use "scribe_v2"), language_code optional (ISO-639-1/3, null = auto)
                  -> JSON: text, language_code, language_probability
  text-to-speech  POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=opus_48000_32
                  JSON: text, model_id, language_code (not supported by eleven_multilingual_v2) -> audio bytes
  models          eleven_v3 / eleven_v4 speak Gujarati, Hindi, Marathi, Punjabi, Tamil; multilingual_v2 / flash_v2_5
                  only Hindi and Tamil of those.
WhatsApp plays OGG/Opus as a voice note; other audio arrives as a file (Twilio WhatsApp media guidance). The bytes
are checked for the OGG signature ("OggS") before being sent as a voice note; otherwise MP3 is used instead.

Settings: ELEVENLABS_API_KEY (secret, SSM), ELEVENLABS_VOICE_ID (else the account's first voice), ELEVENLABS_TTS_MODEL
(default eleven_v3), ELEVENLABS_STT_MODEL (default scribe_v2), VOICE_NOTES=0 turns it off, SETUHAUL_MEDIA_DIR.
"""
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("setuhaul.voice")
API = "https://api.elevenlabs.io"
MAX_IN_BYTES = 5 * 1024 * 1024           # ~5 minutes of WhatsApp Opus; longer notes are not transcribed
MAX_TTS_CHARS = 700                       # a reply longer than this is spoken up to the last full sentence
MEDIA_TTL_S = 24 * 3600                   # reply audio is deleted after a day (Twilio fetches it within seconds)
TTS_LANG = {"en": "en", "hi-Latn": "hi", "hi": "hi", "gu": "gu", "mr": "mr", "pa": "pa", "mwr": "hi", "ta": "ta"}


class VoiceError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ElevenLabs:
    def __init__(self, api_key, voice_id=None, tts_model=None, stt_model=None, timeout=30):
        self.key, self.voice_id = api_key, voice_id or None
        self.tts_model = tts_model or "eleven_v3"
        self.stt_model = stt_model or "scribe_v2"
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None):
        e = env if env is not None else os.environ
        if (e.get("VOICE_NOTES") or "1") == "0" or not e.get("ELEVENLABS_API_KEY"):
            return None
        return cls(e["ELEVENLABS_API_KEY"], e.get("ELEVENLABS_VOICE_ID"), e.get("ELEVENLABS_TTS_MODEL"), e.get("ELEVENLABS_STT_MODEL"))

    def _call(self, req):
        req.add_header("xi-api-key", self.key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise VoiceError(f"ElevenLabs HTTP {e.code}: {e.read()[:200]!r}")
        except (urllib.error.URLError, OSError) as e:
            raise VoiceError(f"ElevenLabs unreachable ({type(e).__name__})")

    # ------------------------------------------------------------ speech to text
    def transcribe(self, audio, content_type="audio/ogg"):
        if len(audio) > MAX_IN_BYTES:
            raise VoiceError("voice note too long")
        boundary = uuid.uuid4().hex
        ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/amr": "amr", "audio/aac": "aac"}.get(
            content_type.split(";")[0].strip(), "ogg")
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="model_id"\r\n\r\n{self.stt_model}\r\n'.encode(),
                 (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="note.{ext}"\r\n'
                  f'Content-Type: {content_type}\r\n\r\n').encode(), audio, f"\r\n--{boundary}--\r\n".encode()]
        req = urllib.request.Request(API + "/v1/speech-to-text", data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        body, _ = self._call(req)
        d = json.loads(body or b"{}")
        text = (d.get("text") or "").strip()
        if not text:
            raise VoiceError("no speech recognised")
        return text, d.get("language_code")

    # ------------------------------------------------------------ text to speech
    def _voice(self):
        if self.voice_id:
            return self.voice_id
        body, _ = self._call(urllib.request.Request(API + "/v1/voices", method="GET"))
        voices = json.loads(body or b"{}").get("voices") or []
        if not voices:
            raise VoiceError("no ElevenLabs voice available; set ELEVENLABS_VOICE_ID")
        self.voice_id = voices[0]["voice_id"]
        return self.voice_id

    def speak(self, text, lang="en"):
        """-> (audio bytes, extension, content type). OGG/Opus when the bytes are OGG, else MP3."""
        payload = {"text": spoken_text(text), "model_id": self.tts_model}
        if "multilingual_v2" not in self.tts_model and TTS_LANG.get(lang):
            payload["language_code"] = TTS_LANG[lang]
        for fmt, ext, ctype in (("opus_48000_32", "ogg", "audio/ogg"), ("mp3_44100_64", "mp3", "audio/mpeg")):
            req = urllib.request.Request(f"{API}/v1/text-to-speech/{self._voice()}?output_format={fmt}", method="POST",
                                         data=json.dumps(payload, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"})
            audio, _ = self._call(req)
            if ext == "ogg" and not audio.startswith(b"OggS"):
                log.warning("ElevenLabs opus output is not an OGG file; falling back to MP3 (sent as a file, not a voice note)")
                continue
            return audio, ext, ctype
        raise VoiceError("no usable audio")


def spoken_text(text):
    """What the voice says: the reply without URLs and markup, cut at a sentence end if very long."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"[*_`#>]", "", t)
    t = re.sub(r"\n+", ". ", t).replace("..", ".").strip()
    if len(t) > MAX_TTS_CHARS:
        cut = t[:MAX_TTS_CHARS]
        t = cut[:max(cut.rfind(". "), cut.rfind("। "), 200) + 1]
    return t


def download_twilio_media(url, account_sid, auth_token, timeout=20):
    """Twilio media URLs may require HTTP Basic auth and redirect to a storage URL: authenticate the first request
    only (a signed storage URL refuses a second auth scheme), then follow the redirect without credentials."""
    import base64
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()})
    for _ in range(3):
        try:
            with opener.open(req, timeout=timeout) as r:
                data = r.read(MAX_IN_BYTES + 1)
                if len(data) > MAX_IN_BYTES:
                    raise VoiceError("voice note too long")
                return data, r.headers.get("Content-Type", "audio/ogg")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                req = urllib.request.Request(e.headers["Location"])           # no credentials on the redirect
                continue
            raise VoiceError(f"media download HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise VoiceError(f"media download failed ({type(e).__name__})")
    raise VoiceError("too many redirects")


def save_reply_audio(media_dir, audio, ext):
    """Store reply audio under an unguessable name; returns the file name (served at /media/<name>)."""
    os.makedirs(media_dir, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{ext}"
    with open(os.path.join(media_dir, name), "wb") as f:
        f.write(audio)
    return name


def cleanup(media_dir, now=None, ttl_s=MEDIA_TTL_S):
    now = now or time.time()
    gone = 0
    if not os.path.isdir(media_dir):
        return 0
    for n in os.listdir(media_dir):
        p = os.path.join(media_dir, n)
        if re.fullmatch(r"[0-9a-f]{32}\.(ogg|mp3)", n) and now - os.path.getmtime(p) > ttl_s:
            os.remove(p)
            gone += 1
    return gone
