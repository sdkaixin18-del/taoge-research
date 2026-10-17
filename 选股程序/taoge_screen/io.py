"""Canonical JSON and immutable, atomic research snapshots."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False)+'\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    """Publish only complete bytes; an existing different snapshot is an error."""
    path = Path(path)
    body = encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise ValueError(f'拒绝覆盖已有快照，请换输出路径：{path}')
    finally:
        os.unlink(temp)


def seal(value):
    value = dict(value)
    value['content_sha256'] = digest(value)
    return value


def verify(value):
    body = {k:v for k,v in value.items() if k != 'content_sha256'}
    if value.get('content_sha256') != digest(body):
        raise ValueError('快照内容或哈希已变化，拒绝继续验证')
