"""按内容哈希组织原件与转码件的内容寻址存储。"""

from __future__ import annotations

import hashlib
from pathlib import Path


class BlobStore:
    """内容寻址存储：同一哈希的原件只保存一份，转码件挂在来源哈希下。"""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "originals").mkdir(parents=True, exist_ok=True)
        (self.root / "derivatives").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def put_original(self, data: bytes) -> str:
        """写入原件，返回内容哈希；相同内容天然去重。"""
        h = self.digest(data)
        path = self.root / "originals" / h
        if not path.exists():
            path.write_bytes(data)
        return h

    def put_derivative(self, source_hash: str, recipe: str, data: bytes) -> str:
        """写入转码件：derivatives/<来源哈希>/<转码配方>/<内容哈希>。"""
        h = self.digest(data)
        directory = self.root / "derivatives" / source_hash / recipe
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / h
        if not path.exists():
            path.write_bytes(data)
        return h

    def exists(self, content_hash: str) -> bool:
        return (self.root / "originals" / content_hash).exists()

    def get(self, content_hash: str) -> bytes:
        return (self.root / "originals" / content_hash).read_bytes()
