"""OpenAI adapter. Network calls occur ONLY in explicitly chosen live mode.
No automatic retries: a timeout may still represent a billable successful request.
"""
from __future__ import annotations
import base64
import json
import mimetypes
import os
import socket
import uuid
from pathlib import Path
from urllib import request, error
from content import (VISUAL_STANDARD, GRID_VIEWS, PLANNER_SYSTEM, SPACE_SYSTEM,
                     validate_concepts, validate_space)

class ProviderError(RuntimeError):
    pass


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or 'image/png'
    return 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


class OpenAIProvider:
    def __init__(self, root: Path, opener=None):
        self.root = root
        self.opener = opener or request.urlopen
        self.api_key = os.environ.get('OPENAI_API_KEY', '')
        self.text_model = os.environ.get('FCD_TEXT_MODEL', 'gpt-6-luna')
        self.image_model = os.environ.get('FCD_IMAGE_MODEL', 'gpt-image-2.5-sunburst')
        self.landscape = os.environ.get('FCD_LANDSCAPE_SIZE', '1536x864')
        self.portrait = os.environ.get('FCD_PORTRAIT_SIZE', '1024x1536')
        self.quality = os.environ.get('FCD_IMAGE_QUALITY', 'high')

    def _post(self, endpoint: str, body: bytes, content_type='application/json') -> dict:
        if not self.api_key:
            raise ProviderError('OPENAI_API_KEY is not configured on the server.')
        req = request.Request('https://api.openai.com/v1/' + endpoint, data=body,
            headers={'Authorization': 'Bearer ' + self.api_key,
                     'Content-Type': content_type}, method='POST')
        try:
            with self.opener(req, timeout=600) as response:
                raw = response.read(120 * 1024 * 1024)
            return json.loads(raw)
        except error.HTTPError as exc:
            rid = exc.headers.get('x-request-id', 'not-provided')
            msg = 'OpenAI HTTP %s; request-id=%s. Check API permissions, model, billing or size settings.' % (exc.code, rid)
            raise ProviderError(msg) from None
        except (error.URLError, TimeoutError, socket.timeout) as exc:
            raise ProviderError('Network timeout/failure. No automatic retry. Check API usage before retrying; the original call may have completed.') from exc
        except (ValueError, KeyError) as exc:
            raise ProviderError('Invalid provider response; no automatic retry.') from exc

    def text_json(self, system: str, text: str, references: list[Path]) -> dict:
        content = [{'type': 'text', 'text': text}]
        content.extend({'type': 'image_url', 'image_url': {'url': data_url(p)}} for p in references)
        payload = {'model': self.text_model, 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': content}],
            'response_format': {'type': 'json_object'}}
        if self.text_model.startswith('gpt-6'):
            payload.update({'max_completion_tokens': 12000, 'reasoning_effort': 'none'})
        else:
            payload['max_tokens'] = 12000
        response = self._post('chat/completions', json.dumps(payload).encode('utf-8'))
        choice = response['choices'][0]
        if choice.get('finish_reason') != 'stop' or choice.get('message', {}).get('refusal'):
            raise ProviderError('Planning response was incomplete or refused. No image has been approved.')
        return json.loads(choice['message']['content'])

    def concepts(self, state: dict) -> dict:
        ref = self.root / state['reference']
        return validate_concepts(self.text_json(PLANNER_SYSTEM,
            json.dumps({'brand': state['brand'], 'season': state['season']}, ensure_ascii=False), [ref]))

    def space(self, state: dict) -> dict:
        return validate_space(self.text_json(SPACE_SYSTEM,
            json.dumps({'brand': state['brand'], 'season': state['season'],
                        'locked_design': state['design']}, ensure_ascii=False),
            [self.root / state['assets']['model']['path']]))

    def image_request(self, state: dict, category: str) -> tuple[str, list[Path]]:
        design = {'brand': state['brand'], 'season': state['season'],
                  'fashion_concept': state['design'], 'model': state['model'],
                  'spatial_specification': state.get('space_spec')}
        refs: list[Path] = []
        task = ''
        if category == 'fashion':
            refs = [self.root / state['reference']]
            task = 'Create the FASHION moodboard only. Image 1 is inspiration, not literal costume. All three approved looks appear; hero largest.'
        elif category == 'model':
            refs = [self.root / state['assets']['fashion']['path']]
            task = 'Create one FULL-BODY STUDIO MODEL photo. Image 1 supplies the exact hero outfit, not the model identity. Apply the explicit adult model choices. No collage, captions or swatches.'
        elif category == 'space':
            refs = [self.root / state['assets']['model']['path'], self.root / state['assets']['fashion']['path']]
            task = 'Create the SPATIAL CONCEPT MOODBOARD, not a fashion board. Image 1 fixes the full mannequin outfit; image 2 fixes design language. Include one master store view, materials, devices and circulation diagram on pure white.'
        elif category == 'grid':
            refs = [self.root / state['assets']['space']['path'], self.root / state['assets']['model']['path']]
            task = 'Create ONLY an exact 3 columns x 3 rows architectural photo grid with thin white gutters. Image 1 locks the one-store architecture, image 2 locks garments. Nine unique physically consistent camera positions, no repeated crop. Row-major views:\n' + '\n'.join(GRID_VIEWS)
        elif category == 'lookbook':
            refs = [self.root / state['assets']['model']['path']]
            task = 'Create ONE photoreal full-body LOOKBOOK photo. Image 1 fixes model identity and ALL garments including shorts, socks and shoes. '
            if state.get('lookbook_reference'):
                refs.append(self.root / state['lookbook_reference'])
                task += 'Image 2 supplies ONLY these explicitly selected attributes: ' + ', '.join(state['transfer']) + '. Never copy its clothes or facial identity. '
            else:
                task += 'No lookbook reference was supplied. Use a coherent premium editorial environment, preserving identity and approved colors. '
            task += 'Single image, no collage, no overlays, anatomically credible pose, entire footwear visible.'
        revision = state.get('revision_note', {}).get(category, '')
        if revision:
            task += '\nUser revision (preserve all other locked specifications): ' + revision
        prompt = VISUAL_STANDARD + '\nLOCKED PROJECT:\n' + json.dumps(design, ensure_ascii=False) + '\nTASK:\n' + task
        return prompt, refs

    def image(self, state: dict, category: str) -> bytes:
        prompt, refs = self.image_request(state, category)
        size = self.landscape if category in ('fashion', 'space', 'grid') else self.portrait
        fields = {'model': self.image_model, 'prompt': prompt, 'n': '1',
                  'size': size, 'quality': self.quality, 'output_format': 'png'}
        if self.image_model == 'gpt-image-1.5':
            fields['input_fidelity'] = 'high'
        boundary = 'FCD' + uuid.uuid4().hex
        chunks = []
        for k, v in fields.items():
            chunks += [(' --' + boundary).lstrip().encode() + b'\r\n',
                       ('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode(),
                       str(v).encode('utf-8') + b'\r\n']
        for path in refs:
            mime = mimetypes.guess_type(path.name)[0] or 'image/png'
            chunks += [('--' + boundary + '\r\n').encode(),
                ('Content-Disposition: form-data; name="image[]"; filename="%s"\r\nContent-Type: %s\r\n\r\n' % (path.name, mime)).encode(),
                path.read_bytes() + b'\r\n']
        chunks.append(('--' + boundary + '--\r\n').encode())
        response = self._post('images/edits', b''.join(chunks), 'multipart/form-data; boundary=' + boundary)
        try:
            return base64.b64decode(response['data'][0]['b64_json'], validate=True)
        except (ValueError, KeyError, IndexError):
            raise ProviderError('Image API returned no valid image bytes.') from None
