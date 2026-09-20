"""以内容哈希组织原件与转码件的只增存储。

原件路径：``objects/ab/<64位哈希>``；转码/派生件路径：
``transcodes/<64位哈希>/<profile>.bin``。同一字节重复上传只落一份，
断点续传不会产生重复作品。存储层不解释内容，只保证
"哈希即地址、写入即校验"。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size: int
    relpath: str


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.transcodes = self.root / "transcodes"
        self.tmp = self.root / "tmp"
        for d in (self.objects, self.transcodes, self.tmp):
            d.mkdir(parents=True, exist_ok=True)

    # ---- 原件 ----------------------------------------------------------

    def object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def has_object(self, digest: str) -> bool:
        return self.object_path(digest).is_file()

    def put_file(self, src: str | Path) -> StoredObject:
        """把磁盘上的已有文件纳入存储，返回其内容哈希。"""
        src = Path(src)
        digest, size = _hash_file(src)
        target = self.object_path(digest)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
        return StoredObject(digest, size, str(target.relative_to(self.root)))

    def put_bytes(self, data: bytes, declared_digest: str | None = None) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        if declared_digest and declared_digest != digest:
            raise ValueError("content hash mismatch: 客户端声明的哈希与实际内容不一致")
        target = self.object_path(digest)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return StoredObject(digest, len(data), str(target.relative_to(self.root)))

    def put_stream(self, stream, declared_digest: str | None = None) -> StoredObject:
        h = hashlib.sha256()
        tmp_path = self.tmp / f"put-{id(stream)}-{h.hexdigest()[:8]}"
        size = 0
        try:
            with tmp_path.open("wb") as out:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            digest = h.hexdigest()
            if declared_digest and declared_digest != digest:
                raise ValueError("content hash mismatch: 客户端声明的哈希与实际内容不一致")
            target = self.object_path(digest)
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_path), str(target))
            return StoredObject(digest, size, str(target.relative_to(self.root)))
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    # ---- 转码/派生件 ----------------------------------------------------

    def put_derivative(self, source_digest: str, profile: str, data: bytes) -> StoredObject:
        """转码件按"原件哈希 + 处理档位"寻址，可重复生成且不覆盖原件。"""
        ddir = self.transcodes / source_digest
        ddir.mkdir(parents=True, exist_ok=True)
        safe = profile.replace("/", "_")
        path = ddir / f"{safe}.bin"
        path.write_bytes(data)
        return StoredObject(
            hashlib.sha256(data).hexdigest(),
            len(data),
            str(path.relative_to(self.root)),
        )

    def derivatives(self, source_digest: str) -> list[str]:
        ddir = self.transcodes / source_digest
        if not ddir.is_dir():
            return []
        return sorted(p.name for p in ddir.iterdir() if p.is_file())


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
