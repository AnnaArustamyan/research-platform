import gzip
import hashlib
import os
import uuid
from pathlib import Path


class RawStore:
    """Content-addressed, gzip-compressed store for downloaded documents.

    Files live at `<root>/<sha[:2]>/<sha><suffix>.gz`, where `sha` is the SHA-256
    of the uncompressed content, so re-downloading identical content is a no-op
    and parsers can re-run without network access. Filing HTML compresses about
    10x, which matters for inline-XBRL 10-Ks.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, content: bytes, suffix: str) -> tuple[str, str]:
        """Store `content` and return its (sha256, path relative to root)."""
        sha = hashlib.sha256(content).hexdigest()
        relative = f"{sha[:2]}/{sha}{suffix}.gz"
        path = self.root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(gzip.compress(content, mtime=0))
            os.replace(tmp, path)
        return sha, relative

    def read(self, relative: str) -> bytes:
        return gzip.decompress((self.root / relative).read_bytes())
