"""
harmony_audio.py — browser mic → STT (Whisper) → fills the chat input.

Audio INPUT only (no TTS). The browser records webm/opus with MediaRecorder,
POSTs it to the /stt endpoint, which ffmpeg-converts to 16k mono wav and
transcribes it with faster-whisper (stt_engine.py, copied from
AI_ENGINE/UI_TTS). The recognised text lands in the chat input box.

Thin-module rule: harmony-ai.py keeps only `import harmony_audio` + one
`harmony_audio.inject(...)` + `harmony_audio.build_mic()` call inside the
input's append slot. The Whisper model is warmed up in the background and
auto-unloaded when idle.

Note: getUserMedia requires a secure context — use http://localhost:8080
(or HTTPS). On http://<lan-ip>:8080 the browser blocks the mic.
"""
import asyncio
import os
import subprocess
import sys
import threading

from fastapi import Request
from nicegui import app, ui

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(BASE_DIR, 'History', 'tmp')

_prompt_input = None
_notify = None
_send = None


def inject(deps):
    """Wire in the chat input + notify helper + send callback. Never raises."""
    global _prompt_input, _notify, _send
    try:
        _prompt_input = deps.get('prompt_input')
        _notify = deps.get('safe_notify')
        _send = deps.get('send')
        os.makedirs(TMP_DIR, exist_ok=True)
    except Exception:
        pass


def _say(msg, type='positive', timeout=6000):
    try:
        if _notify:
            _notify(msg, type=type, timeout=timeout)
        else:
            ui.notify(msg, type=type, timeout=timeout)
    except Exception:
        pass


def warm_stt_on_boot(size='base'):
    """Preload Whisper in the background so the first mic use is instant."""
    def _do():
        try:
            sys.path.insert(0, BASE_DIR)
            import stt_engine
            stt_engine.warm_up(size)
        except Exception as e:
            print(f'[STT] warm-up skipped: {e}', flush=True)
    threading.Thread(target=_do, daemon=True).start()


@app.post('/stt')
async def stt_upload(request: Request):
    """Receive browser audio, transcribe it, return {text, language, confidence}."""
    body = await request.body()
    if not body:
        return {'text': ''}
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        src = os.path.join(TMP_DIR, 'voice_input.webm')
        wav = os.path.join(TMP_DIR, 'voice_input.wav')
        with open(src, 'wb') as f:
            f.write(body)
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-i', src, '-ar', '16000', '-ac', '1', wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        except Exception as e:
            return {'text': f'ffmpeg error: {e}'}
        sys.path.insert(0, BASE_DIR)
        from stt_engine import transcribe
        text, info = await asyncio.to_thread(transcribe, wav, return_info=True)
        return {'text': text, 'language': info.get('language', ''),
                'confidence': info.get('confidence', 0.0)}
    except Exception as e:
        return {'text': f'STT error: {e}'}


def build_mic():
    """Create and return a hold-to-talk mic button wired to /stt.

    Call this inside the input's append slot so the button sits next to Send.
    Fills the chat input with the recognised text (press Enter/Send to go).
    """
    async def on_stt_result(e):
        args = e.args or []
        text = (args[0] if len(args) > 0 and args[0] else '') or ''
        confidence = float(args[1]) if len(args) > 1 and args[1] else 0.0
        language = (args[2] if len(args) > 2 and args[2] else '') or '?'
        try:
            if _prompt_input is not None:
                _prompt_input.value = text
                _prompt_input.update()
        except Exception:
            pass
        if not text.strip():
            _say('🗣️ I heard silence — speak a little louder?', type='warning')
            return
        if confidence < 0.5:
            _say(f'🗣️ Not sure I heard that (detected {language}). '
                 f'Did you say: "{text}"?', type='warning', timeout=8000)
            return
        _say('🗣️ Voice captured — sending!', type='positive')
        if _send is not None:
            try:
                _result = _send()
                if asyncio.iscoroutine(_result):
                    await _result
            except Exception as ex:
                _say(f'Send failed: {ex}', type='negative')

    voice_bridge = ui.element()
    voice_bridge.on('stt-result', on_stt_result)

    def _inject_voice_js():
        try:
            current_lid = next(iter(voice_bridge._event_listeners))
        except Exception:
            current_lid = None
        current_eid = voice_bridge.id
        ui.run_javascript(f'''
            window.__sttIds = {{ id: {current_eid}, lid: "{current_lid}" }};
            window.__voiceRec = null; window.__voiceChunks = [];
            window.__voiceActive = false;
            window.__startVoice = function() {{
                if (window.__voiceActive) return;
                if (!window.isSecureContext) {{
                    alert('Microphone blocked: not HTTPS. Use https://192.168.0.6:8080');
                    return;
                }}
                if (!navigator.mediaDevices || !window.MediaRecorder) {{
                    alert('Voice input not supported in this browser.'); return;
                }}
                window.__voiceActive = true;
                window.__voiceChunks = [];
                navigator.mediaDevices.getUserMedia({{ audio: true }}).then(stream => {{
                    window.__voiceStream = stream;
                    const rec = new MediaRecorder(stream);
                    window.__voiceRec = rec;
                    rec.ondataavailable = e => {{ if (e.data && e.data.size) window.__voiceChunks.push(e.data); }};
                    rec.onstop = () => {{
                        window.__voiceActive = false;
                        const blob = new Blob(window.__voiceChunks, {{ type: 'audio/webm' }});
                        fetch('/stt', {{ method: 'POST', body: blob }})
                            .then(r => r.json())
                            .then(d => {{
                                window.socket.emit('event', {{
                                    id: window.__sttIds.id,
                                    client_id: window.clientId,
                                    listener_id: window.__sttIds.lid,
                                    args: [JSON.stringify(d.text || ''), JSON.stringify(d.confidence || 0), JSON.stringify(d.language || '')],
                                }});
                            }})
                            .catch(err => console.error('STT error:', err));
                        stream.getTracks().forEach(t => t.stop());
                    }};
                    rec.start();
                }}).catch(err => {{ alert('Microphone permission denied: ' + err.message); }});
            }};
            window.__stopVoice = function() {{
                if (window.__voiceRec && window.__voiceRec.state !== 'inactive') window.__voiceRec.stop();
            }};
        ''')
    ui.timer(0.1, _inject_voice_js, once=True)

    def hold_start():
        ui.run_javascript('__startVoice()')

    def hold_stop():
        ui.run_javascript('__stopVoice()')

    mic_btn = ui.button(icon='mic').props('flat round').classes(
        'text-red-500').tooltip('Hold to talk (release to fill the input)')
    mic_btn.on('mousedown', hold_start)
    mic_btn.on('mouseup', hold_stop)
    mic_btn.on('mouseleave', hold_stop)
    mic_btn.on('touchstart', hold_start, []).on('touchend', hold_stop, [])
    mic_btn.props('style="user-select:none;-webkit-user-select:none;"')

    def stt_idle_cleanup():
        try:
            sys.path.insert(0, BASE_DIR)
            import stt_engine
            stt_engine.auto_unload_if_idle()
        except Exception:
            pass
    ui.timer(30.0, stt_idle_cleanup)

    return mic_btn
