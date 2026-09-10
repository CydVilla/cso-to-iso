<div align="center">

# cso2iso

**Turn compressed `.cso` and `.zso` disc images back into plain `.iso` files.**

One small download, nothing to install, no dependencies. Windows, macOS and Linux.

[![CI](https://github.com/CydVilla/cso-to-iso/actions/workflows/ci.yml/badge.svg)](https://github.com/CydVilla/cso-to-iso/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/CydVilla/cso-to-iso?color=5b4bd6&label=release)](https://github.com/CydVilla/cso-to-iso/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/CydVilla/cso-to-iso/total?color=5b4bd6)](https://github.com/CydVilla/cso-to-iso/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-5b4bd6)](LICENSE)

**[⬇ Download page](https://cydvilla.github.io/cso-to-iso/)** · [Releases](https://github.com/CydVilla/cso-to-iso/releases/latest)

</div>

---

CSO is a compressed disc image: an ISO squeezed down block by block. Emulators
read it happily, but plenty of other software wants a real ISO. `cso2iso` puts
one back, byte for byte.

```
$ cso2iso 'Wipeout Pure.cso'
  Wipeout Pure.cso  ->  Wipeout Pure.iso  (1.7 GiB, 512.4 MiB/s in 0:03)
```

## Download

| System | File |
| --- | --- |
| Windows 10 / 11, 64-bit | [`cso2iso-windows-x86_64.zip`](https://github.com/CydVilla/cso-to-iso/releases/latest/download/cso2iso-windows-x86_64.zip) |
| macOS, Apple Silicon | [`cso2iso-macos-arm64.tar.gz`](https://github.com/CydVilla/cso-to-iso/releases/latest/download/cso2iso-macos-arm64.tar.gz) |
| macOS, Intel | [`cso2iso-macos-x86_64.tar.gz`](https://github.com/CydVilla/cso-to-iso/releases/latest/download/cso2iso-macos-x86_64.tar.gz) |
| Linux, 64-bit | [`cso2iso-linux-x86_64.tar.gz`](https://github.com/CydVilla/cso-to-iso/releases/latest/download/cso2iso-linux-x86_64.tar.gz) |

Each archive holds a single self-contained program, this README and the
licence. Every release also ships a `SHA256SUMS` file, and the builds are made
in public by [the release workflow](.github/workflows/release.yml).

## Running it

### Windows

Unzip the download, then **drag your `.cso` file onto `cso2iso.exe`**. The
`.iso` lands next to the original.

To type commands instead, open the unzipped folder, type `cmd` in the address
bar, press <kbd>Enter</kbd> and run:

```bat
cso2iso.exe "C:\games\Wipeout Pure.cso"
```

The first launch may bring up *Windows protected your PC*, because these builds
are not code signed. Choose **More info**, then **Run anyway**.

### macOS

Double-click the `.tar.gz` to unpack it, then open Terminal, type `cd ` and drag
the unpacked folder in. macOS quarantines anything a browser downloads, so clear
that flag once:

```bash
xattr -dr com.apple.quarantine cso2iso
./cso2iso ~/Downloads/game.cso
```

Apple menu → About This Mac tells you which build you need. Anything listing an
Apple M chip takes `arm64`; older machines take `x86_64`.

### Linux

```bash
tar -xzf cso2iso-linux-x86_64.tar.gz
cd cso2iso-linux-x86_64
chmod +x cso2iso
./cso2iso ~/roms/game.cso
```

To keep it around: `sudo install -m 755 cso2iso /usr/local/bin/`

### From source, on anything

The converter is a single file with no dependencies beyond Python 3.9 or newer,
which macOS and most Linux systems already have.

```bash
curl -LO https://raw.githubusercontent.com/CydVilla/cso-to-iso/main/cso2iso.py
python3 cso2iso.py game.cso
```

Or install it as a proper command:

```bash
pipx install git+https://github.com/CydVilla/cso-to-iso
```

## Usage

```
cso2iso game.cso                 write game.iso next to game.cso
cso2iso game.cso out/game.iso    choose the output name
cso2iso *.cso -o isos/           convert a whole folder into isos/
cso2iso --info game.cso          print what is inside, convert nothing
```

| Option | What it does |
| --- | --- |
| `-o`, `--output PATH` | Output file, or a directory when there are several inputs |
| `-f`, `--force` | Overwrite files that already exist |
| `-i`, `--info` | Show the image details and stop |
| `-q`, `--quiet` | Only report errors |
| `--no-gui` | Never open the file picker |
| `--version` | Print the version |

`--info` reads only the header, so it answers instantly even for a large image:

```
$ cso2iso --info game.cso
game.cso
  format        CISO v1 (deflate)
  block size    2048 bytes
  index align   0
  blocks        876544
  compressed    1.1 GiB
  uncompressed  1.7 GiB (1794113536 bytes)
  ratio         64.8% of the original size
```

## What it reads

| Extension | Format | Compression |
| --- | --- | --- |
| `.cso`, `.ciso` | CISO v0 and v1 | deflate |
| `.cso` | CISO v2 | deflate or LZ4, per block |
| `.zso` | ZISO | LZ4 |

Files are recognised by their contents, not their name. DAX images are detected
and reported rather than mangled.

LZ4 images are handled by a small pure Python decoder, which the test suite
checks against the reference C library. The prebuilt downloads bundle that C
library for speed; if you run the script yourself and convert `.zso` files
often, `pip install lz4` gives you the same speed-up.

## How it works

A CSO is a 24-byte header, a table of block offsets, then the blocks. The header
records the size of the original ISO and the block size, usually 2048 bytes, one
disc sector. Each entry in the table points at one stored block, and its top bit
says whether that block was worth compressing or was stored as-is.

Converting is a matter of walking the table, expanding each block and writing
the result out in order. Two details matter for getting the bytes exactly right:

- The final block is trimmed to the size the header promised, because
  compressors pad it out to a whole block.
- Offsets can be shifted left by an alignment value, which leaves padding
  between blocks. Deflate stops at its own end marker, but LZ4 has none, so the
  decoder is told how many bytes to produce and ignores whatever follows.

The ISO is written to a `.part` file and only renamed once every block has been
written, so an interrupted run cannot leave behind a truncated image that looks
complete.

## Development

```bash
git clone https://github.com/CydVilla/cso-to-iso
cd cso-to-iso
python3 -m unittest discover -s tests -t . -v
```

The suite builds CSO and ZSO images in memory with a miniature compressor and
converts them back, covering deflate and LZ4 blocks, stored blocks, index
alignment, partial final blocks, truncated and corrupt files, and the command
line itself. It needs nothing but the standard library; installing `lz4` runs
the same tests through the native decoder as well.

To build a standalone binary for the system you are on:

```bash
pip install pyinstaller
pyinstaller --onefile --console --name cso2iso cso2iso.py
```

Pushing a `v*` tag builds all four downloads and publishes them as a release.

## Licence

MIT. See [LICENSE](LICENSE).
