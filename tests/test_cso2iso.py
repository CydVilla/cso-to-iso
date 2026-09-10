"""Tests for cso2iso.

The suite builds CSO/ZSO images in memory with a miniature compressor and then
checks that converting them back reproduces the original bytes exactly.
"""

import io
import random
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cso2iso
from cso2iso import CorruptImage, CsoError, UnsupportedFormat


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def deflate(chunk):
    engine = zlib.compressobj(9, zlib.DEFLATED, -15)
    return engine.compress(chunk) + engine.flush()


def lz4_literals(chunk):
    """Encode a chunk as a single LZ4 literal run, which is a valid block."""
    out = bytearray()
    count = len(chunk)
    if count < 15:
        out.append(count << 4)
    else:
        out.append(0xF0)
        remaining = count - 15
        while remaining >= 255:
            out.append(255)
            remaining -= 255
        out.append(remaining)
    out += chunk
    return bytes(out)


def make_cso(data, block_size=2048, align=0, magic=b"CISO", version=1,
             codec="deflate", always_compress=False):
    """Build a CSO/ZSO image out of ``data``."""
    packer = deflate if codec == "deflate" else lz4_literals
    num_blocks = (len(data) + block_size - 1) // block_size
    index_size = (num_blocks + 1) * 4
    base = cso2iso.HEADER_SIZE + index_size
    step = 1 << align

    body = bytearray()
    entries = []
    for i in range(num_blocks):
        chunk = data[i * block_size:(i + 1) * block_size]
        packed = packer(chunk)
        plain = len(packed) >= len(chunk) and not always_compress
        position = base + len(body)
        padding = -position % step
        body += b"\x00" * padding
        position += padding
        entries.append((position >> align) | (cso2iso.PLAIN_FLAG if plain else 0))
        body += chunk if plain else packed

    tail = base + len(body)
    body += b"\x00" * (-tail % step)
    entries.append((base + len(body)) >> align)

    header = struct.pack(
        "<4sIQIBBH", magic, cso2iso.HEADER_SIZE, len(data), block_size, version, align, 0
    )
    return header + struct.pack("<%dI" % len(entries), *entries) + bytes(body)


def sample_data(size, seed=1234):
    """Data that is part compressible and part incompressible."""
    rng = random.Random(seed)
    out = bytearray()
    while len(out) < size:
        if rng.random() < 0.5:
            out += bytes([rng.randrange(256)]) * rng.randrange(500, 4000)
        else:
            out += bytes(rng.randrange(256) for _ in range(rng.randrange(500, 4000)))
    return bytes(out[:size])


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, payload):
        path = self.tmp / name
        path.write_bytes(payload)
        return path

    def roundtrip(self, data, **kwargs):
        source = self.write("image.cso", make_cso(data, **kwargs))
        destination = self.tmp / "image.iso"
        result = cso2iso.convert(source, destination)
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(result.iso_size, len(data))
        return result


# --------------------------------------------------------------------------
# format decoding
# --------------------------------------------------------------------------


