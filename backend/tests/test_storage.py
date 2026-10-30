import hashlib
from pathlib import Path

from trackrecord.ingest.storage import RawStore


def test_put_is_content_addressed_and_compressed(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    content = b"<html>" + b"press release " * 1000 + b"</html>"

    sha, relative = store.put(content, ".htm")

    assert sha == hashlib.sha256(content).hexdigest()
    assert relative == f"{sha[:2]}/{sha}.htm.gz"
    assert (tmp_path / relative).stat().st_size < len(content) / 10
    assert store.read(relative) == content


def test_put_same_content_twice_is_a_no_op(tmp_path: Path) -> None:
    store = RawStore(tmp_path)

    first = store.put(b"same", ".htm")
    second = store.put(b"same", ".htm")

    assert first == second
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == [Path(first[1]).name]
