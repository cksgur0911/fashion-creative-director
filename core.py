"""Persistent, click-driven, single-user workflow; no ChatGPT turn dependency."""
from __future__ import annotations
import base64
import copy
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from content import CATEGORIES, STEMS, validate_concepts, validate_space
from provider import OpenAIProvider

MAX_IMAGE_BYTES = 15 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000
MODEL_OPTIONS = {
    'gender': ['female', 'male', 'nonbinary', 'unspecified'],
    'age': ['20s', '30s', '40s', '50s', '60plus', 'adult_unspecified'],
    'appearance': ['east_asian', 'southeast_asian', 'south_asian', 'black_african',
                   'white_european', 'latino_background', 'mixed_multiracial', 'unspecified'],
    'styling': ['approved', 'sporty_high_fashion', 'minimal_lookbook']}
TRANSFER = ['lighting', 'background', 'hair', 'pose', 'props']
REVISIONS = {
    'fidelity': 'Improve fidelity to the approved design and reference; do not redesign.',
    'white': 'Correct only the presentation canvas to pure white #FFFFFF; preserve all photo colors.',
    'details': 'Improve garment texture, stitching and hardware readability; preserve design and palette.',
    'layout': 'Improve hierarchy and whitespace, keep the complete hero unobscured.',
    'circulation': 'Improve clear product sightlines and open circulation, preserve master architecture.'}

class Conflict(ValueError):
    pass


def clean_note(value, limit=800):
    if value is None:
        return ''
    text = ' '.join(str(value).replace('\r', '\n').split())
    if len(text) > limit:
        raise ValueError('수정 메모는 800자 이하여야 합니다')
    return text


