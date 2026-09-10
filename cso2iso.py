#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Convert compressed disc images (.cso / .zso / .ciso) back into plain .iso files.

Supported inputs:

* CISO v0 / v1 -- deflate compressed blocks. This is what almost every tool
  produces and what PSP/PS2 CSO images use.
* CISO v2      -- blocks may be deflate or LZ4.
* ZISO / ZSO   -- LZ4 compressed blocks.

There are no third party dependencies. If the ``lz4`` package happens to be
installed it is used for LZ4 blocks because the C implementation is faster,
otherwise the small pure Python decoder in this file does the work.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

__version__ = "1.0.0"
__all__ = [
    "CsoError",
    "UnsupportedFormat",
    "CorruptImage",
    "CsoHeader",
    "ConversionResult",
    "read_header",
    "convert",
    "main",
]

CISO_MAGIC = b"CISO"
ZISO_MAGIC = b"ZISO"
DAX_MAGIC = b"DAX\x00"

HEADER_SIZE = 24
PLAIN_FLAG = 0x80000000
POSITION_MASK = 0x7FFFFFFF
MAX_BLOCK_SIZE = 64 * 1024 * 1024
IO_BUFFER = 1 << 20
PROGRESS_EVERY = 128
MAX_LENGTH_PROBES = 32

try:  # optional speed-up, never required
    from lz4.block import decompress as _lz4_native  # type: ignore
except Exception:  # pragma: no cover - depends on the environment
    _lz4_native = None

_NATIVE_INDEX = array("I").itemsize == 4


class CsoError(Exception):
    """Base class for every error raised by this module."""


class UnsupportedFormat(CsoError):
    """The file is not a CSO/ZSO image, or uses a variant we cannot read."""


class CorruptImage(CsoError):
    """The file looks like a CSO/ZSO image but its contents do not add up."""


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CsoHeader:
    """The 24 byte header that starts every CSO/ZSO image."""

    magic: bytes
    header_size: int
    total_bytes: int
    block_size: int
    version: int
    align: int

    @property
    def num_blocks(self) -> int:
        return (self.total_bytes + self.block_size - 1) // self.block_size

    @property
    def index_size(self) -> int:
        return (self.num_blocks + 1) * 4

    @property
    def codec(self) -> str:
        if self.magic == ZISO_MAGIC:
            return "LZ4"
        return "deflate or LZ4" if self.version >= 2 else "deflate"

    @property
    def format_name(self) -> str:
        name = "ZISO" if self.magic == ZISO_MAGIC else "CISO"
        return "%s v%d (%s)" % (name, self.version, self.codec)


def parse_header(raw: bytes) -> CsoHeader:
    """Turn the first 24 bytes of an image into a :class:`CsoHeader`."""
    if len(raw) < HEADER_SIZE:
        raise UnsupportedFormat("file is too small to be a compressed disc image")

    magic = bytes(raw[:4])
    if magic == DAX_MAGIC:
        raise UnsupportedFormat("this is a DAX image, which cso2iso cannot read")
    if magic not in (CISO_MAGIC, ZISO_MAGIC):
        raise UnsupportedFormat(
            "not a CSO/ZSO image: expected magic 'CISO' or 'ZISO', found %r" % magic
        )

    header_size, total_bytes, block_size, version, align = struct.unpack_from(
        "<IQIBB", raw, 4
    )
    if header_size == 0:
        header_size = HEADER_SIZE
    if header_size < HEADER_SIZE:
        raise CorruptImage("header claims to be only %d bytes long" % header_size)
    if version > 2:
        raise UnsupportedFormat("unsupported format version %d" % version)
    if block_size == 0 or block_size > MAX_BLOCK_SIZE:
        raise CorruptImage("implausible block size of %d bytes" % block_size)
    if align > 31:
        raise CorruptImage("implausible index alignment of %d" % align)
    if total_bytes == 0:
        raise CorruptImage("header says the image contains no data")

    return CsoHeader(magic, header_size, total_bytes, block_size, version, align)


def read_header(source) -> CsoHeader:
    """Read the header of ``source``, which may be a path or an open file."""
    if hasattr(source, "read"):
        return parse_header(source.read(HEADER_SIZE))
    with open(source, "rb") as fh:
        return parse_header(fh.read(HEADER_SIZE))


