/* cso2iso in the browser. MIT licensed, same as the rest of the project.
 *
 * Mirrors cso2iso.py: parse the header, walk the block index, expand each
 * block and write the bytes out in order, or the reverse, packing a plain ISO
 * into a CISO v1 image block by block. Nothing is uploaded anywhere; the
 * file is read locally through the File API and written back with either the
 * File System Access API or a blob download.
 */
(function () {
  "use strict";

  var CISO = "CISO", ZISO = "ZISO", DAX = "DAX\0";
  var HEADER_SIZE = 24;
  var PLAIN_FLAG = 0x80000000;
  var POSITION_MASK = 0x7fffffff;
  var WINDOW = 8 << 20;
  var BLOB_FLUSH = 48 << 20;
  var BATCH = 4 << 20;

  function CsoError(message) {
    var err = new Error(message);
    err.name = "CsoError";
    return err;
  }

  function ascii(view, offset, length) {
    var out = "";
    for (var i = 0; i < length; i++) out += String.fromCharCode(view.getUint8(offset + i));
    return out;
  }

  function parseHeader(bytes) {
    if (bytes.length < HEADER_SIZE) throw CsoError("file is too small to be a compressed disc image");
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var magic = ascii(view, 0, 4);
    if (magic === DAX) throw CsoError("this is a DAX image, which cso2iso cannot read");
    if (magic !== CISO && magic !== ZISO) {
      throw CsoError("not a CSO/ZSO image: expected CISO or ZISO, found “" + magic.replace(/[^\x20-\x7e]/g, "?") + "”");
    }
    var headerSize = view.getUint32(4, true) || HEADER_SIZE;
    // total size is 64 bit; browsers cap files well below 2^53 so this is safe
    var totalBytes = view.getUint32(8, true) + view.getUint32(12, true) * 4294967296;
    var blockSize = view.getUint32(16, true);
    var version = view.getUint8(20);
    var align = view.getUint8(21);

    if (headerSize < HEADER_SIZE) throw CsoError("header claims to be only " + headerSize + " bytes long");
    if (version > 2) throw CsoError("unsupported format version " + version);
    if (!blockSize || blockSize > 64 * 1024 * 1024) throw CsoError("implausible block size of " + blockSize + " bytes");
    if (align > 31) throw CsoError("implausible index alignment of " + align);
    if (!totalBytes) throw CsoError("header says the image contains no data");

    var numBlocks = Math.ceil(totalBytes / blockSize);
    return {
      magic: magic, headerSize: headerSize, totalBytes: totalBytes,
      blockSize: blockSize, version: version, align: align,
      numBlocks: numBlocks, indexSize: (numBlocks + 1) * 4,
      codec: magic === ZISO ? "LZ4" : (version >= 2 ? "deflate or LZ4" : "deflate"),
      get formatName() { return this.magic + " v" + this.version + " (" + this.codec + ")"; }
    };
  }

  /* One LZ4 block. `limit` is how many bytes we need, which is what makes it
     safe to hand over a block that still carries its alignment padding: LZ4
     has no end-of-stream marker, so without a limit the padding decodes as
     though it were more data. */
  function lz4Decompress(src, limit) {
    var out = new Uint8Array(limit);
    var pos = 0, o = 0, end = src.length, extra;
    while (pos < end && o < limit) {
      var token = src[pos++];
      var lit = token >> 4;
      if (lit === 15) {
        do {
          if (pos >= end) throw CsoError("truncated LZ4 literal length");
          extra = src[pos++]; lit += extra;
        } while (extra === 255);
      }
      if (lit) {
        if (pos + lit > end) throw CsoError("truncated LZ4 literal run");
        var take = Math.min(lit, limit - o);
        out.set(src.subarray(pos, pos + take), o);
        o += take; pos += lit;
      }
      if (pos === end || o >= limit) break;
      if (pos + 2 > end) throw CsoError("truncated LZ4 match offset");
      var offset = src[pos] | (src[pos + 1] << 8);
      pos += 2;
      if (offset === 0 || offset > o) throw CsoError("invalid LZ4 match offset " + offset);
      var mlen = token & 15;
      if (mlen === 15) {
        do {
          if (pos >= end) throw CsoError("truncated LZ4 match length");
          extra = src[pos++]; mlen += extra;
        } while (extra === 255);
      }
      mlen += 4;
      var from = o - offset;
      var stop = Math.min(o + mlen, limit);
      while (o < stop) out[o++] = out[from++];
    }
    if (o < limit) throw CsoError("LZ4 block produced " + o + " of " + limit + " bytes");
    return out;
  }

  /* Raw deflate through the browser's own decompressor. The stream is never
     closed: closing makes it inspect whatever follows the compressed data and
     fail on the alignment padding, so instead we read the bytes we need and
     cancel. */
  async function inflateRaw(payload, wanted) {
    var ds = new DecompressionStream("deflate-raw");
    var writer = ds.writable.getWriter();
    writer.write(payload).catch(function () {});
    var reader = ds.readable.getReader();
    var out = new Uint8Array(wanted);
    var got = 0;
    try {
      while (got < wanted) {
        var step = await reader.read();
        if (step.done) break;
        var take = Math.min(step.value.length, wanted - got);
        out.set(step.value.subarray(0, take), got);
        got += take;
      }
    } catch (err) {
      throw CsoError("deflate error: " + err.message);
    } finally {
      reader.cancel().catch(function () {});
      writer.abort().catch(function () {});
    }
    if (got < wanted) throw CsoError("block decoded to " + got + " of " + wanted + " bytes");
    return out;
  }

  /* The fast path. fflate decodes a block with no stream to construct and no
     promise to await, which is worth about five times the throughput of
     DecompressionStream on blocks this small. `out` doubles as the length
     limit, so alignment padding is ignored and an over-long final block is
     truncated, matching what the Python version does. */
  function inflateFast(payload, wanted, scratch) {
    try {
      return window.fflate.inflateSync(payload, { out: scratch.subarray(0, wanted) });
    } catch (err) {
      throw CsoError("deflate error: " + err.message);
    }
  }

  function decodeFast(payload, header, wanted, scratch) {
    if (header.magic === ZISO) return lz4Decompress(payload, wanted);
    if (header.version < 2) return inflateFast(payload, wanted, scratch);
    try {
      return inflateFast(payload, wanted, scratch);
    } catch (err) {
      return lz4Decompress(payload, wanted);
    }
  }

  async function decodeBlock(payload, header, wanted) {
    if (header.magic === ZISO) return lz4Decompress(payload, wanted);
    if (header.version < 2) return inflateRaw(payload, wanted);
    try {
      return await inflateRaw(payload, wanted);
    } catch (err) {
      return lz4Decompress(payload, wanted);
    }
  }

  /* Reads the file through a sliding window so each block is not its own
     round trip to disk. */
  function Reader(file) {
    this.file = file;
    this.start = 0;
    this.buf = new Uint8Array(0);
  }
  /* Returns the bytes straight away when they are already in the window, so
     the hot loop only goes async when the file actually has to be read. */
  Reader.prototype.peek = function (offset, length) {
    if (offset >= this.start && offset + length <= this.start + this.buf.length) {
      var from = offset - this.start;
      return this.buf.subarray(from, from + length);
    }
    return null;
  };
  Reader.prototype.bytes = async function (offset, length) {
    if (offset < this.start || offset + length > this.start + this.buf.length) {
      var span = Math.max(WINDOW, length);
      var end = Math.min(this.file.size, offset + span);
      this.buf = new Uint8Array(await this.file.slice(offset, end).arrayBuffer());
      this.start = offset;
    }
    var from = offset - this.start;
    if (from + length > this.buf.length) throw CsoError("file ends earlier than the index says");
    return this.buf.subarray(from, from + length);
  };

  /* Sinks take the output in order. Compressing also needs `reserve`, which
     holds space for the block index at the very start, and `patch`, which
     fills it in once every block is down. */
  function blobSink() {
    var parts = [], pending = [], pendingBytes = 0, prefix = null;
    return {
      kind: "blob",
      write: function (chunk) {
        pending.push(chunk);
        pendingBytes += chunk.length;
        if (pendingBytes >= BLOB_FLUSH) {
          parts.push(new Blob(pending));
          pending = []; pendingBytes = 0;
        }
      },
      reserve: function (length) { prefix = new Uint8Array(length); },
      patch: function (position, chunk) { prefix.set(chunk, position); },
      finish: function () {
        if (pending.length) parts.push(new Blob(pending));
        if (prefix) parts.unshift(prefix);
        return new Blob(parts, { type: "application/octet-stream" });
      }
    };
  }

  async function fileSink(handle) {
    var stream = await handle.createWritable();
    return {
      kind: "disk",
      write: function (chunk) { return stream.write(chunk); },
      reserve: function (length) { return stream.write(new Uint8Array(length)); },
      patch: function (position, chunk) {
        return stream.write({ type: "write", position: position, data: chunk });
      },
      finish: async function () { await stream.close(); return null; }
    };
  }

  async function convert(file, sink, onProgress) {
    var reader = new Reader(file);
    var header = parseHeader(await reader.bytes(0, HEADER_SIZE));

    var indexBytes = await reader.bytes(header.headerSize, header.indexSize);
    var index = new Uint32Array(indexBytes.slice().buffer);

    var fast = !!(window.fflate && typeof window.fflate.inflateSync === "function");
    var scratch = new Uint8Array(header.blockSize);
    var step = Math.pow(2, header.align);

    // Blocks are gathered into a large buffer before reaching the sink. Writing
    // 2 KB at a time to a file on disk costs far more than the decoding does.
    var batchSize = Math.max(BATCH, header.blockSize);
    var batch = new Uint8Array(batchSize);
    var used = 0;

    var written = 0;
    var started = performance.now();
    var lastPaint = started;

    for (var i = 0; i < header.numBlocks; i++) {
      var entry = index[i];
      var offset = (entry & POSITION_MASK) * step;
      var stored = (index[i + 1] & POSITION_MASK) * step - offset;
      if (stored <= 0) throw CsoError("block " + i + " has an invalid stored size of " + stored);

      var payload = reader.peek(offset, stored);
      if (payload === null) payload = await reader.bytes(offset, stored);

      var wanted = Math.min(header.blockSize, header.totalBytes - written);
      var block;
      if (entry & PLAIN_FLAG) {
        if (payload.length < wanted) throw CsoError("stored block " + i + " is short");
        block = payload;
      } else if (fast) {
        block = decodeFast(payload, header, wanted, scratch);
      } else {
        block = await decodeBlock(payload, header, wanted);
      }
      if (block.length < wanted) {
        throw CsoError("block " + i + " decoded to " + block.length + " of " + wanted + " bytes");
      }

      if (used + wanted > batchSize) {
        await sink.write(batch.subarray(0, used));
        batch = new Uint8Array(batchSize);
        used = 0;
      }
      batch.set(block.subarray(0, wanted), used);
      used += wanted;
      written += wanted;

      // The fast path never yields on its own, so hand the browser a moment to
      // repaint every so often, otherwise the progress bar would not move.
      if (i % 256 === 0 || i === header.numBlocks - 1) {
        var now = performance.now();
        if (now - lastPaint >= 50 || i === header.numBlocks - 1) {
          lastPaint = now;
          if (onProgress) onProgress(written, header.totalBytes, (now - started) / 1000);
          await new Promise(function (resume) { setTimeout(resume, 0); });
        }
      }
    }
    if (used) await sink.write(batch.subarray(0, used));

    if (written !== header.totalBytes) {
      throw CsoError("wrote " + written + " bytes but the header promised " + header.totalBytes);
    }
    var result = await sink.finish();
    return {
      header: header, bytes: written, blob: result,
      seconds: (performance.now() - started) / 1000,
      decoder: fast ? "fflate" : "DecompressionStream"
    };
  }

  /* ---- compressing, the reverse direction ----
     Mirrors compress() in cso2iso.py: CISO v1, level 9 raw deflate in 2048
     byte blocks, blocks that do not shrink stored as they are and flagged in
     the index, a partial final block padded with zeros. That is the variant
     PSP custom firmware, Adrenaline and PPSSPP all read. */

  var COMPRESS_BLOCK = 2048;
  var COMPRESS_LEVEL = 9;
  var ISO9660_AT = 0x8001;

  /* Which way a file should go: "expand" for a compressed image, "compress"
     for a plain ISO, null for anything else. Like the Python version, an ISO
     is recognised by its volume descriptor or, failing that, its name. */
  async function sniff(file) {
    var head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
    var magic = String.fromCharCode.apply(null, head);
    if (magic === CISO || magic === ZISO || magic === DAX) return "expand";
    if (file.size > ISO9660_AT + 5) {
      var sig = new Uint8Array(await file.slice(ISO9660_AT, ISO9660_AT + 5).arrayBuffer());
      if (String.fromCharCode.apply(null, sig) === "CD001") return "compress";
    }
    if (/\.iso$/i.test(file.name)) return "compress";
    return null;
  }

  /* Index entries hold 31 bits of offset, so the output must stay under 2 GiB
     unless offsets are scaled by an alignment. Assumes every block is stored
     whole and needs the most padding, so it is safe before compressing. */
  function pickAlign(totalBytes, blockSize) {
    var numBlocks = Math.ceil(totalBytes / blockSize);
    var base = HEADER_SIZE + (numBlocks + 1) * 4;
    for (var align = 0; align < 32; align++) {
      var step = Math.pow(2, align);
      var worst = base + numBlocks * (blockSize + step - 1) + step;
      if (Math.floor(worst / step) <= POSITION_MASK) return align;
    }
    throw CsoError("image is too large to compress");
  }

  function deflateFast(block) {
    return window.fflate.deflateSync(block, { level: COMPRESS_LEVEL });
  }

  async function deflateRaw(block) {
    var cs = new CompressionStream("deflate-raw");
    var bytes = await new Response(new Blob([block]).stream().pipeThrough(cs)).arrayBuffer();
    return new Uint8Array(bytes);
  }

  async function compress(file, sink, onProgress) {
    var totalBytes = file.size;
    if (!totalBytes) throw CsoError("file is empty, there is nothing to compress");
    if (await sniff(file) === "expand") throw CsoError("file is already a compressed image");

    var blockSize = COMPRESS_BLOCK;
    var align = pickAlign(totalBytes, blockSize);
    var step = Math.pow(2, align);
    var numBlocks = Math.ceil(totalBytes / blockSize);
    var indexSize = (numBlocks + 1) * 4;
    var index = new Uint32Array(numBlocks + 1);

    var fast = !!(window.fflate && typeof window.fflate.deflateSync === "function");
    var reader = new Reader(file);
    var block = new Uint8Array(blockSize);

    await sink.reserve(HEADER_SIZE + indexSize);
    var position = HEADER_SIZE + indexSize;

    var batchSize = Math.max(BATCH, blockSize * 2 + step);
    var batch = new Uint8Array(batchSize);
    var used = 0;
    async function put(bytes) {
      if (used + bytes.length > batchSize) {
        await sink.write(batch.subarray(0, used));
        batch = new Uint8Array(batchSize);
        used = 0;
      }
      batch.set(bytes, used);
      used += bytes.length;
      position += bytes.length;
    }

    var started = performance.now();
    var lastPaint = started;

    for (var i = 0; i < numBlocks; i++) {
      var start = i * blockSize;
      var length = Math.min(blockSize, totalBytes - start);
      var raw = reader.peek(start, length);
      if (raw === null) raw = await reader.bytes(start, length);
      if (length < blockSize) {
        block.fill(0);
        block.set(raw);
        raw = block;
      }

      var packed = fast ? deflateFast(raw) : await deflateRaw(raw);
      var plain = packed.length >= blockSize;

      var padding = (step - position % step) % step;
      if (padding) await put(new Uint8Array(padding));
      var entry = position / step;
      if (entry > POSITION_MASK) throw CsoError("compressed image outgrew its index alignment");
      index[i] = plain ? (entry | PLAIN_FLAG) >>> 0 : entry;
      await put(plain ? raw : packed);

      if (i % 256 === 0 || i === numBlocks - 1) {
        var now = performance.now();
        if (now - lastPaint >= 50 || i === numBlocks - 1) {
          lastPaint = now;
          if (onProgress) onProgress(Math.min(start + length, totalBytes), totalBytes, (now - started) / 1000);
          await new Promise(function (resume) { setTimeout(resume, 0); });
        }
      }
    }
    var tail = (step - position % step) % step;
    if (tail) await put(new Uint8Array(tail));
    index[numBlocks] = position / step;
    if (used) await sink.write(batch.subarray(0, used));

    var head = new Uint8Array(HEADER_SIZE);
    var view = new DataView(head.buffer);
    for (var c = 0; c < 4; c++) head[c] = CISO.charCodeAt(c);
    view.setUint32(4, HEADER_SIZE, true);
    view.setUint32(8, totalBytes % 4294967296, true);
    view.setUint32(12, Math.floor(totalBytes / 4294967296), true);
    view.setUint32(16, blockSize, true);
    head[20] = 1;
    head[21] = align;
    await sink.patch(0, head);
    var indexBytes = new Uint8Array(indexSize);
    var indexView = new DataView(indexBytes.buffer);
    for (var k = 0; k <= numBlocks; k++) indexView.setUint32(k * 4, index[k], true);
    await sink.patch(HEADER_SIZE, indexBytes);

    var result = await sink.finish();
    return {
      bytes: position, iso: totalBytes, blob: result,
      seconds: (performance.now() - started) / 1000,
      encoder: fast ? "fflate" : "CompressionStream"
    };
  }

  window.cso2iso = {
    parseHeader: parseHeader,
    lz4Decompress: lz4Decompress,
    inflateRaw: inflateRaw,
    convert: convert,
    sniff: sniff,
    compress: compress,
    blobSink: blobSink,
    fileSink: fileSink,
    supported: typeof DecompressionStream === "function",
    canCompress: typeof CompressionStream === "function" ||
      !!(window.fflate && typeof window.fflate.deflateSync === "function"),
    canSaveToDisk: typeof window.showSaveFilePicker === "function"
  };
})();