def image_info(raw: bytes) -> tuple[str, int, int]:
    if not raw or len(raw) > 60 * 1024 * 1024:
        raise ValueError('Invalid image size')
    try:
        with Image.open(io.BytesIO(raw)) as im:
            fmt, dims = im.format, im.size
            if fmt not in ('PNG', 'JPEG', 'WEBP') or min(dims) < 32:
                raise ValueError('Only nonempty PNG, JPEG and WebP images are accepted')
            im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('The file is not a readable supported image') from exc
    return {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[fmt], dims[0], dims[1]


def decode_upload(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 1.4:
        raise ValueError('Reference exceeds the 15 MB upload limit')
    if not re.match(r'^data:image/(png|jpeg|webp);base64,', value):
        raise ValueError('Reference must be a PNG, JPEG or WebP data URL')
    try:
        raw = base64.b64decode(value.split(',', 1)[1], validate=True)
    except ValueError:
        raise ValueError('Invalid image encoding') from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Reference exceeds 15 MB')
    image_info(raw)
    return raw


class Engine:
    def __init__(self, root: Path, app_root: Path, provider_factory=None):
        self.root = root.resolve(); self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.app_root = app_root.resolve()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fcd-image-worker')
        self.provider_factory = provider_factory or (lambda: OpenAIProvider(self.root))
        for file in self.root.glob('*/state.json'):
            state = json.loads(file.read_text(encoding='utf-8'))
            if state.get('busy'):
                state.update(busy=False, phase='ERROR', error='The server stopped during a job. Existing outputs were kept. Check API usage before retrying.')
                self._save(state)

    def close(self):
        self.pool.shutdown(wait=True)

    def _statefile(self, pid):
        if not re.fullmatch(r'[a-f0-9]{32}', str(pid)):
            raise ValueError('Invalid project identifier')
        return self.root / pid / 'state.json'

    def _load(self, pid):
        return json.loads(self._statefile(pid).read_text(encoding='utf-8'))

    def _save(self, state):
        state['revision'] += 1
        state['updated_at'] = time.time()
        target = self._statefile(state['id']); target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, target)

    def get(self, pid, owner=None):
        with self.lock:
            s = self._load(pid)
            if owner is not None and s.get('owner') != owner:
                raise FileNotFoundError('Project not found')
            return copy.deepcopy(s)

    def list_projects(self, owner=None):
        result = []
        with self.lock:
            for p in self.root.glob('*/state.json'):
                s = json.loads(p.read_text(encoding='utf-8'))
                if owner is not None and s.get('owner') != owner:
                    continue
                result.append({k: s[k] for k in ('id', 'brand', 'season', 'phase', 'mode', 'updated_at')})
        return sorted(result, key=lambda x:x['updated_at'], reverse=True)

    def _store_reference(self, pid, name, raw):
        ext, _, _ = image_info(raw)
        rel = Path(pid) / 'references' / (name + ext)
        target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return rel.as_posix()

    def create(self, body, owner=None):
        mode = 'live'
        if body.get('mode', 'live') != 'live':
            raise ValueError('Shared cloud supports live mode only')
        brand, season = str(body.get('brand', '')).strip(), str(body.get('season', '')).strip()
        if not brand or len(brand) > 100 or not re.fullmatch(r'20\d{2} (S/S|F/W)', season):
            raise ValueError('Select a valid brand and season')
        if body.get('consent_api') is not True:
            raise ValueError('API upload and cost consent is required')
        if not os.environ.get('OPENAI_API_KEY'):
            raise ValueError('Configure OPENAI_API_KEY on the server before live generation')
        if not body.get('reference'):
            raise ValueError('Attach an inspiration image for live mode')
        pid = uuid.uuid4().hex
        raw = decode_upload(body['reference'])
        with self.lock:
            ref = self._store_reference(pid, 'inspiration', raw)
            s = {'id':pid, 'revision':0, 'mode':mode, 'brand':brand, 'season':season, 'owner': owner,
                 'phase':'CONCEPTS_GENERATING', 'busy':True, 'task':'concepts',
                 'reference':ref, 'design':None, 'model':{}, 'assets':{},
                 'approvals':{}, 'revision_note':{}, 'seen':[], 'error':None,
                 'lookbook_reference':None, 'transfer':TRANSFER, 'history':[], 'created_at':time.time()}
            self._save(s)
        self.pool.submit(self._run, pid, 'concepts')
        return self.get(pid)

    def _expect(self, s, phase):
        if s['phase'] != phase:
            raise Conflict('This action no longer matches the current stage; refresh the project')

    def apply(self, pid, body, owner=None):
        task = None
        event_id = str(body.get('event_id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', event_id):
            raise ValueError('A valid event identifier is required')
        with self.lock:
            s = self._load(pid)
            if owner is not None and s.get('owner') != owner:
                raise FileNotFoundError('Project not found')
            if event_id in s['seen']:
                return s
            if body.get('revision') != s['revision']:
                raise Conflict('Stale control. The current project has been refreshed; choose again.')
            if s['busy']:
                raise Conflict('A job is already in progress; no duplicate request was sent')
            action, value = body.get('action'), body.get('value')
            if action == 'select_concept':
                self._expect(s, 'CONCEPTS')
                chosen = next((x for x in s['concepts'] if x['id'] == value), None)
                if not chosen: raise ValueError('Unknown concept')
                s['design'] = copy.deepcopy(chosen); s['phase'] = 'DESIGN_REVIEW'
            elif action == 'choose_again':
                self._expect(s, 'DESIGN_REVIEW'); s['phase'] = 'CONCEPTS'; s['design'] = None
            elif action == 'approve_design':
                self._expect(s, 'DESIGN_REVIEW')
                note = clean_note(body.get('note'))
                if note:
                    s['revision_note']['fashion'] = note
                s['approvals']['design'] = hashlib.sha256(json.dumps(s['design'], sort_keys=True).encode()).hexdigest()
                task = 'fashion'
            elif action == 'approve_fashion':
                self._expect(s, 'FASHION_REVIEW')
                s['approvals']['fashion'] = s['assets']['fashion']['sha256']
                s['phase'] = 'MODEL_SETTINGS'
            elif action == 'set_model':
                self._expect(s, 'MODEL_SETTINGS')
                field = body.get('field')
                if field not in MODEL_OPTIONS or value not in MODEL_OPTIONS[field]:
                    raise ValueError('Invalid model choice')
                s['model'][field] = value
                if all(k in s['model'] for k in MODEL_OPTIONS): task = 'model_space'
            elif action == 'approve_space':
                self._expect(s, 'SPACE_REVIEW')
                s['approvals']['space'] = s['assets']['space']['sha256']; task = 'grid'
            elif action in ('attach_lookbook', 'skip_lookbook'):
                self._expect(s, 'LOOKBOOK_REFERENCE')
                if action == 'attach_lookbook':
                    raw = decode_upload(body.get('reference', ''))
                    s['lookbook_reference'] = self._store_reference(pid, 'lookbook', raw)
                    transfer = body.get('transfer', TRANSFER)
                    if not isinstance(transfer, list) or not transfer or any(v not in TRANSFER for v in transfer):
                        raise ValueError('Select valid reference attributes')
                    s['transfer'] = transfer
                else:
                    s['lookbook_reference'] = None
                task = 'lookbook_package'
            elif action in ('regenerate', 'revise'):
                category = {'FASHION_REVIEW':'fashion', 'SPACE_REVIEW':'space'}.get(s['phase'])
                if not category: raise Conflict('Regeneration is only available at an active review gate')
                if action == 'revise':
                    preset = value if value in REVISIONS else None
                    note = clean_note(body.get('note'))
                    if not preset and not note:
                        raise ValueError('수정 방향 또는 수정 메모를 입력해 주세요')
                    parts = []
                    if preset:
                        parts.append(REVISIONS[preset])
                    if note:
                        parts.append(note)
                    s['revision_note'][category] = ' '.join(parts)
                old = s['assets'].pop(category, None)
                if old: s['history'].append(old)
                s['approvals'].pop(category, None); task = category
            elif action == 'retry':
                self._expect(s, 'ERROR'); task = s['task']
            else:
                raise ValueError('Unknown action')
            if task:
                s.update(task=task, busy=True, phase=self._phase(task), error=None)
            s['seen'] = (s['seen'] + [event_id])[-100:]
            self._save(s)
        if task:
            self.pool.submit(self._run, pid, task)
        return self.get(pid)

    @staticmethod
    def _phase(task):
        return {'concepts':'CONCEPTS_GENERATING','fashion':'FASHION_GENERATING',
            'model_space':'MODEL_GENERATING','space':'SPACE_GENERATING','grid':'GRID_GENERATING',
            'lookbook_package':'LOOKBOOK_GENERATING'}[task]

    def _update(self, pid, **changes):
        with self.lock:
            s = self._load(pid); s.update(changes); self._save(s)
        return s

    def _record(self, pid, category, raw):
        ext, width, height = image_info(raw)
        s = self.get(pid)
        name = STEMS[category] + '_' + uuid.uuid4().hex[:8] + ext
        relative = Path(pid) / 'assets' / name
        target = self.root / relative; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if category in ('fashion', 'space', 'grid') and abs(width / height - 16 / 9) > .015:
            raise ValueError('Returned board is not 16:9. Raw image retained for diagnosis; no automatic crop or retry.')
        asset = {'category':category, 'path':relative.as_posix(), 'file':name,
            'width':width,'height':height,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'source':'openai_image_api'}
        with self.lock:
            s = self._load(pid); s['assets'][category] = asset; self._save(s)

    def _make_image(self, pid, category, provider):
        s = self.get(pid)
        if category in s['assets']: return
        raw = provider.image(s, category)
        self._record(pid, category, raw)

    def _run(self, pid, task):
        try:
            s = self.get(pid); provider = self.provider_factory()
            if task == 'concepts':
                result = provider.concepts(s)
                self._update(pid, concepts=result['concepts'], analysis=result['analysis'], recommendation=result['recommendation'], phase='CONCEPTS', busy=False)
            elif task == 'fashion':
                self._make_image(pid, 'fashion', provider)
                self._update(pid, phase='FASHION_REVIEW', busy=False)
            elif task in ('model_space','space'):
                if task == 'model_space':
                    self._make_image(pid, 'model', provider)
                self._update(pid, phase='SPACE_GENERATING')
                s = self.get(pid)
                if not s.get('space_spec'):
                    space = provider.space(s)
                    self._update(pid, space_spec=space)
                self._make_image(pid, 'space', provider)
                self._update(pid, phase='SPACE_REVIEW', busy=False)
            elif task == 'grid':
                self._make_image(pid, 'grid', provider)
                self._update(pid, phase='LOOKBOOK_REFERENCE', busy=False)
            elif task == 'lookbook_package':
                self._make_image(pid, 'lookbook', provider)
                self._update(pid, phase='PACKAGING')
                package = self.package(pid)
                self._update(pid, phase='COMPLETE', busy=False, package=package)
        except Exception as exc:
            self._update(pid, phase='ERROR', busy=False, error=str(exc)[:1000])

    def package(self, pid):
        s = self.get(pid)
        if not s['approvals'].get('fashion') or not s['approvals'].get('space'):
            raise ValueError('Both moodboards must be explicitly approved before packaging')
        if set(s['assets']) != set(CATEGORIES):
            raise ValueError('All five image deliverables are required')
        files, hashes, records = [], set(), []
        for category in CATEGORIES:
            a = s['assets'][category]; raw = (self.root/a['path']).read_bytes()
            ext, w, h = image_info(raw); digest = hashlib.sha256(raw).hexdigest()
            if digest != a['sha256'] or digest in hashes:
                raise ValueError('Image checksum mismatch or duplicate deliverables')
            hashes.add(digest); name = STEMS[category] + ext; files.append((name, raw))
            records.append({'file':name,'sha256':digest,'width':w,'height':h,'bytes':len(raw),'source':a['source']})
        manifest = {'project_id':pid,'brand':s['brand'],'season':s['season'],'mode':s['mode'],
                    'fashion_concept':s['design']['title'],'space_concept':s['space_spec']['title'],
                    'images':records,'approval_hashes':s['approvals']}
        rel = Path(pid) / 'FASHION_CREATIVE_DIRECTOR_FINAL.zip'
        with zipfile.ZipFile(self.root/rel, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for name, raw in files: z.writestr(name, raw)
            z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
        with zipfile.ZipFile(self.root/rel) as z:
            if z.testzip() is not None or len(z.namelist()) != 6:
                raise ValueError('ZIP integrity failure')
            for name, raw in files:
                if z.read(name) != raw: raise ValueError('Image bytes changed during packaging')
        return {'path':rel.as_posix(),'file':rel.name,'bytes':(self.root/rel).stat().st_size}
