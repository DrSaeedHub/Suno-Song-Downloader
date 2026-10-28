// MP3 encoder worker. A classic worker, because lame.min.js is a plain script
// that defines the global `lamejs`.
importScripts('vendor/lame.min.js');

const BLOCK = 1152 * 32; // whole MP3 frames per encode call

function toInt16(samples) {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

self.onmessage = ({ data: { channels, sampleRate, kbps } }) => {
  try {
    const [left, right] = channels.map(toInt16);
    const encoder = new lamejs.Mp3Encoder(channels.length, sampleRate, kbps);
    const parts = [];
    let size = 0;
    const keep = (chunk) => {
      if (chunk.length) {
        parts.push(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.length));
        size += chunk.length;
      }
    };
    for (let start = 0; start < left.length; start += BLOCK) {
      const end = start + BLOCK;
      keep(right
        ? encoder.encodeBuffer(left.subarray(start, end), right.subarray(start, end))
        : encoder.encodeBuffer(left.subarray(start, end)));
      self.postMessage({ progress: Math.min(end, left.length) / left.length });
    }
    keep(encoder.flush());

    const mp3 = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) {
      mp3.set(part, offset);
      offset += part.length;
    }
    self.postMessage({ mp3 }, [mp3.buffer]);
  } catch (error) {
    self.postMessage({ error: String(error?.message ?? error) });
  }
};
