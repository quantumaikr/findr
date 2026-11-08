"""Icons, sprites and macOS '._' files are not photos: no OCR, no photo vector (they stay findable by name)."""
import os

from PIL import Image

from findr.core.index import asset_like


def _img(path, size, noise=True):
    import random
    im = Image.new("RGB", size)
    if noise:   # a real-looking file size (a flat image compresses to almost nothing)
        R = random.Random(0)
        im.putdata([(R.randint(0, 255), R.randint(0, 255), R.randint(0, 255)) for _ in range(size[0] * size[1])])
    im.save(path)
    return path, os.path.getsize(path)


def test_macos_metadata_and_tiny_files(tmp_path):
    p = tmp_path / "._IMG_0001.jpg"; p.write_bytes(b"\0" * 4096)
    assert asset_like(str(p), 4096)
    assert asset_like(str(tmp_path / "x.png"), 900)   # under 2 KB: never a photo


def test_icon_is_an_asset_photo_is_not(tmp_path):
    icon, s1 = _img(str(tmp_path / "icon.png"), (64, 64))
    photo, s2 = _img(str(tmp_path / "photo.jpg"), (400, 300))
    assert asset_like(icon, max(s1, 4096))
    assert not asset_like(photo, max(s2, 4096))


def test_large_file_is_not_opened(tmp_path):
    assert not asset_like(str(tmp_path / "missing_big.jpg"), 2_000_000)   # big: a photo, the header is not read
    assert not asset_like(str(tmp_path / "missing.jpg"), 50_000)          # unreadable: kept (not hidden for that)
