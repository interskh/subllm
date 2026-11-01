import pytest

from subllm._images import resolve_references
from subllm.errors import ClientError

PNG = b"\x89PNG\r\n\x1a\nrest"
FORMATS = {
    "a.png": PNG,
    "a.jpg": b"\xff\xd8\xff\xe0rest",
    "a.gif": b"GIF89a rest",
    "a.gif87": b"GIF87a rest",
    "a.webp": b"RIFF\x00\x00\x00\x00WEBPVP8 ",
}


@pytest.mark.parametrize("name", FORMATS)
def test_image_formats_are_accepted(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(FORMATS[name])
    assert resolve_references([f]) == [f.resolve()]


@pytest.mark.parametrize(
    "name,data",
    [("note.txt", b"hello world, not an image"), ("doc.pdf", b"%PDF-1.7 rest"), ("empty.png", b""),
     ("riff.png", b"RIFF\x00\x00\x00\x00WAVEfmt "), ("short.png", b"\x89PNG")],
)
def test_non_image_files_are_a_client_error(tmp_path, name, data):
    f = tmp_path / name
    f.write_bytes(data)
    with pytest.raises(ClientError, match="not a PNG, JPEG, WebP or GIF") as ei:
        resolve_references([f])
    assert type(ei.value) is ClientError