class TestRoundTrip(TempDirCase):
    def test_deflate_blocks(self):
        self.roundtrip(sample_data(2048 * 40 + 517))

    def test_partial_final_block(self):
        self.roundtrip(sample_data(2048 * 3 + 1))

    def test_single_short_block(self):
        self.roundtrip(b"hello world")

    def test_incompressible_data_uses_plain_blocks(self):
        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(2048 * 8))
        image = make_cso(data)
        source = self.write("plain.cso", image)
        index = struct.unpack_from("<9I", image, cso2iso.HEADER_SIZE)
        self.assertTrue(any(entry & cso2iso.PLAIN_FLAG for entry in index[:-1]))
        cso2iso.convert(source, self.tmp / "plain.iso")
        self.assertEqual((self.tmp / "plain.iso").read_bytes(), data)

    def test_index_alignment(self):
        for align in (1, 2, 8):
            with self.subTest(align=align):
                data = sample_data(2048 * 12 + 9, seed=align)
                source = self.write("a%d.cso" % align, make_cso(data, align=align))
                destination = self.tmp / ("a%d.iso" % align)
                cso2iso.convert(source, destination)
                self.assertEqual(destination.read_bytes(), data)

    def test_large_block_size(self):
        self.roundtrip(sample_data(65536 * 5 + 33), block_size=65536)

    def test_ziso_lz4_blocks(self):
        self.roundtrip(
            sample_data(2048 * 20 + 3),
            magic=b"ZISO",
            codec="lz4",
            always_compress=True,
        )

    def test_ziso_with_index_alignment(self):
        # LZ4 has no end-of-stream marker, so the padding that aligns the next
        # block must not be decoded as if it were more data.
        for align in (1, 2, 8):
            with self.subTest(align=align):
                data = sample_data(2048 * 15 + 61, seed=align + 50)
                source = self.write("z%d.zso" % align, make_cso(
                    data, magic=b"ZISO", codec="lz4", always_compress=True, align=align))
                destination = self.tmp / ("z%d.iso" % align)
                cso2iso.convert(source, destination)
                self.assertEqual(destination.read_bytes(), data)

    def test_cso_v2_lz4_with_index_alignment(self):
        data = sample_data(2048 * 15 + 61, seed=77)
        source = self.write("v2a.cso", make_cso(
            data, version=2, codec="lz4", always_compress=True, align=4))
        destination = self.tmp / "v2a.iso"
        cso2iso.convert(source, destination)
        self.assertEqual(destination.read_bytes(), data)

    def test_cso_v2_lz4_blocks(self):
        self.roundtrip(
            sample_data(2048 * 20 + 3),
            version=2,
            codec="lz4",
            always_compress=True,
        )

    def test_cso_v2_deflate_blocks(self):
        self.roundtrip(sample_data(2048 * 20 + 3), version=2, always_compress=True)

    def test_reports_ratio_and_header(self):
        result = self.roundtrip(sample_data(2048 * 30))
        self.assertEqual(result.header.block_size, 2048)
        self.assertEqual(result.header.num_blocks, 30)
        self.assertEqual(result.header.format_name, "CISO v1 (deflate)")
        self.assertGreater(result.ratio, 0)


class TestLz4Decoder(unittest.TestCase):
    def test_literal_only_block(self):
        self.assertEqual(cso2iso.lz4_decompress_block(lz4_literals(b"abc")), b"abc")

    def test_long_literal_run_uses_extension_bytes(self):
        payload = bytes(range(256)) * 3
        self.assertEqual(cso2iso.lz4_decompress_block(lz4_literals(payload)), payload)

    def test_match_copy(self):
        # token 0x1B: one literal, match length 11 + 4; offset 1 repeats the "A"
        block = bytes([0x1B]) + b"A" + bytes([0x01, 0x00])
        self.assertEqual(cso2iso.lz4_decompress_block(block), b"A" * 16)

    def test_match_then_trailing_literals(self):
        block = bytes([0x1B]) + b"A" + bytes([0x01, 0x00]) + bytes([0x50]) + b"BCDEF"
        self.assertEqual(cso2iso.lz4_decompress_block(block), b"A" * 16 + b"BCDEF")

    def test_non_overlapping_match(self):
        # 8 literals "abcdefgh", then copy 4 bytes from offset 8
        block = bytes([0x80]) + b"abcdefgh" + bytes([0x08, 0x00])
        self.assertEqual(cso2iso.lz4_decompress_block(block), b"abcdefghabcd")

    def test_extended_match_length(self):
        # match length nibble 15 plus one extension byte of 6 -> 15 + 6 + 4 = 25
        block = bytes([0x1F]) + b"Z" + bytes([0x01, 0x00]) + bytes([0x06])
        self.assertEqual(cso2iso.lz4_decompress_block(block), b"Z" * 26)

    def test_limit_stops_before_trailing_padding(self):
        block = lz4_literals(b"payload!") + b"\x00" * 7
        self.assertEqual(cso2iso.lz4_decompress_block(block, 8), b"payload!")

    def test_limit_stops_after_a_match(self):
        block = bytes([0x1B]) + b"A" + bytes([0x01, 0x00]) + b"\x00" * 3
        self.assertEqual(cso2iso.lz4_decompress_block(block, 16), b"A" * 16)

    def test_padding_without_a_limit_is_an_error(self):
        with self.assertRaises(CorruptImage):
            cso2iso.lz4_decompress_block(lz4_literals(b"payload!") + b"\x00" * 7)

    def test_rejects_bad_offset(self):
        with self.assertRaises(CorruptImage):
            cso2iso.lz4_decompress_block(bytes([0x10]) + b"A" + bytes([0x09, 0x00]))

    def test_rejects_truncated_literals(self):
        with self.assertRaises(CorruptImage):
            cso2iso.lz4_decompress_block(bytes([0x50]) + b"AB")


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------


