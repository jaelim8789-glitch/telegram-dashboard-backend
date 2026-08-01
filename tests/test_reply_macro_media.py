import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.delivery import normalize_media_paths


def test_normalize_media_paths_handles_single_and_multiple_files():
    assert normalize_media_paths(None) == []
    assert normalize_media_paths("/tmp/file.jpg") == ["/tmp/file.jpg"]
    assert normalize_media_paths('["/tmp/a.jpg", "/tmp/b.mp4"]') == ["/tmp/a.jpg", "/tmp/b.mp4"]
