#!/usr/bin/env python3
"""Live compatibility checks. Dry-run by default; --execute spends on selected cases.

uv run --env-file .env.local --no-project --python .venv/bin/python tools/provider_compat.py --execute
Each case keeps its request metadata before POST. Existing cases are never submitted twice.
A video can be continued with --resume CASE (GET only).
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/generate2dmedia/scripts'))
import generate_media as media
import media_providers as providers
from PIL import Image, ImageDraw


def cases():
    model = 'gpt-image-2.6-sunburst'
    slime = 'One green slime enemy for a 2D Godot game. Full body centered, hard clean silhouette, flat colors, isolated on a pure magenta #FF00FF background. No text, no cast shadow.'
    return {
        'openai-b64-alpha': dict(provider='openai', model=model, kind='image', prompt=slime,
            size='1024x1024', quality='low', background='transparent', response_format='b64_json', output_format='png'),
        'openai-edit-one': dict(provider='openai', model=model, kind='image', edit=True, refs=1,
            prompt='Edit the reference: change only the blue circle into a green circle. Keep the red square, yellow triangle, white background and exact layout.',
            size='1024x1024', quality='low', response_format='b64_json', output_format='png'),
        'openai-edit-two': dict(provider='openai', model=model, kind='image', edit=True, refs=2,
            prompt='Use image 1 as the layout. Replace only its blue circle with the purple star from image 2. Keep the red square and yellow triangle unchanged, white background.',
            size='1024x1024', response_format='b64_json', output_format='png'),
        'openai-sdk-landscape': dict(provider='openai', model=model, kind='image', sdk=True,
            prompt='2D side-scrolling game background, quiet blue mountain silhouettes, layered distant hills, empty foreground, no characters or text, seamless-looking horizontal composition.',
            size='1536x1024', quality='low', response_format='b64_json', output_format='webp', complexity='low'),
        'xai-image': dict(provider='xai', model='grok-imagine-image-2.0', kind='image', prompt=slime,
            n=1, resolution='1k', aspect_ratio='1:1', quality='low', response_format='b64_json'),
        'xai-edit-two': dict(provider='xai', model='grok-imagine-image-2.0', kind='image', edit=True, refs=2,
            prompt='Use image 1 as the layout. Replace only its blue circle with the purple star from image 2. Keep the red square and yellow triangle unchanged, white background.',
            n=1, resolution='1k', quality='medium', response_format='b64_json'),
        'xai-pins': dict(provider='xai', model='grok-imagine-video-1.5', kind='video', refs=1, pins=True,
            prompt='A fixed camera 2D game animation. The blue circle gently pulses once and returns to its starting size. Keep the red square and yellow triangle completely still. Preserve the flat white background and layout. No text.',
            duration=2, resolution='720p', generate_audio=False),
        'xai-1080': dict(provider='xai', model='grok-imagine-video-1.5', kind='video', refs=1,
            prompt='Fixed camera, subtle single pulse of the blue circle, keep the red square and yellow triangle still. Preserve the white background and exact layout.',
            duration=1, resolution='1080p', generate_audio=False),
    }


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def inputs(folder):
    folder.mkdir(parents=True, exist_ok=True)
    one = folder / 'layout.png'
    two = folder / 'star.png'
    if not one.exists():
        img = Image.new('RGB', (1024, 1024), 'white'); d = ImageDraw.Draw(img)
        d.rectangle((100, 100, 350, 350), fill=(230, 30, 30))
        d.ellipse((600, 100, 850, 350), fill=(30, 60, 240))
        d.polygon([(512, 600), (360, 850), (664, 850)], fill=(245, 210, 20)); img.save(one)
    if not two.exists():
        img = Image.new('RGB', (1024, 1024), 'white'); d = ImageDraw.Draw(img)
        d.polygon([(512,200),(590,410),(820,410),(635,550),(700,780),(512,640),(324,780),(389,550),(204,410),(434,410)], fill=(160, 30, 220)); img.save(two)
    return [one, two]


def datauri(path):
    return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def summarize(reply):
    result = {k: reply[k] for k in ('created', 'model', 'request_id', 'status', 'progress', 'usage') if k in reply}
    if isinstance(reply.get('data'), list):
        result['data'] = [{k: ('<base64>' if k == 'b64_json' else
                                    '<url>' if k == 'url' else v)
                           for k, v in item.items()} for item in reply['data'] if isinstance(item, dict)]
    if isinstance(reply.get('video'), dict):
        result['video'] = {k: ('<url>' if k == 'url' else v) for k, v in reply['video'].items()}
    return result


def inspect_artifact(path):
    blob = path.read_bytes()
    meta = {'path': path.name, 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
    if path.suffix != '.mp4':
        with Image.open(io.BytesIO(blob)) as im:
            meta.update(format=im.format, size=list(im.size), mode=im.mode,
                        alpha_extrema=list(im.getchannel('A').getextrema()) if 'A' in im.getbands() else None)
            im.verify()
    else:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        run = subprocess.run([ffmpeg, '-hide_banner', '-i', str(path), '-map', '0:v:0', '-f', 'null', '-'],
                             capture_output=True, text=True, timeout=120)
        meta.update(decode_ok=run.returncode == 0, decode_log=run.stderr[-5000:])
        # Decode a first/last-frame pair for pinned animation review.
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(path), '-frames:v', '1', str(path.parent/'first.png')],
                       capture_output=True, timeout=60)
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-sseof', '-0.1', '-i', str(path), '-frames:v', '1', str(path.parent/'last.png')],
                       capture_output=True, timeout=60)
    return meta


def complete_video(folder, record, key):
    transport = media.Transport(); adapter = providers.adapter('xai')
    deadline = time.monotonic() + 420
    job = {'apiBase': record['apiBase'], 'requestId': record['requestId']}
    while time.monotonic() < deadline:
        reply = transport.api('GET', adapter.status_url(job, record['apiBase']), key, timeout=60)
        record['poll'] = summarize(reply); write(folder/'result.json', record)
        state, ref = adapter.status(reply, job)
        if state == 'done':
            content = media._fetch_media(adapter, transport, ref, key, 90)
            path = folder/'result.mp4'; path.write_bytes(content)
            record.update(status='done', artifact=inspect_artifact(path), costUsd=ref.cost_usd,
                          returnedModel=ref.returned_model, durationReported=ref.duration)
            write(folder/'result.json', record); return
        time.sleep(3)
    record['status'] = 'pending_timeout'; write(folder/'result.json', record)


def run_case(name, spec, out, refs):
    folder = out/name
    if folder.exists():
        print(json.dumps({'case': name, 'status': 'already_attempted', 'record': str(folder/'result.json')}), flush=True)
        return
    folder.mkdir(parents=True)
    key = os.environ[{'openai':'OPENAI_API_KEY','xai':'XAI_API_KEY'}[spec['provider']]]
    base = media.checked_base_url(os.environ[{'openai':'OPENAI_API_BASE_URL','xai':'XAI_API_BASE_URL'}[spec['provider']]])
    endpoint = base + ('/videos/generations' if spec['kind']=='video' else '/images/edits' if spec.get('edit') else '/images/generations')
    fields = {k:v for k,v in spec.items() if k not in ('provider','kind','edit','refs','pins','sdk')}
    fields.setdefault('n',1) if spec['kind']=='image' else None
    refpaths = refs[:spec.get('refs',0)]
    record = dict(case=name, provider=spec['provider'], apiBase=base, endpoint=endpoint,
                  request=fields, referenceHashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in refpaths],
                  status='submitting', postRetries=0, sdk=bool(spec.get('sdk')))
    write(folder/'result.json', record)  # record before paid POST; never blindly retry
    started = time.monotonic()
    try:
        transport = media.Transport()
        if spec.get('sdk'):
            import openai
            with openai.OpenAI(api_key=key,base_url=base,max_retries=0,timeout=300) as client:
                reply = client.images.generate(**{k:v for k,v in fields.items() if k not in ('complexity',)},
                    extra_body={'complexity': fields['complexity']}).model_dump(exclude_none=True)
                record['sdkVersion'] = openai.__version__
        else:
            ctype = 'application/json'
            if spec.get('edit') and spec['provider']=='openai':
                pairs = [(media.inspect_image(p), p.read_bytes()) for p in refpaths]
                # inspect_image already returns (metadata, bytes) in ASF.
                pairs = [media.inspect_image(p) for p in refpaths]
                body, ctype = providers.multipart(fields, pairs)
            else:
                wire = dict(fields)
                if spec.get('edit'):
                    wire['images']=[{'type':'image_url','url':datauri(p)} for p in refpaths]
                elif spec['kind']=='video':
                    wire['image']={'url':datauri(refpaths[0])}
                    if spec.get('pins'):
                        wire['last_frame']={'url':datauri(refpaths[0])}
                        wire['keyframes']=[{'image':{'url':datauri(refpaths[0])},'timestamp_s':1}]
                body = json.dumps(wire).encode()
            meta={}; reply = transport.api('POST',endpoint,key,body,ctype,timeout=300,meta=meta)
            record.update(httpStatus=meta.get('httpStatus'),providerRequestId=meta.get('providerRequestId'))
        record.update(submitSeconds=round(time.monotonic()-started,3), response=summarize(reply))
        if spec['kind']=='video':
            record.update(requestId=reply['request_id'], status='pending');write(folder/'result.json',record)
            complete_video(folder,record,key)
        else:
            adapter = providers.adapter(spec['provider']); ref = adapter.read_submit(reply,'image')
            content = media._fetch_media(adapter,transport,ref,key,90)
            metadata, ext = media._validate_artifact(content,'image'); path = folder/('result'+ext);path.write_bytes(content)
            record.update(status='done',artifact=inspect_artifact(path),returnedModel=ref.returned_model)
            if 'size' in fields:
                record['sizeMatches'] = fields['size'] == 'x'.join(map(str,metadata['size']))
        record['totalSeconds']=round(time.monotonic()-started,3)
    except Exception as exc:
        # Never persist raw SDK exceptions (they may include headers/URLs/credentials).
        record.update(status='interrupted' if record.get('requestId') else 'failed_or_unknown',
                      exception=type(exc).__name__,errorCode=getattr(exc,'code',None),
                      httpStatus=getattr(exc,'status_code',None) or (getattr(exc,'provider',None) or {}).get('httpStatus'))
        if isinstance(exc,providers.MediaError): record['error']=str(exc)
    write(folder/'result.json',record)
    print(json.dumps(record,ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',action='append',choices=list(cases()))
    p.add_argument('--execute',action='store_true')
    p.add_argument('--resume',choices=list(cases()))
    p.add_argument('--out',type=Path,default=ROOT/'outputs/provider-compat-2026-11-12/matrix')
    args=p.parse_args()
    selected=args.case or list(cases())
    if args.resume:
        folder=args.out/args.resume;record=json.loads((folder/'result.json').read_text())
        complete_video(folder,record,os.environ['XAI_API_KEY']);print(json.dumps(record,ensure_ascii=False));return
    if not args.execute:
        print(json.dumps({'execution':'dry-run','cases':{n:cases()[n] for n in selected},'paidPosts':len(selected)},ensure_ascii=False,indent=2));return
    refs=inputs(args.out/'inputs')
    for name in selected:run_case(name,cases()[name],args.out,refs)

if __name__=='__main__':main()