class TestRejectsBadInput(TempDirCase):
    def test_file_too_small(self):
        with self.assertRaises(UnsupportedFormat):
            cso2iso.read_header(self.write("tiny.cso", b"CISO"))

    def test_wrong_magic(self):
        payload = b"NOPE" + b"\x00" * 40
        with self.assertRaisesRegex(UnsupportedFormat, "not a CSO/ZSO image"):
            cso2iso.read_header(self.write("bad.cso", payload))

    def test_dax_image_is_named(self):
        payload = b"DAX\x00" + b"\x00" * 40
        with self.assertRaisesRegex(UnsupportedFormat, "DAX"):
            cso2iso.read_header(self.write("game.dax", payload))

    def test_zero_block_size(self):
        payload = struct.pack("<4sIQIBBH", b"CISO", 24, 4096, 0, 1, 0, 0)
        with self.assertRaisesRegex(CorruptImage, "block size"):
            cso2iso.read_header(self.write("zero.cso", payload))

    def test_empty_image(self):
        payload = struct.pack("<4sIQIBBH", b"CISO", 24, 0, 2048, 1, 0, 0)
        with self.assertRaisesRegex(CorruptImage, "no data"):
            cso2iso.read_header(self.write("empty.cso", payload))

    def test_future_version(self):
        payload = struct.pack("<4sIQIBBH", b"CISO", 24, 4096, 2048, 9, 0, 0)
        with self.assertRaisesRegex(UnsupportedFormat, "version"):
            cso2iso.read_header(self.write("v9.cso", payload))

    def test_truncated_index(self):
        image = make_cso(sample_data(2048 * 10))
        source = self.write("cut.cso", image[:cso2iso.HEADER_SIZE + 8])
        with self.assertRaisesRegex(CorruptImage, "index table is truncated"):
            cso2iso.convert(source, self.tmp / "cut.iso")

    def test_truncated_body(self):
        image = make_cso(sample_data(2048 * 10))
        source = self.write("short.cso", image[:-200])
        with self.assertRaises(CorruptImage):
            cso2iso.convert(source, self.tmp / "short.iso")

    def test_corrupt_block_is_reported(self):
        image = bytearray(make_cso(sample_data(2048 * 10), always_compress=True))
        offset = cso2iso.HEADER_SIZE + 5 * 4
        start = struct.unpack_from("<I", image, offset)[0] & cso2iso.POSITION_MASK
        image[start:start + 16] = b"\xff" * 16
        source = self.write("rot.cso", bytes(image))
        with self.assertRaises(CorruptImage):
            cso2iso.convert(source, self.tmp / "rot.iso")

    def test_failed_conversion_leaves_no_output(self):
        source = self.write("short.cso", make_cso(sample_data(2048 * 10))[:-200])
        destination = self.tmp / "short.iso"
        with self.assertRaises(CorruptImage):
            cso2iso.convert(source, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name("short.iso.part").exists())

    def test_refuses_to_overwrite(self):
        source = self.write("dup.cso", make_cso(b"hello"))
        destination = self.write("dup.iso", b"do not clobber me")
        with self.assertRaisesRegex(CsoError, "already exists"):
            cso2iso.convert(source, destination)
        self.assertEqual(destination.read_bytes(), b"do not clobber me")
        cso2iso.convert(source, destination, force=True)
        self.assertEqual(destination.read_bytes(), b"hello")

    def test_refuses_same_path(self):
        source = self.write("same.cso", make_cso(b"hello"))
        with self.assertRaisesRegex(CsoError, "same file"):
            cso2iso.convert(source, source)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


