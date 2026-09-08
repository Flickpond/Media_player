"""Container sniffing.

This is the only check in the upload path that reads the file rather than a
header the client wrote, so a gap here is a gap in the whole control. The
negative cases matter as much as the positive ones: anything this function
accepts gets stored and served back under the type it reports.
"""

import pytest

from app.services.media_type import SNIFF_LENGTH, sniff_video_type


def ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x20ftyp" + brand + b"\x00\x00\x02\x00isomiso2avc1mp41"


def ebml(doctype: bytes) -> bytes:
    return b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x23B\x82\x84" + doctype + b"\x00" * 16


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (ftyp(b"isom"), "video/mp4"),
        (ftyp(b"mp42"), "video/mp4"),
        (ftyp(b"avc1"), "video/mp4"),
        (ftyp(b"qt  "), "video/quicktime"),
        (b"\x00\x00\x00\x20moov" + b"\x00" * 16, "video/quicktime"),
        (ebml(b"webm"), "video/webm"),
        (ebml(b"matroska"), "video/x-matroska"),
        (b"RIFF\x24\x00\x00\x00AVI LIST" + b"\x00" * 16, "video/x-msvideo"),
        (b"\x00\x00\x01\xba" + b"\x00" * 32, "video/mpeg"),
        (b"\x00\x00\x01\xb3" + b"\x00" * 32, "video/mpeg"),
    ],
)
def test_recognised_containers(head, expected):
    assert sniff_video_type(head) == expected


def test_mpeg_transport_stream_needs_three_sync_bytes_at_the_right_stride():
    stream = bytearray(b"\x00" * 512)
    for offset in (0, 188, 376):
        stream[offset] = 0x47

    assert sniff_video_type(bytes(stream)) == "video/mpeg"


def test_a_lone_sync_byte_is_not_a_transport_stream():
    """0x47 is the letter G; without the 188-byte stride this is just text."""
    assert sniff_video_type(b"G" + b"eneric text that is not a video at all." * 20) is None


@pytest.mark.parametrize(
    ("label", "head"),
    [
        ("pdf", b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n"),
        ("html", b"<html><body><script>alert(document.domain)</script></body></html>"),
        ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32),
        ("zip", b"PK\x03\x04" + b"\x00" * 32),
        ("elf", b"\x7fELF" + b"\x00" * 32),
        ("plain text", b"just some words in a file, nothing more"),
        ("all zeroes", b"\x00" * 64),
        ("riff that is not avi", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16),
        ("ebml with no doctype", b"\x1a\x45\xdf\xa3" + b"\x00" * 64),
    ],
)
def test_rejected_content(label, head):
    assert sniff_video_type(head) is None, label


@pytest.mark.parametrize("head", [b"", b"\x00", b"\x00\x00\x00\x20ftyp"])
def test_too_short_to_identify(head):
    """Better to refuse than to guess from a partial signature."""
    assert sniff_video_type(head) is None


def test_the_signature_must_be_at_the_front():
    """A video header buried inside another file does not make it a video."""
    assert sniff_video_type(b"%PDF-1.4\n" + ftyp(b"isom")) is None


def test_sniff_length_covers_the_transport_stream_stride():
    """The endpoint reads exactly SNIFF_LENGTH bytes, so it has to be enough
    for the widest check here -- three TS packets, 377 bytes in.
    """
    assert SNIFF_LENGTH >= 377
