import json
import sys
from pathlib import Path

import pytest

STUB = Path(__file__).parent / "fixtures" / "fake_ytdlp.py"
STUB_BIN = f"{sys.executable} {STUB}"

DEFAULT_DATA = {
    "channel": [
        {"id": "PLaaa", "title": "Sommer Mix"},
        {"id": "PLbbb", "title": "Konzert SETLIST 2024"},
    ],
    "playlists": {
        "PLaaa": [{"id": "vid00000001", "title": "One"}, {"id": "vid00000002", "title": "Two"},
                  {"id": "vid00000003", "title": "Three"}],
        "PLbbb": [{"id": "vid00000011", "title": "Song A"}, {"id": "vid00000012", "title": "Song B"}],
    },
    "fail": {},
    "slow": 0,
}


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """Returns a helper object to edit the stub data. Sets FAKE_YTDLP_DATA."""
    path = tmp_path / "fake_data.json"
    data = json.loads(json.dumps(DEFAULT_DATA))
    path.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_YTDLP_DATA", str(path))

    class Stub:
        bin = STUB_BIN
        data_ref = data

        def save(self):
            path.write_text(json.dumps(self.data_ref))

    return Stub()
