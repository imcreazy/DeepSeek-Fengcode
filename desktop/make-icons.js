/**
 * 生成桌面端图标（PNG + ICO 占位）。
 *
 * 不依赖任何图像库：手写最小 PNG（纯色 + 简单图形），
 * 保证 electron-builder 打包不会因为缺图标而失败。
 * 用户可自行替换为更好看的图标。
 */

const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

const OUT_DIR = __dirname;
const SIZE = 256;

/** 手写 PNG：RGBA 像素 → PNG 字节流 */
function makePng(size, painter) {
  const raw = Buffer.alloc(size * (size * 4 + 1));
  let p = 0;
  for (let y = 0; y < size; y++) {
    raw[p++] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = painter(x, y, size);
      raw[p++] = r;
      raw[p++] = g;
      raw[p++] = b;
      raw[p++] = a;
    }
  }
  const idat = zlib.deflateSync(raw, { level: 9 });

  function chunk(type, data) {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length, 0);
    const typeBuf = Buffer.from(type, "ascii");
    const crcInput = Buffer.concat([typeBuf, data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(crcInput) >>> 0, 0);
    return Buffer.concat([len, typeBuf, data, crc]);
  }

  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;   // bit depth
  ihdr[9] = 6;   // color type RGBA
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;

  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

let CRC_TABLE = null;
function crc32(buf) {
  if (!CRC_TABLE) {
    CRC_TABLE = new Int32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      CRC_TABLE[i] = c;
    }
  }
  let crc = -1;
  for (let i = 0; i < buf.length; i++) {
    crc = CRC_TABLE[(crc ^ buf[i]) & 0xff] ^ (crc >>> 8);
  }
  return crc ^ -1;
}

/** 品牌着色：靛蓝渐变底 + 白色 F */
function paint(x, y, size) {
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2;
  // 圆角矩形（近似圆）
  const dx = Math.abs(x - cx);
  const dy = Math.abs(y - cy);
  const corner = size * 0.22;
  const insideSquare =
    dx <= r - corner || dy <= r - corner ||
    Math.hypot(dx - (r - corner), dy - (r - corner)) <= corner;
  if (!insideSquare) return [0, 0, 0, 0];

  // 斜向渐变 #4f46e5 → #8b5cf6
  const t = (x + y) / (size * 2);
  const R = Math.round(0x4f + (0x8b - 0x4f) * t);
  const G = Math.round(0x46 + (0x5c - 0x46) * t);
  const B = Math.round(0xe5 + (0xf6 - 0xe5) * t);

  // 白色字母 "F"（用简单笔画绘制）
  const fx0 = size * 0.34, fx1 = size * 0.68;
  const fy0 = size * 0.28, fy1 = size * 0.74;
  const thick = size * 0.088;
  const inF =
    (x >= fx0 && x <= fx0 + thick && y >= fy0 && y <= fy1) ||          // 竖笔
    (y >= fy0 && y <= fy0 + thick && x >= fx0 && x <= fx1) ||          // 上横
    (y >= size * 0.46 && y <= size * 0.46 + thick && x >= fx0 && x <= fx1 * 0.92); // 中横
  if (inF) return [255, 255, 255, 255];

  return [R, G, B, 255];
}

function write(name, size, painter) {
  const buf = makePng(size, painter);
  const p = path.join(OUT_DIR, name);
  fs.writeFileSync(p, buf);
  console.log(`已生成 ${name}（${size}×${size}，${buf.length} 字节）`);
}

console.log("生成桌面端图标…");
write("icon.png", 256, paint);
write("tray.png", 32, paint);
console.log("完成。可自行替换 icon.png / tray.png 为更精美的图标。");