def _read_index(fh, header: CsoHeader):
    fh.seek(header.header_size)
    raw = fh.read(header.index_size)
    if len(raw) != header.index_size:
        raise CorruptImage(
            "index table is truncated: wanted %d bytes, got %d"
            % (header.index_size, len(raw))
        )
    if _NATIVE_INDEX:
        index = array("I")
        index.frombytes(raw)
        if sys.byteorder == "big":
            index.byteswap()
        return index
    return struct.unpack("<%dI" % (header.num_blocks + 1), raw)


# --------------------------------------------------------------------------
# block decoders
# --------------------------------------------------------------------------


def lz4_decompress_block(src: bytes, limit: Optional[int] = None) -> bytes:
    """Decode one LZ4 *block* (the raw format, without a frame header).

    ``limit`` stops the decoder as soon as that many bytes exist. That is what
    makes it safe to hand over a block that still carries the zero padding used
    to align the next block: LZ4 has no end-of-stream marker, so without a limit
    the padding would be decoded as though it were more compressed data.
    """
    out = bytearray()
    pos = 0
    end = len(src)
    while pos < end:
        token = src[pos]
        pos += 1

        literals = token >> 4
        if literals == 15:
            while True:
                if pos >= end:
                    raise CorruptImage("truncated LZ4 literal length")
                extra = src[pos]
                pos += 1
                literals += extra
                if extra != 0xFF:
                    break
        if literals:
            stop = pos + literals
            if stop > end:
                raise CorruptImage("truncated LZ4 literal run")
            out += src[pos:stop]
            pos = stop
        if pos == end or (limit is not None and len(out) >= limit):
            break

        if pos + 2 > end:
            raise CorruptImage("truncated LZ4 match offset")
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise CorruptImage("invalid LZ4 match offset %d" % offset)

        length = token & 0x0F
        if length == 15:
            while True:
                if pos >= end:
                    raise CorruptImage("truncated LZ4 match length")
                extra = src[pos]
                pos += 1
                length += extra
                if extra != 0xFF:
                    break
        length += 4

        start = len(out) - offset
        if offset >= length:
            out += out[start : start + length]
        else:  # overlapping copy, byte by byte
            for i in range(length):
                out.append(out[start + i])
    return bytes(out)


def _inflate(payload: bytes, limit: int) -> bytes:
    obj = zlib.decompressobj(-15)
    try:
        return obj.decompress(payload, limit)
    except zlib.error as exc:
        raise CorruptImage("deflate error: %s" % exc)


def _unlz4(payload: bytes, block_size: int, wanted: int, allowance: int) -> bytes:
    """Decode an LZ4 block that may still have alignment padding attached.

    The C library is faster but insists on an exact compressed length, which the
    index does not record: all we know is that the real length is within
    ``allowance`` bytes of the stored extent. Candidates are therefore tried
    longest first, and because that decoder only reports success when it has
    consumed the whole input, the first length that works is the real one. The
    pure Python decoder needs no such help and has the final say.
    """
    if _lz4_native is not None:
        for trim in range(min(allowance, MAX_LENGTH_PROBES) + 1):
            candidate = payload[: len(payload) - trim] if trim else payload
            if not candidate:
                break
            for size in (block_size, wanted):
                try:
                    return _lz4_native(candidate, uncompressed_size=size)
                except Exception:
                    continue
    return lz4_decompress_block(payload, wanted)


def _decode_block(payload: bytes, header: CsoHeader, wanted: int) -> bytes:
    block_size = header.block_size
    allowance = (1 << header.align) - 1
    if header.magic == ZISO_MAGIC:
        return _unlz4(payload, block_size, wanted, allowance)
    if header.version < 2:
        return _inflate(payload, block_size)

    # CSO v2 mixes deflate and LZ4 blocks. Try deflate first and fall back to
    # LZ4; a wrong guess virtually always fails outright, and the caller checks
    # the length of whatever comes back.
    try:
        block = _inflate(payload, block_size)
        if len(block) >= wanted:
            return block
    except CorruptImage:
        pass
    return _unlz4(payload, block_size, wanted, allowance)


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------


