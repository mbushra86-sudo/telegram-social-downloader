"""Telegram video downloader for public social media links."""
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Thread
from urllib.parse import urlsplit

import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('bot')
TOKEN = os.getenv('BOT_TOKEN', '').strip()
BASE_URL = (os.getenv('RENDER_EXTERNAL_URL') or os.getenv('BASE_URL') or '').rstrip('/')
OWNER_ID = os.getenv('OWNER_TELEGRAM_ID', '').strip()
SECRET = os.getenv('WEBHOOK_SECRET', '').strip() or hashlib.sha256(('webhook:' + TOKEN).encode()).hexdigest()
PORT = int(os.getenv('PORT', '10000'))
MAX_BYTES = 45 * 1024 * 1024
URL_RE = re.compile(r'https?://[^\s<>\"\']+', re.I)
HOSTS = ('instagram.com', 'tiktok.com', 'youtube.com', 'youtu.be', 'facebook.com', 'fb.watch', 'x.com', 'twitter.com')
EXECUTOR = ThreadPoolExecutor(max_workers=2)
SLOTS = BoundedSemaphore(2)


def find_url(text):
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip('.,!?)،؛')
        try:
            u = urlsplit(url)
            h = (u.hostname or '').lower().rstrip('.')
            if u.scheme in ('http', 'https') and any(h == domain or h.endswith('.' + domain) for domain in HOSTS):
                return url
        except ValueError:
            pass
    return None


def api(method, data, files=None, timeout=25):
    url = f'https://api.telegram.org/bot{TOKEN}/{method}'
    response = requests.post(url, json=data if files is None else None,
                             data=data if files is not None else None,
                             files=files, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError('Telegram API rejected the request')
    return result


def send(chat_id, text):
    try:
        api('sendMessage', {'chat_id': chat_id, 'text': text})
    except Exception as exc:
        log.warning('send failed: %s', type(exc).__name__)


def download(chat_id, url):
    try:
        from yt_dlp import YoutubeDL
        with tempfile.TemporaryDirectory() as folder:
            def progress(status):
                if status.get('status') == 'downloading' and status.get('downloaded_bytes', 0) > MAX_BYTES:
                    raise ValueError('too_large')
            opts = {
                'format': 'best[ext=mp4][height<=720]/best[height<=720]/best',
                'outtmpl': str(Path(folder) / 'video.%(ext)s'),
                'noplaylist': True, 'playlist_items': '1', 'max_filesize': MAX_BYTES,
                'progress_hooks': [progress], 'socket_timeout': 20,
                'retries': 2, 'fragment_retries': 2,
                'quiet': True, 'no_warnings': True,
            }
            # TikTok sometimes rejects the default request fingerprint.
            # Try alternate user agents that have resolved this yt-dlp error.
            is_tiktok = 'tiktok.com' in (urlsplit(url).hostname or '').lower()
            agents = [None]
            if is_tiktok:
                agents += [
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 OPR/118.0.0.0',
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
                ]
            last_error = None
            for agent in agents:
                attempt = dict(opts)
                if agent:
                    attempt['http_headers'] = {
                        'User-Agent': agent,
                        'Referer': 'https://www.tiktok.com/',
                    }
                try:
                    with YoutubeDL(attempt) as dl:
                        dl.extract_info(url, download=True)
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    if not is_tiktok:
                        raise
                    log.warning('TikTok extraction attempt failed: %s', type(error).__name__)
            if last_error is not None:
                raise last_error
            candidates = [p for p in Path(folder).iterdir() if p.is_file() and not p.name.endswith(('.part', '.ytdl'))]
            if not candidates:
                raise RuntimeError('no_file')
            file = max(candidates, key=lambda p: p.stat().st_size)
            if file.stat().st_size > MAX_BYTES:
                raise ValueError('too_large')
            is_mp4 = file.suffix.lower() == '.mp4'
            with file.open('rb') as stream:
                api('sendVideo' if is_mp4 else 'sendDocument',
                    {'chat_id': chat_id, 'caption': '✅ الفيديو جاهز'},
                    files={('video' if is_mp4 else 'document'): (file.name, stream)},
                    timeout=180)
    except ValueError:
        send(chat_id, 'الفيديو كبير، الحد الأقصى 45 ميغابايت. جرب فيديو أقصر.')
    except Exception as exc:
        log.warning('download failed: %s', type(exc).__name__)
        send(chat_id, 'ما قدرت أنزل الفيديو. اتأكد إن الرابط عام ومتاح بدون تسجيل دخول. بعض المنصات بتمنع التنزيل من السيرفرات المجانية.')


def process(update):
    msg = update.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    user_id = (msg.get('from') or {}).get('id')
    text = (msg.get('text') or msg.get('caption') or '').strip()
    if not chat_id or not text:
        return
    cmd = text.split()[0].split('@')[0].lower()
    if cmd == '/id':
        send(chat_id, f'رقم حسابك: {user_id}')
    elif OWNER_ID and str(user_id) != OWNER_ID:
        send(chat_id, 'البوت مخصص لصاحبه فقط.')
    elif cmd in ('/start', '/help'):
        send(chat_id, 'أهلاً 👋 أرسل رابط فيديو عام من Instagram أو TikTok أو YouTube أو Facebook أو X. بدون متابعة قنوات. /id لمعرفة رقم حسابك.')
    else:
        url = find_url(text)
        if not url:
            send(chat_id, 'أرسل رابط مباشر لفيديو عام من منصة مدعومة.')
        else:
            send(chat_id, '⏳ جاري تحميل الفيديو...')
            download(chat_id, url)


def enqueue(update):
    if not SLOTS.acquire(blocking=False):
        return False
    def run():
        try:
            process(update)
        finally:
            SLOTS.release()
    try:
        EXECUTOR.submit(run)
        return True
    except Exception:
        SLOTS.release()
        raise


def set_webhook():
    if not TOKEN or not BASE_URL:
        log.warning('BOT_TOKEN or public URL not yet configured')
        return
    try:
        api('setWebhook', {'url': BASE_URL + '/telegram', 'secret_token': SECRET,
                           'allowed_updates': ['message'], 'max_connections': 2})
        log.info('webhook configured')
    except Exception as exc:
        log.warning('webhook setup failed: %s', type(exc).__name__)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.respond(200, {'status': 'ok', 'configured': bool(TOKEN)}) if urlsplit(self.path).path in ('/', '/health') else self.respond(404, {'error': 'not found'})

    def do_POST(self):
        if urlsplit(self.path).path != '/telegram':
            return self.respond(404, {'error': 'not found'})
        if not TOKEN:
            return self.respond(503, {'error': 'unconfigured'})
        if not hmac.compare_digest(self.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), SECRET):
            return self.respond(403, {'error': 'forbidden'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 131072:
                return self.respond(413, {'error': 'bad size'})
            update = json.loads(self.rfile.read(size))
            if not isinstance(update, dict):
                raise ValueError('not an object')
        except (ValueError, json.JSONDecodeError):
            return self.respond(400, {'error': 'bad json'})
        return self.respond(200, {'ok': True}) if enqueue(update) else self.respond(503, {'error': 'busy'})


if __name__ == '__main__':
    Thread(target=set_webhook, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
