#!/usr/bin/env python3
"""Minimal Baidu Pan client for the cold tier: chunked upload + ranged read.

Reads use dlink with HTTP Range, which the API supports (verified 206 with a
correct Content-Range), so a parquet reader can pull just the footer and the
column chunks it needs instead of the whole file. Random reads cost roughly a
second each, so callers should prefer few large seeks over many small ones.
"""
from __future__ import annotations
import hashlib, json, os, time, urllib.parse, urllib.request

TOKEN_FILE = os.path.expanduser('~/.config/feishu-relay/baidu-pan-token.json')
API = 'https://pan.baidu.com'
UPLOAD_API = 'https://d.pcs.baidu.com'
UA = 'pan.baidu.com'
SLICE = 4 * 1024 * 1024


def _token() -> str:
    with open(TOKEN_FILE) as f:
        return json.load(f)['access_token']


def _get(path: str, params: dict, base: str = API, timeout: int = 60):
    url = f"{base}{path}?{urllib.parse.urlencode({**params, 'access_token': _token()})}"
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post(path: str, params: dict, data: bytes, base: str = API, ctype: str = 'application/x-www-form-urlencoded', timeout: int = 300):
    url = f"{base}{path}?{urllib.parse.urlencode({**params, 'access_token': _token()})}"
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, 'Content-Type': ctype}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _multipart(fields: dict, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = '----panupload' + hashlib.md5(filename.encode()).hexdigest()[:12]
    out = bytearray()
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    out += b'Content-Type: application/octet-stream\r\n\r\n' + payload + b'\r\n'
    out += f'--{boundary}--\r\n'.encode()
    return bytes(out), f'multipart/form-data; boundary={boundary}'


def upload(local_path: str, pan_path: str, progress=None) -> dict:
    """precreate -> superfile2 per slice -> create."""
    size = os.path.getsize(local_path)
    with open(local_path, 'rb') as f:
        blocks, md5s = [], []
        while True:
            chunk = f.read(SLICE)
            if not chunk:
                break
            blocks.append(chunk)
            md5s.append(hashlib.md5(chunk).hexdigest())
    if not blocks:
        raise ValueError('empty file')

    pre = _post('/rest/2.0/xpan/file', {'method': 'precreate'},
                urllib.parse.urlencode({'path': pan_path, 'size': size, 'isdir': 0, 'autoinit': 1,
                                        'rtype': 3, 'block_list': json.dumps(md5s)}).encode())
    if pre.get('errno') not in (0, None):
        raise RuntimeError(f"precreate errno={pre.get('errno')}")
    uploadid = pre['uploadid']

    # A single slice failing used to abandon the whole file, which is expensive
    # when a multi-gigabyte dump is 256 slices long. Retry each slice instead;
    # 5xx from the upload host is common and usually transient.
    for i, chunk in enumerate(blocks):
        body, ctype = _multipart({}, os.path.basename(pan_path), chunk)
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                r = _post('/rest/2.0/pcs/superfile2',
                          {'method': 'upload', 'type': 'tmpfile', 'path': pan_path,
                           'uploadid': uploadid, 'partseq': i},
                          body, base=UPLOAD_API, ctype=ctype)
                if 'md5' not in r:
                    raise RuntimeError(f"slice {i}: {str(r)[:120]}")
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
        if last_error is not None:
            raise RuntimeError(f"slice {i} failed after retries: {last_error}") from last_error
        if progress and (i + 1) % 32 == 0:
            progress(i + 1, len(blocks))

    created = _post('/rest/2.0/xpan/file', {'method': 'create'},
                    urllib.parse.urlencode({'path': pan_path, 'size': size, 'isdir': 0,
                                            'rtype': 3, 'uploadid': uploadid,
                                            'block_list': json.dumps(md5s)}).encode())
    if created.get('errno') not in (0, None):
        raise RuntimeError(f"create errno={created.get('errno')}")
    return created


def stat(pan_path: str) -> dict | None:
    d = os.path.dirname(pan_path) or '/'
    name = os.path.basename(pan_path)
    start = 0
    while True:
        r = _get('/rest/2.0/xpan/file', {'method': 'list', 'dir': d, 'limit': 1000, 'start': start})
        items = r.get('list', [])
        for it in items:
            if it.get('server_filename') == name:
                return it
        if len(items) < 1000:
            return None
        start += 1000


def dlink_for(fs_id: int) -> str:
    # A freshly uploaded file can take a moment to appear in filemetas, which
    # returns an empty list rather than an error, so retry before giving up.
    last: dict | None = None
    for attempt in range(5):
        last = _get('/rest/2.0/xpan/multimedia',
                    {'method': 'filemetas', 'fsids': json.dumps([fs_id]), 'dlink': 1})
        items = last.get('list') or []
        if items and items[0].get('dlink'):
            return items[0]['dlink'] + '&access_token=' + _token()
        time.sleep(1 + attempt)
    raise RuntimeError(f'no dlink for fs_id={fs_id} after retries: {str(last)[:120]}')


class PanFile:
    """File-like object over a pan file, backed by HTTP Range requests."""

    # A range request costs about a second regardless of how few bytes it asks
    # for, so a small file is cheaper to fetch once than to seek around in.
    PREFETCH_LIMIT = 8 * 1024 * 1024

    def __init__(self, fs_id: int, size: int, prefetch: bool | None = None):
        self.fs_id, self.size, self._pos = fs_id, size, 0
        self._dlink, self._dlink_at = None, 0.0
        self.reads = 0
        self.closed = False
        self._buf: bytes | None = None
        self._prefetch = (size <= self.PREFETCH_LIMIT) if prefetch is None else prefetch

    def _link(self) -> str:
        # dlink is short lived; refresh well inside its window.
        if not self._dlink or time.time() - self._dlink_at > 1800:
            self._dlink, self._dlink_at = dlink_for(self.fs_id), time.time()
        return self._dlink

    def seek(self, pos: int, whence: int = 0) -> int:
        self._pos = pos if whence == 0 else (self._pos + pos if whence == 1 else self.size + pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def _fill(self) -> bytes:
        if self._buf is None:
            self._buf = self._fetch(0, self.size - 1)
        return self._buf

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self._pos
        if n <= 0 or self._pos >= self.size:
            return b''
        if self._prefetch:
            buf = self._fill()
            chunk = buf[self._pos:self._pos + n]
            self._pos += len(chunk)
            return chunk
        end = min(self._pos + n, self.size) - 1
        data = self._fetch(self._pos, end)
        self._pos += len(data)
        return data

    def _fetch(self, start: int, end: int) -> bytes:
        for attempt in range(2):
            req = urllib.request.Request(self._link(), headers={'User-Agent': UA, 'Range': f'bytes={start}-{end}'})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                self.reads += 1
                return data
            except Exception:
                if attempt == 1:
                    raise
                self._dlink = None  # link may have expired; force a refresh
        raise RuntimeError('unreachable')

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def open_pan(pan_path: str) -> PanFile:
    info = stat(pan_path)
    if not info:
        raise FileNotFoundError(pan_path)
    return PanFile(info['fs_id'], info['size'])