@dataclass
class ConversionResult:
    source: Path
    destination: Path
    header: CsoHeader
    compressed_size: int
    iso_size: int
    seconds: float

    @property
    def ratio(self) -> float:
        return self.compressed_size / self.iso_size if self.iso_size else 0.0


ProgressCallback = Callable[[int, int], None]


def _extract(fh, out, header: CsoHeader, index, progress: Optional[ProgressCallback]) -> int:
    block_size = header.block_size
    align = header.align
    total = header.total_bytes
    written = 0

    for i in range(header.num_blocks):
        entry = index[i]
        offset = (entry & POSITION_MASK) << align
        stored = ((index[i + 1] & POSITION_MASK) << align) - offset
        if stored <= 0:
            raise CorruptImage("block %d has an invalid stored size of %d" % (i, stored))

        if fh.tell() != offset:
            fh.seek(offset)
        payload = fh.read(stored)
        if len(payload) != stored:
            raise CorruptImage("file ends in the middle of block %d" % i)

        wanted = min(block_size, total - written)
        if entry & PLAIN_FLAG:
            block = payload[:block_size]
        else:
            block = _decode_block(payload, header, wanted)

        if len(block) < wanted:
            raise CorruptImage(
                "block %d decoded to %d bytes, expected %d" % (i, len(block), wanted)
            )
        out.write(block[:wanted] if len(block) > wanted else block)
        written += wanted

        if progress is not None and i % PROGRESS_EVERY == 0:
            progress(written, total)

    if written != total:
        raise CorruptImage(
            "wrote %d bytes but the header promised %d" % (written, total)
        )
    if progress is not None:
        progress(written, total)
    return written


