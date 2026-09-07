"""Work out what an uploaded file actually is, from its bytes.

The multipart `Content-Type` is a header the client writes, and browsers derive
it from the file extension -- so a PDF renamed to `.mp4` arrives declared as
`video/mp4` and no header check can catch it. The only trustworthy signal is
the content, so this module reads the container signature and reports what the
file really is. The upload endpoint stores that answer instead of the client's.

This proves the *container*, not that the stream decodes. A file can carry a
valid MP4 header and still be unplayable; catching that needs a real demux,
which is FFmpeg's job when it replaces the copy step in sprint 2.
"""

# Enough to cover an EBML header and the first three MPEG-TS packets. Read from
# the front of the file only -- container signatures all live there, and the
# whole point is to decide before touching the rest.
SNIFF_LENGTH = 4096

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
# The DocType that separates WebM from Matroska sits inside the EBML header,
# a few dozen bytes in.
_EBML_DOCTYPE_WINDOW = 256

# ISO base media file format: a four-byte box type at offset 4. `ftyp` is the
# modern form and names a brand; the others are the older QuickTime layout,
# which has no brand to read.
_ISO_BMFF_LEGACY_BOXES = frozenset({b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot"})
_QUICKTIME_BRANDS = frozenset({b"qt  "})

_MPEG_START_CODES = frozenset({b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"})

# MPEG-TS has no magic number: it is a stream of 188-byte packets that each
# open with 0x47. Three in a row at the right stride is the accepted test.
_TS_SYNC_BYTE = 0x47
_TS_PACKET_OFFSETS = (0, 188, 376)


def _looks_like_mpeg_ts(head: bytes) -> bool:
    if len(head) <= _TS_PACKET_OFFSETS[-1]:
        return False
    return all(head[offset] == _TS_SYNC_BYTE for offset in _TS_PACKET_OFFSETS)


def sniff_video_type(head: bytes) -> str | None:
    """Return the media type these bytes really are, or None if not a video.

    `head` is the first few KB of the file. Returning None means "nothing here
    matches a container we accept" -- the caller rejects rather than guessing.
    """
    if len(head) < 12:
        return None

    if head[:4] == _EBML_MAGIC:
        window = head[:_EBML_DOCTYPE_WINDOW]
        if b"webm" in window:
            return "video/webm"
        if b"matroska" in window:
            return "video/x-matroska"
        # EBML is a generic container; without a DocType we cannot claim it is
        # video at all.
        return None

    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video/x-msvideo"

    box = head[4:8]
    if box == b"ftyp":
        return "video/quicktime" if head[8:12] in _QUICKTIME_BRANDS else "video/mp4"
    if box in _ISO_BMFF_LEGACY_BOXES:
        return "video/quicktime"

    if head[:4] in _MPEG_START_CODES:
        return "video/mpeg"
    if _looks_like_mpeg_ts(head):
        return "video/mpeg"

    return None
