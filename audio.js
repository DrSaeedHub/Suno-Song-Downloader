/**
 * Optional MP3 output. Chrome decodes the decrypted Opus/MP4 audio (decoding
 * needs a page, so this runs in the side panel); LAME then encodes it in a
 * worker so the panel stays responsive.
 */

const DECODE_SAMPLE_RATE = 48000; // Suno streams Opus, which is natively 48 kHz

function concat(parts) {
  const out = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

/** A minimal ID3v2.3 tag holding only the title (TIT2), like ffmpeg -metadata title=. */
export function id3Title(title) {
  if (!title) return new Uint8Array(0);
  // Encoding byte 1 = UTF-16 with BOM, so Persian and other non-Latin titles survive.
  const body = new Uint8Array(3 + title.length * 2);
  body.set([1, 0xff, 0xfe]);
  for (let i = 0; i < title.length; i++) {
    const unit = title.charCodeAt(i);
    body[3 + 2 * i] = unit & 0xff;
    body[4 + 2 * i] = unit >> 8;
  }
  const frame = new Uint8Array(10 + body.length);
  frame.set([0x54, 0x49, 0x54, 0x32]); // "TIT2"
  new DataView(frame.buffer).setUint32(4, body.length); // v2.3 frame sizes are plain big-endian
  frame.set(body, 10);

  const header = new Uint8Array(10);
  header.set([0x49, 0x44, 0x33, 3, 0, 0]); // "ID3", v2.3.0, no flags
  const size = frame.length; // the tag size is "synchsafe": 7 bits per byte
  header.set([(size >> 21) & 0x7f, (size >> 14) & 0x7f, (size >> 7) & 0x7f, size & 0x7f], 6);
  return concat([header, frame]);
}

function encodeInWorker(message, transfer, onProgress) {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./mp3-worker.js', import.meta.url));
    worker.onmessage = ({ data }) => {
      if (data.progress !== undefined) {
        onProgress?.(data.progress);
        return;
      }
      worker.terminate();
      if (data.error) reject(new Error(`MP3 encoding failed: ${data.error}`));
      else resolve(data.mp3);
    };
    worker.onerror = (event) => {
      worker.terminate();
      reject(new Error(`MP3 encoder crashed: ${event.message || 'unknown error'}`));
    };
    worker.postMessage(message, transfer);
  });
}

/** Decrypted audio bytes -> MP3 bytes (with a title tag). */
export async function encodeMp3(bytes, { kbps = 320, title, onProgress } = {}) {
  const context = new OfflineAudioContext({ numberOfChannels: 2, length: 1, sampleRate: DECODE_SAMPLE_RATE });
  let audio;
  try {
    audio = await context.decodeAudioData(bytes.slice().buffer); // decodeAudioData detaches its input
  } catch (error) {
    throw new Error(`Chrome could not decode the audio for MP3 conversion (${error?.message ?? error})`);
  }
  const channels = [];
  for (let i = 0; i < Math.min(audio.numberOfChannels, 2); i++) channels.push(audio.getChannelData(i).slice());
  const mp3 = await encodeInWorker(
    { channels, sampleRate: audio.sampleRate, kbps },
    channels.map((channel) => channel.buffer),
    onProgress,
  );
  return concat([id3Title(title), mp3]);
}