def convert(
    source,
    destination,
    *,
    force: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> ConversionResult:
    """Expand the CSO/ZSO image at ``source`` into an ISO at ``destination``.

    The ISO is built in a ``.part`` file that is renamed into place only after
    every block has been written, so an interrupted run never leaves behind a
    truncated image that looks finished.
    """
    source = Path(source)
    destination = Path(destination)

    if os.path.abspath(source) == os.path.abspath(destination):
        raise CsoError("source and destination are the same file")
    if destination.exists() and not force:
        raise CsoError("%s already exists (pass --force to overwrite it)" % destination)

    started = time.monotonic()
    compressed_size = source.stat().st_size
    partial = destination.with_name(destination.name + ".part")

    with open(source, "rb", buffering=IO_BUFFER) as fh:
        header = parse_header(fh.read(HEADER_SIZE))
        index = _read_index(fh, header)
        if destination.parent and not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(partial, "wb", buffering=IO_BUFFER) as out:
                iso_size = _extract(fh, out, header, index, progress)
        except BaseException:
            try:
                partial.unlink()
            except OSError:
                pass
            raise

    os.replace(str(partial), str(destination))
    return ConversionResult(
        source=source,
        destination=destination,
        header=header,
        compressed_size=compressed_size,
        iso_size=iso_size,
        seconds=time.monotonic() - started,
    )


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def human_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "%d %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TiB" % value


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


class _Progress:
    """Draws a single self-updating status line on stderr."""

    WIDTH = 22

    def __init__(self, label: str, stream) -> None:
        self.label = label if len(label) <= 28 else label[:27] + "…"
        self.stream = stream
        self.started = time.monotonic()
        self.last = 0.0
        self.drawn = 0

    def __call__(self, done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - self.last < 0.1:
            return
        self.last = now
        elapsed = max(now - self.started, 1e-6)
        speed = done / elapsed
        fraction = done / total if total else 1.0
        filled = int(self.WIDTH * fraction)
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        eta = human_time((total - done) / speed) if speed > 0 else "--:--"
        line = "  %-28s %s %3d%%  %s/s  eta %s" % (
            self.label,
            bar,
            round(fraction * 100),
            human_bytes(speed),
            eta,
        )
        self.stream.write("\r" + line.ljust(self.drawn))
        self.stream.flush()
        self.drawn = len(line)

    def clear(self) -> None:
        if self.drawn:
            self.stream.write("\r" + " " * self.drawn + "\r")
            self.stream.flush()
            self.drawn = 0


def describe(path: Path, header: CsoHeader, compressed_size: int) -> str:
    ratio = compressed_size / header.total_bytes if header.total_bytes else 0.0
    rows = [
        ("format", header.format_name),
        ("block size", "%d bytes" % header.block_size),
        ("index align", str(header.align)),
        ("blocks", "%d" % header.num_blocks),
        ("compressed", human_bytes(compressed_size)),
        ("uncompressed", "%s (%d bytes)" % (human_bytes(header.total_bytes), header.total_bytes)),
        ("ratio", "%.1f%% of the original size" % (ratio * 100)),
    ]
    lines = [str(path)]
    lines += ["  %-14s%s" % (name, value) for name, value in rows]
    return "\n".join(lines)


def default_output(source: Path, output: Optional[str], multiple: bool) -> Path:
    if output is None:
        return source.with_suffix(".iso")
    target = Path(output)
    if multiple or target.is_dir() or output.endswith(("/", os.sep)):
        return target / (source.stem + ".iso")
    return target


def _gui_pick_files() -> Optional[List[Path]]:
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None
    try:
        root = tkinter.Tk()
        root.withdraw()
        chosen = filedialog.askopenfilenames(
            title="Choose the .cso / .zso files to convert",
            filetypes=[
                ("Compressed disc images", "*.cso *.zso *.ciso"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
    except Exception:
        return None
    return [Path(p) for p in chosen]


def _gui_available() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _owns_console() -> bool:
    """True when we were double-clicked on Windows and own the console window."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        buffer = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(buffer, 4) <= 1
    except Exception:
        return False


def _pause() -> None:
    try:
        input("\nPress Enter to close this window...")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cso2iso",
        description="Convert compressed disc images (.cso/.zso/.ciso) back into .iso files.",
        epilog=(
            "examples:\n"
            "  cso2iso game.cso                 write game.iso next to game.cso\n"
            "  cso2iso game.cso out/game.iso    choose the output name\n"
            "  cso2iso *.cso -o isos/           convert a whole folder into isos/\n"
            "  cso2iso --info game.cso          just print what is inside\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", metavar="INPUT", help=".cso/.zso file to convert")
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="output file, or a directory when converting several inputs",
    )
    parser.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    parser.add_argument("-i", "--info", action="store_true", help="show image details, convert nothing")
    parser.add_argument("-q", "--quiet", action="store_true", help="only report errors")
    parser.add_argument("--no-gui", action="store_true", help="never open the file picker")
    parser.add_argument("--version", action="version", version="cso2iso " + __version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    inputs = [Path(p) for p in args.inputs]
    interactive = False
    if not inputs and not args.no_gui and _gui_available():
        picked = _gui_pick_files()
        if picked:
            inputs = picked
            interactive = True
    if not inputs:
        parser.print_help()
        if _owns_console():
            _pause()
        return 2

    if args.output is not None and len(inputs) > 1 and not Path(args.output).is_dir():
        Path(args.output).mkdir(parents=True, exist_ok=True)

    log = (lambda text: None) if args.quiet else (lambda text: print(text))
    show_progress = not args.quiet and sys.stderr.isatty()
    failures = 0

    for source in inputs:
        try:
            if not source.is_file():
                raise CsoError("no such file: %s" % source)
            if args.info:
                log(describe(source, read_header(source), source.stat().st_size))
                continue

            destination = default_output(source, args.output, len(inputs) > 1)
            bar = _Progress(source.name, sys.stderr) if show_progress else None
            try:
                result = convert(source, destination, force=args.force, progress=bar)
            finally:
                if bar is not None:
                    bar.clear()
            log(
                "  %s  ->  %s  (%s, %s in %s)"
                % (
                    source.name,
                    result.destination,
                    human_bytes(result.iso_size),
                    header_speed(result),
                    human_time(result.seconds),
                )
            )
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except (CsoError, OSError) as exc:
            print("cso2iso: %s: %s" % (source.name, exc), file=sys.stderr)
            failures += 1

    if interactive or _owns_console():
        _pause()
    return 1 if failures else 0


def header_speed(result: ConversionResult) -> str:
    speed = result.iso_size / result.seconds if result.seconds > 0 else 0
    return "%s/s" % human_bytes(speed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