class TestCommandLine(TempDirCase):
    def setUp(self):
        super().setUp()
        self._owns = cso2iso._owns_console
        cso2iso._owns_console = lambda: False
        self.addCleanup(lambda: setattr(cso2iso, "_owns_console", self._owns))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cso2iso.main([str(a) for a in argv])
        finally:
            sys.stdout, sys.stderr = saved
        return code, out.getvalue(), err.getvalue()

    def test_converts_next_to_the_source(self):
        data = sample_data(2048 * 6)
        source = self.write("game.cso", make_cso(data))
        code, out, _ = self.run_cli(source)
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "game.iso").read_bytes(), data)
        self.assertIn("game.cso", out)

    def test_explicit_output_name(self):
        data = sample_data(2048 * 4)
        source = self.write("game.cso", make_cso(data))
        target = self.tmp / "nested" / "custom.iso"
        code, _, _ = self.run_cli(source, "-o", target)
        self.assertEqual(code, 0)
        self.assertEqual(target.read_bytes(), data)

    def test_several_inputs_into_one_directory(self):
        first = self.write("one.cso", make_cso(b"first image"))
        second = self.write("two.zso", make_cso(b"second image", magic=b"ZISO",
                                                codec="lz4", always_compress=True))
        out_dir = self.tmp / "isos"
        code, _, _ = self.run_cli(first, second, "-o", out_dir)
        self.assertEqual(code, 0)
        self.assertEqual((out_dir / "one.iso").read_bytes(), b"first image")
        self.assertEqual((out_dir / "two.iso").read_bytes(), b"second image")

    def test_info_does_not_write_anything(self):
        source = self.write("game.cso", make_cso(sample_data(2048 * 5)))
        code, out, _ = self.run_cli("--info", source)
        self.assertEqual(code, 0)
        self.assertIn("CISO v1 (deflate)", out)
        self.assertIn("block size", out)
        self.assertFalse((self.tmp / "game.iso").exists())

    def test_missing_file_reports_and_exits_nonzero(self):
        code, _, err = self.run_cli(self.tmp / "ghost.cso")
        self.assertEqual(code, 1)
        self.assertIn("no such file", err)

    def test_existing_output_needs_force(self):
        source = self.write("game.cso", make_cso(b"payload"))
        self.write("game.iso", b"older")
        code, _, err = self.run_cli(source)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        code, _, _ = self.run_cli(source, "--force")
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "game.iso").read_bytes(), b"payload")

    def test_one_bad_input_does_not_stop_the_others(self):
        good = self.write("good.cso", make_cso(b"fine"))
        bad = self.write("bad.cso", b"NOPE" + b"\x00" * 40)
        code, _, err = self.run_cli(bad, good, "-o", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("bad.cso", err)
        self.assertEqual((self.tmp / "out" / "good.iso").read_bytes(), b"fine")

    def test_quiet_prints_nothing_on_success(self):
        source = self.write("game.cso", make_cso(b"payload"))
        code, out, err = self.run_cli(source, "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_no_arguments_shows_help(self):
        code, out, _ = self.run_cli("--no-gui")
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)


class TestFormatting(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(cso2iso.human_bytes(512), "512 B")
        self.assertEqual(cso2iso.human_bytes(1536), "1.5 KiB")
        self.assertEqual(cso2iso.human_bytes(1024 ** 3), "1.0 GiB")

    def test_human_time(self):
        self.assertEqual(cso2iso.human_time(45), "0:45")
        self.assertEqual(cso2iso.human_time(90), "1:30")
        self.assertEqual(cso2iso.human_time(3725), "1:02:05")


if __name__ == "__main__":
    unittest.main()
