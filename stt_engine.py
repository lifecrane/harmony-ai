"""Faster-Whisper STT engine wrapper for the NiceAI control panel.

API expected by niceai.py:
    set_model_size('tiny' | 'base' | 'small')   - switch model size (unloads current)
    unload_model()                               - free the model from RAM
    auto_unload_if_idle(max_idle_seconds=300)    - unload if idle too long
    transcribe(wav_path, return_info=True)       -> (text, {'language': ..., 'confidence': ...})
"""
import os
import threading
import time

_current_size = None
_model = None
_model_lock = threading.Lock()
_last_use = 0.0


def _load(size):
    global _model, _current_size, _last_use
    if _current_size == size and _model is not None:
        _last_use = time.time()
        return _model
    _model = None
    _current_size = None
    try:
        from faster_whisper import WhisperModel
        model_path = f'Systran/faster-whisper-{size}'
        _model = WhisperModel(model_path, device='cpu', compute_type='int8')
        _current_size = size
        _last_use = time.time()
    except Exception as e:
        print(f'[STT] load error: {e}', flush=True)
        _model = None
        _current_size = None
    return _model


def set_model_size(size):
    """Switch to a different whisper size. Small is multilingual, base/tiny are lighter."""
    size = str(size).strip().lower()
    if size not in ('tiny', 'base', 'small'):
        size = 'base'
    with _model_lock:
        _load(size)


def unload_model():
    global _model, _current_size
    with _model_lock:
        _model = None
        _current_size = None


def auto_unload_if_idle(max_idle_seconds=300):
    global _model, _current_size
    if _model is not None and (time.time() - _last_use) > max_idle_seconds:
        with _model_lock:
            _model = None
            _current_size = None


def warm_up(size='base'):
    """Preload the STT model in a background thread so the first mic use is instant.
    Safe to call at startup — never blocks, never fails the app."""
    def _do():
        try:
            with _model_lock:
                _load(size)
            print(f'[STT] warmed up with {size} model', flush=True)
        except Exception as e:
            print(f'[STT] warm-up failed: {e}', flush=True)
    threading.Thread(target=_do, daemon=True).start()


def transcribe(wav_path, return_info=True, language=None):
    global _last_use
    size = _current_size or 'base'
    with _model_lock:
        model = _load(size)
    if model is None:
        if return_info:
            return 'STT model failed to load', {'language': '', 'confidence': 0.0}
        return 'STT model failed to load'
    _last_use = time.time()
    try:
        segments, info = model.transcribe(wav_path, language=language)
        text = ''.join(seg.text for seg in segments).strip()
        if return_info:
            return text, {
                'language': getattr(info, 'language', ''),
                'confidence': float(getattr(info, 'language_probability', 0.0) or 0.0),
            }
        return text
    except Exception as e:
        print(f'[STT] transcribe error: {e}', flush=True)
        if return_info:
            return '', {'language': '', 'confidence': 0.0}
        return ''
