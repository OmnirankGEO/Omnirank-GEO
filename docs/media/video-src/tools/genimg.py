"""APIMart 生图(gpt-image-2.5):提交 → 轮询任务 → 下载。

    python3 genimg.py <输出.png> "<提示词>" [--size 9:16] [--resolution 2k] [--quality high] [--transparent]

Key 从环境变量 APIMART_API_KEY 读取,不写入代码或记录。已存在的输出文件不会重复生成。
"""
import argparse, json, os, sys, time, uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = 'https://api.apimart.ai/v1'


def call(method, path, key, body=None, timeout=60):
    req = Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def find_urls(obj):
    """在任务结果里找出图片地址(字段名可能是 url / urls / image_url 等)。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith('http') and ('url' in k.lower() or v.split('?')[0].lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))):
                out.append(v)
            else:
                out += find_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            out += find_urls(v) if not isinstance(v, str) else ([v] if v.startswith('http') else [])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out'); ap.add_argument('prompt')
    ap.add_argument('--model', default='gpt-image-2.5-flare')
    ap.add_argument('--size', default='9:16'); ap.add_argument('--resolution', default='2k')
    ap.add_argument('--quality', default='high'); ap.add_argument('--transparent', action='store_true')
    a = ap.parse_args()
    key = os.environ.get('APIMART_API_KEY', '').strip()
    if not key:
        raise SystemExit('需要环境变量 APIMART_API_KEY')
    if os.path.exists(a.out):
        print('已存在,跳过', a.out); return
    rec = a.out + '.request.json'
    body = {'model': a.model, 'prompt': a.prompt, 'size': a.size, 'resolution': a.resolution, 'quality': a.quality, 'n': 1}
    if a.transparent:
        body.update(background='transparent', output_format='png')
    state = {'id': str(uuid.uuid4()), 'body': body, 'status': 'submitting'}
    save = lambda: open(rec, 'w').write(json.dumps(state, ensure_ascii=False, indent=1))
    save()
    try:
        r = call('POST', '/images/generations', key, body)
        task = r['data'][0]['task_id']; state.update(task_id=task, status='submitted'); save()
        t0 = time.time()
        while True:
            time.sleep(4)
            s = call('GET', f'/tasks/{task}', key)
            data = s.get('data', s)
            st = str(data.get('status', '')).lower() if isinstance(data, dict) else ''
            if st in ('completed', 'succeeded', 'success', 'done'):
                urls = find_urls(data)
                if not urls:
                    state.update(status='no_url', raw=data); save(); raise SystemExit('任务完成但没找到图片地址,见 ' + rec)
                with urlopen(urls[0], timeout=120) as resp:
                    open(a.out, 'wb').write(resp.read())
                state.update(status='completed', url=urls[0], seconds=round(time.time() - t0, 1)); save()
                print('完成', a.out, f'{state["seconds"]}s'); return
            if st in ('failed', 'error', 'cancelled', 'canceled'):
                state.update(status='failed', raw=data); save(); raise SystemExit('生成失败,见 ' + rec)
            if time.time() - t0 > 600:
                state.update(status='timeout'); save(); raise SystemExit('超时,见 ' + rec)
    except (HTTPError, URLError) as e:
        state.update(status='http_error', error=str(e)); save()
        raise SystemExit(f'请求出错:{e};见 {rec}')


if __name__ == '__main__':
    main()
