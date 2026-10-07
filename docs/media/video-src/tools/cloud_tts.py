"""One cloud TTS request using the existing project's voice and settings."""
import argparse
import base64
import json
import os
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def main():
    p = argparse.ArgumentParser()
    p.add_argument('text_file')
    p.add_argument('output')
    args = p.parse_args()
    key = os.environ.get('VOLCANO_TTS_API_KEY', '').strip()
    if not key:
        raise SystemExit('Set VOLCANO_TTS_API_KEY in your cloud environment.')
    text = Path(args.text_file).read_text(encoding='utf-8-sig').strip()
    if not text:
        raise SystemExit('Input text is empty.')
    out = Path(args.output)
    record = out.with_suffix(out.suffix + '.request.json')
    if out.exists() or record.exists():
        raise SystemExit('Output or request record already exists; inspect it before a new request.')
    out.parent.mkdir(parents=True, exist_ok=True)
    reqid = str(uuid.uuid4())
    payload = {
        'app': {'cluster': 'volcano_icl'},
        'user': {'uid': 'doubao_voice_clone'},
        'audio': {
            'voice_type': os.environ.get('VOLCANO_TTS_VOICE_ID', 'S_UwHny2AM1'),
            'encoding': 'mp3', 'speed_ratio': 1.12, 'pitch_ratio': 1.0,
            'enable_emotion': True, 'emotion': 'happy', 'emotion_scale': 2.0,
        },
        'request': {'reqid': reqid, 'text': text, 'operation': 'query'},
    }
    state = {'reqid': reqid, 'status': 'submitting', 'payload': payload}
    def save():
        record.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    with record.open('x', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    request = Request(
        'https://openspeech.bytedance.com/api/v1/tts',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'x-api-key': key, 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=150) as response:
            raw = response.read()
            content_type = response.headers.get('Content-Type', '').lower()
            state['logid'] = response.headers.get('X-Tt-Logid')
        if 'audio' in content_type or raw.startswith(b'ID3'):
            audio = raw
        else:
            result = json.loads(raw)
            code = result.get('code')
            if code not in (None, 0, 3000, '0', '3000'):
                state.update(status='api_error', code=code)
                raise RuntimeError(f'Provider error code {code}')
            encoded = result.get('data') or result.get('audio')
            if not isinstance(encoded, str) or not encoded:
                raise RuntimeError('Response has no Base64 audio string.')
            audio = base64.b64decode(encoded)
        if not audio:
            raise RuntimeError('Empty audio response.')
        with out.open('xb') as f:
            f.write(audio)
        state.update(status='completed', output=str(out), bytes=len(audio))
        save()
        print(f'Saved {out} ({len(audio)} bytes), reqid={reqid}')
    except Exception as exc:
        if isinstance(exc, HTTPError):
            state['http_status'] = exc.code
        if state['status'] == 'submitting':
            state['status'] = 'result_unknown'
        state['error_type'] = type(exc).__name__
        save()
        raise SystemExit(f'{type(exc).__name__}; reqid={reqid}; inspect {record} before retrying.')


if __name__ == '__main__':
    main()
