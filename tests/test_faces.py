import io

import numpy as np
from PIL import Image

from app import faces


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def test_cluster_groups_similar_faces():
    rng = np.random.default_rng(0)
    a, b = _unit(rng.normal(size=128)), _unit(rng.normal(size=128))
    emb = np.stack([_unit(a + rng.normal(scale=0.03, size=128)) for _ in range(3)]
                   + [_unit(b + rng.normal(scale=0.03, size=128)) for _ in range(2)])
    rows = [{"area": 1.0} for _ in range(5)]
    labels = faces.cluster(rows, emb)
    assert labels[0] == labels[1] == labels[2]
    assert labels[3] == labels[4] != labels[0]


def test_crop_stays_inside_the_photo(monkeypatch):
    img = Image.new("RGB", (400, 300), "white")
    buf = io.BytesIO()
    img.save(buf, "WEBP")

    class Store:
        def get(self, key):
            return buf.getvalue()

    monkeypatch.setattr(faces.storage, "store", lambda: Store())
    # A face in the top-left corner: the square slides inside instead of padding with black.
    out = Image.open(io.BytesIO(faces.crop({"sha256": "x", "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.25}, size=50)))
    assert out.size == (50, 50)
    assert min(out.convert("L").getdata()) > 200
