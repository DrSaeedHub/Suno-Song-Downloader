/**
 * Side panel: lists the signed-in user's own songs and downloads + decrypts
 * the selected ones - the music_workflow.py pipeline, but with the live
 * session of the open suno.com tab instead of a capture file and a hardcoded
 * token.
 *
 * Per song: license (fetched in the Suno tab) -> download the encrypted file
 * -> decrypt -> optionally transcode to MP3 -> save via chrome.downloads.
 */

import { sunoTabCall } from './suno-tab.js';
import { decryptMedia, sniffExtension, unwrapLicense } from './crypto.js';
import { encodeMp3 } from './audio.js';

const SUNO_TAB_PATTERNS = ['https://suno.com/*', 'https://www.suno.com/*'];
const PAGE_SIZE = 20;

const $ = (id) => document.getElementById(id);
const ui = {
  account: $('account'),
  connect: $('connect'),
  connectMessage: $('connect-message'),
  openSuno: $('open-suno'),
  reconnect: $('reconnect'),
  app: $('app'),
  workspace: $('workspace'),
  refresh: $('refresh'),
  selectAll: $('select-all'),
  selectCount: $('select-count'),
  filter: $('filter'),
  list: $('list'),
  songs: $('songs'),
  noMatch: $('no-match'),
  listStatus: $('list-status'),
  loadMore: $('load-more'),
  format: $('format'), // fieldset of radio buttons
  folder: $('folder'),
  stack: $('stack'),
  download: $('download'),
  downloadLabel: $('download-label'),
  jobs: $('jobs'),
  summary: $('summary'),
};

const state = {
  me: null, // { userId, email, handle } of the account signed in on the Suno tab
  workspaceId: 'default',
  clips: [],
  cursor: null,
  hasMore: false,
  selected: new Set(),
  loading: false,
  busy: false,
  batch: null, // { total, finished } while a download runs
  connecting: false,
};

// Song rows are built once per clip and reused, so cover art is not reloaded
// on every render. A reset of the list clears them.
const rows = new Map();

// --------------------------------------------------------------------------- //
// Talking to the Suno tab
// --------------------------------------------------------------------------- //
function isSunoUrl(url) {
  try {
    const { protocol, hostname } = new URL(url);
    return protocol === 'https:' && (hostname === 'suno.com' || hostname === 'www.suno.com');
  } catch {
    return false;
  }
}

async function findSunoTab() {
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (active && isSunoUrl(active.url) && !active.discarded) return active;
  const tabs = await chrome.tabs.query({ url: SUNO_TAB_PATTERNS });
  return tabs.find((tab) => !tab.discarded) ?? null;
}

async function callTab(request) {
  const tab = await findSunoTab();
  if (!tab) throw new Error('No suno.com tab is open. Open suno.com, sign in, then press Connect.');
  let injection;
  try {
    [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: sunoTabCall,
      args: [request],
    });
  } catch (error) {
    throw new Error(`Could not reach the suno.com tab (${error.message}). Reload it and try again.`);
  }
  const result = injection?.result;
  if (!result) throw new Error('The suno.com tab did not answer. Reload it and try again.');
  if (result.error) throw new Error(result.error);
  return result;
}

async function api(method, path, body) {
  const result = await callTab({ kind: 'api', method, path, body });
  if (!result.ok) {
    const hint = result.status === 401 || result.status === 403
      ? ' - your Suno session may have expired; reload the suno.com tab'
      : '';
    throw new Error(`Suno returned HTTP ${result.status || 'error'} for ${path}${hint}`);
  }
  return result;
}

// --------------------------------------------------------------------------- //
// Connection, workspaces and the song list
// --------------------------------------------------------------------------- //
function showConnect(message, isError) {
  ui.connect.hidden = false;
  ui.connect.classList.toggle('error', Boolean(isError));
  ui.connectMessage.textContent = message;
  ui.app.hidden = true;
  ui.account.textContent = 'Not connected';
  ui.account.title = '';
  ui.account.classList.remove('connected');
}

async function connect() {
  if (state.connecting || state.busy) return;
  state.connecting = true;
  showConnect('Connecting to your suno.com tab…', false);
  try {
    state.me = await callTab({ kind: 'session' });
  } catch (error) {
    state.me = null;
    showConnect(error.message, true);
    return;
  } finally {
    state.connecting = false;
  }
  ui.account.textContent = state.me.handle ? `@${state.me.handle}` : (state.me.email ?? 'Signed in');
  ui.account.title = state.me.email ?? '';
  ui.account.classList.add('connected');
  ui.connect.hidden = true;
  ui.app.hidden = false;
  await loadWorkspaces();
  await loadSongs({ reset: true });
}

async function loadWorkspaces() {
  const projects = [];
  try {
    for (let page = 1; page <= 50; page++) {
      const { data } = await api(
        'GET',
        `/api/project/me?page=${page}&sort=max_created_at_last_updated_clip&show_trashed=false&exclude_shared=false`,
      );
      const batch = Array.isArray(data?.projects) ? data.projects : [];
      projects.push(...batch);
      if (!batch.length || projects.length >= (data?.num_total_results ?? 0)) break;
    }
  } catch (error) {
    console.warn('Could not list workspaces; using My Workspace only.', error);
  }
  if (!projects.some((project) => project.id === 'default')) projects.unshift({ id: 'default', name: 'My Workspace' });

  ui.workspace.replaceChildren(...projects.map((project) => {
    const option = document.createElement('option');
    option.value = project.id;
    option.textContent = project.clip_count == null ? project.name : `${project.name} (${project.clip_count})`;
    return option;
  }));
  if (!projects.some((project) => project.id === state.workspaceId)) state.workspaceId = 'default';
  ui.workspace.value = state.workspaceId;
}

// The same filters the Create page uses for a workspace's song list.
function feedFilters(workspaceId) {
  return {
    disliked: 'False',
    trashed: 'False',
    fromStudioProject: { presence: 'False' },
    stem: { presence: 'False' },
    stemComplement: 'False',
    workspace: { presence: 'True', workspaceId },
  };
}

async function loadSongs({ reset }) {
  if (state.loading) return;
  if (reset) {
    state.clips = [];
    state.cursor = null;
    state.hasMore = false;
    state.selected.clear();
    rows.clear();
    ui.list.scrollTop = 0;
  }
  state.loading = true;
  setListStatus(''); // placeholder rows, or the Load more button, show the loading
  render();
  try {
    const { data } = await api('POST', '/api/feed/v3', {
      cursor: state.cursor,
      limit: PAGE_SIZE,
      filters: feedFilters(state.workspaceId),
    });
    const known = new Set(state.clips.map((clip) => clip.id));
    for (const clip of data?.clips ?? []) {
      if (clip?.id && !known.has(clip.id)) state.clips.push(clip);
    }
    state.cursor = data?.next_cursor ?? null;
    state.hasMore = Boolean(data?.has_more && state.cursor);
    setListStatus(state.clips.length ? '' : 'No songs in this workspace.');
  } catch (error) {
    setListStatus(error.message, true);
  } finally {
    state.loading = false;
    render();
  }
}

function setListStatus(message, isError = false) {
  ui.listStatus.hidden = !message;
  ui.listStatus.textContent = message;
  ui.listStatus.classList.toggle('error', isError);
}

function progressiveMedia(clip) {
  return (clip.media_urls ?? []).find((media) => media?.delivery === 'progressive' && media.url) ?? null;
}

// Only the signed-in user's own, finished songs can be downloaded.
function blockReason(clip) {
  if (clip.user_id !== state.me?.userId) return clip.handle ? `by @${clip.handle}` : 'not your song';
  if (clip.status !== 'complete') return clip.status ?? 'not ready';
  if (!progressiveMedia(clip)) return 'no audio file';
  return null;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return '';
  const total = Math.round(seconds);
  const [h, m, s] = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
  const mmss = `${h ? String(m).padStart(2, '0') : m}:${String(s).padStart(2, '0')}`;
  return h ? `${h}:${mmss}` : mmss;
}

function formatDate(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, sameYear
    ? { month: 'short', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
}

function visibleClips() {
  const query = ui.filter.value.trim().toLowerCase();
  return query ? state.clips.filter((clip) => (clip.title ?? '').toLowerCase().includes(query)) : state.clips;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** A song's cover image, or a note glyph when it has none or it fails to load. */
function coverArt(clip, { lazy = true } = {}) {
  const art = element('span', 'art');
  const url = clip.image_url;
  if (typeof url === 'string' && url.startsWith('https://')) {
    const img = element('img');
    img.alt = '';
    img.decoding = 'async';
    if (lazy) img.loading = 'lazy';
    img.addEventListener('error', () => {
      img.remove();
      art.classList.add('empty');
    });
    img.src = url;
    art.append(img);
  } else {
    art.classList.add('empty');
  }
  return art;
}

function songRow(clip) {
  const cached = rows.get(clip.id);
  if (cached) return cached;

  const reason = blockReason(clip);
  const item = element('li', reason ? 'song blocked' : 'song');

  const box = element('input');
  box.type = 'checkbox';
  box.setAttribute('aria-label', clip.title || '(untitled)');
  box.addEventListener('change', () => {
    if (box.checked) state.selected.add(clip.id);
    else state.selected.delete(clip.id);
    render();
  });

  const title = element('span', 'song-title', clip.title || '(untitled)');
  title.dir = 'auto';
  title.title = clip.title ?? '';
  const meta = element('span', 'song-meta');
  if (clip.major_model_version) meta.append(element('span', 'model', clip.major_model_version));
  if (reason) meta.append(element('span', 'reason', reason));
  const tags = element('span', 'tags', clip.display_tags ?? '');
  tags.dir = 'auto';
  meta.append(tags);
  const text = element('span', 'song-text');
  text.append(title, meta);

  const side = element('span', 'song-side');
  side.append(
    element('span', 'duration', formatDuration(clip.metadata?.duration)),
    element('span', 'date', clip.created_at ? formatDate(clip.created_at) : ''),
  );

  item.append(box, coverArt(clip), text, side);
  item.addEventListener('click', (event) => {
    if (event.target !== box && !box.disabled) box.click();
  });

  const row = { item, box };
  rows.set(clip.id, row);
  return row;
}

function placeholderRow() {
  const item = element('li', 'song skeleton');
  item.setAttribute('aria-hidden', 'true');
  const text = element('span', 'song-text');
  text.append(element('span', 'bar'), element('span', 'bar'));
  item.append(element('span', 'art'), text, element('span'));
  return item;
}

// The covers of the last three songs picked, newest on top. Only rebuilt when
// that changes, and only a newly added cover drops in.
function renderStack() {
  const picked = [...state.selected]
    .slice(-3)
    .map((id) => state.clips.find((clip) => clip.id === id))
    .filter(Boolean);
  const key = picked.map((clip) => clip.id).join(',');
  if (key === ui.stack.dataset.key) return;
  const before = new Set((ui.stack.dataset.key ?? '').split(','));
  ui.stack.dataset.key = key;
  ui.stack.replaceChildren(...picked.map((clip) => {
    const art = coverArt(clip, { lazy: false });
    if (!before.has(clip.id)) art.classList.add('fresh');
    return art;
  }));
}

function render() {
  const visible = visibleClips();
  if (state.loading && !state.clips.length) {
    ui.songs.replaceChildren(...Array.from({ length: 6 }, placeholderRow));
  } else {
    ui.songs.replaceChildren(...visible.map((clip) => {
      const { item, box } = songRow(clip);
      const selected = state.selected.has(clip.id);
      box.checked = selected;
      box.disabled = item.classList.contains('blocked') || state.busy;
      item.classList.toggle('selected', selected);
      return item;
    }));
  }

  const query = ui.filter.value.trim();
  ui.noMatch.hidden = !(query && state.clips.length && !visible.length);
  ui.noMatch.textContent = `No titles match “${query}”.`;

  const selectable = visible.filter((clip) => !blockReason(clip));
  const selectedVisible = selectable.filter((clip) => state.selected.has(clip.id)).length;
  ui.selectAll.checked = selectable.length > 0 && selectedVisible === selectable.length;
  ui.selectAll.indeterminate = selectedVisible > 0 && selectedVisible < selectable.length;
  ui.selectAll.disabled = state.busy || !selectable.length;

  const count = state.selected.size;
  const seconds = state.clips
    .filter((clip) => state.selected.has(clip.id))
    .reduce((total, clip) => total + (clip.metadata?.duration || 0), 0);
  ui.selectCount.classList.toggle('active', count > 0);
  ui.selectCount.textContent = count
    ? `${count} selected · ${formatDuration(seconds)}`
    : state.clips.length ? `${visible.length}${state.hasMore && !query ? '+' : ''} songs` : '';

  ui.loadMore.hidden = !state.hasMore;
  ui.loadMore.disabled = state.loading || state.busy;
  ui.loadMore.textContent = state.loading ? 'Loading…' : 'Load more';
  ui.workspace.disabled = state.busy || state.loading;
  ui.refresh.disabled = state.busy || state.loading;
  ui.refresh.classList.toggle('spinning', state.loading);
  ui.format.disabled = state.busy;
  ui.folder.disabled = state.busy;

  renderStack();
  ui.download.disabled = state.busy || count === 0;
  ui.download.classList.toggle('busy', state.busy);
  ui.download.style.setProperty('--done', state.batch ? state.batch.finished / state.batch.total : 0);
  ui.downloadLabel.textContent = state.batch
    ? `Downloading ${Math.min(state.batch.finished + 1, state.batch.total)} of ${state.batch.total}…`
    : count ? `Download ${count} song${count === 1 ? '' : 's'}` : 'Select songs to download';
}

// --------------------------------------------------------------------------- //
// File names
// --------------------------------------------------------------------------- //
const RESERVED_WINDOWS_NAME = /^(con|prn|aux|nul|com\d|lpt\d)$/i;

/**
 * Turn a title into a file name Chrome will accept: Windows-reserved
 * characters removed and, because Chrome's downloads API rejects them too,
 * "~" and invisible format characters (a Persian zero-width non-joiner becomes
 * a space). Falls back to the clip id when nothing is left.
 */
function safeName(name, fallback) {
  let clean = (name ?? '')
    .replace(/‌/g, ' ')
    .replace(/[<>:"/\\|?*~]|\p{Cc}|\p{Cf}/gu, '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[.\s]+|[.\s]+$/g, '');
  const chars = Array.from(clean);
  if (chars.length > 120) clean = chars.slice(0, 120).join('').trim().replace(/[.\s]+$/g, '');
  if (RESERVED_WINDOWS_NAME.test(clean)) clean = `${clean}_`;
  return clean || fallback;
}

function safeFolder(raw) {
  return raw
    .split(/[\\/]+/)
    .map((part) => safeName(part, ''))
    .filter((part) => part && part !== '.' && part !== '..')
    .join('/');
}

// --------------------------------------------------------------------------- //
// Download pipeline
// --------------------------------------------------------------------------- //
async function fetchMedia(url, onProgress) {
  if (new URL(url).protocol !== 'https:') throw new Error('Expected an HTTPS media URL');
  const response = await fetch(url, { credentials: 'omit', cache: 'no-store' });
  if (!response.ok) throw new Error(`The media server returned HTTP ${response.status}`);
  const type = (response.headers.get('content-type') ?? '').toLowerCase();
  if (['text/html', 'application/json', 'text/plain'].some((t) => type.includes(t))) {
    throw new Error(`The media server returned ${type}, not a media file`);
  }
  const total = Number(response.headers.get('content-length')) || 0;
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    if (total) onProgress(received / total);
  }
  if (!received) throw new Error('Empty media file');
  if (total && received !== total) throw new Error(`Truncated download: ${received}/${total} bytes`);
  const data = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    data.set(chunk, offset);
    offset += chunk.length;
  }
  return data;
}

function waitForDownload(downloadId) {
  return new Promise((resolve, reject) => {
    const finish = (state, error) => {
      chrome.downloads.onChanged.removeListener(listener);
      if (state === 'complete') resolve();
      else reject(new Error(`Saving failed: ${error || 'interrupted'}`));
    };
    const listener = (delta) => {
      if (delta.id !== downloadId) return;
      const state = delta.state?.current;
      if (state === 'complete' || state === 'interrupted') finish(state, delta.error?.current);
    };
    chrome.downloads.onChanged.addListener(listener);
    // It may already have finished before the listener was attached.
    chrome.downloads.search({ id: downloadId }).then(([item]) => {
      if (item?.state === 'complete' || item?.state === 'interrupted') finish(item.state, item.error);
    });
  });
}

async function saveFile(bytes, mimeType, filename) {
  const url = URL.createObjectURL(new Blob([bytes], { type: mimeType }));
  try {
    const downloadId = await chrome.downloads.download({ url, filename, conflictAction: 'uniquify', saveAs: false });
    await waitForDownload(downloadId);
    const [item] = await chrome.downloads.search({ id: downloadId });
    return item?.filename || filename;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** License -> download -> decrypt -> (MP3) -> save, for one song. */
async function processClip(clip, { format, folder, stem }, job) {
  const media = progressiveMedia(clip);

  // 1. License first, so a song we cannot license fails before downloading.
  job.status('Requesting license…');
  const license = await api('POST', '/api/mango/rights', {
    content_params: { content_id: clip.id, content_type: 'clip' },
  });
  const { key, iv } = await unwrapLicense(license.data, clip.id, [license.token, license.data?.glt]);

  // 2. Encrypted file.
  job.status('Downloading…');
  const ciphertext = await fetchMedia(media.url, (fraction) => job.progress(fraction));

  // 3. Decrypt.
  job.status('Decrypting…');
  job.progress(null);
  const plaintext = await decryptMedia(ciphertext, key, iv);
  const ext = sniffExtension(plaintext);

  // 4. Optional MP3, else the original audio untouched.
  let bytes = plaintext;
  let fileExt = ext;
  let mimeType = ext === 'webm' ? 'audio/webm' : ext === 'm4a' ? 'audio/mp4' : 'application/octet-stream';
  if (format === 'mp3') {
    job.status('Converting to MP3…');
    bytes = await encodeMp3(plaintext, {
      kbps: 320,
      title: clip.title || undefined,
      onProgress: (fraction) => job.progress(fraction),
    });
    fileExt = 'mp3';
    mimeType = 'audio/mpeg';
  }

  // 5. Save.
  job.status('Saving…');
  const filename = `${folder ? `${folder}/` : ''}${stem}.${fileExt}`;
  const savedPath = await saveFile(bytes, mimeType, filename);
  return { savedPath, warning: ext === 'bin' && format !== 'mp3' ? 'Unrecognised audio format; saved as .bin' : null };
}

function addJob(clip) {
  const item = element('li', 'job');
  const title = element('span', 'job-title', clip.title || clip.id);
  title.dir = 'auto';
  const status = element('span', 'job-status', 'Waiting…');
  const text = element('span', 'job-text');
  text.append(title, status);
  const bar = element('span', 'meter');
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', `${clip.title || clip.id} progress`);
  bar.hidden = true;
  item.append(coverArt(clip, { lazy: false }), text, element('span', 'job-icon'), bar);
  ui.jobs.append(item);
  ui.jobs.scrollTop = ui.jobs.scrollHeight;
  const settle = (className, message) => {
    item.classList.remove('active');
    item.classList.add(className);
    bar.hidden = true;
    status.textContent = message;
  };
  return {
    status(message) {
      item.classList.add('active');
      status.textContent = message;
    },
    progress(fraction) {
      bar.hidden = false;
      bar.classList.toggle('indeterminate', fraction == null);
      if (fraction == null) {
        bar.removeAttribute('aria-valuenow');
      } else {
        bar.style.setProperty('--p', fraction);
        bar.setAttribute('aria-valuenow', String(Math.round(fraction * 100)));
      }
    },
    done(message) {
      settle('done', message);
    },
    fail(message) {
      settle('failed', message);
    },
  };
}

async function downloadSelected() {
  if (state.busy) return;
  const clips = state.clips.filter((clip) => state.selected.has(clip.id) && !blockReason(clip));
  if (!clips.length) return;

  // The tab may have switched accounts since the list was loaded.
  let me;
  try {
    me = await callTab({ kind: 'session' });
  } catch (error) {
    showConnect(error.message, true);
    return;
  }
  if (me.userId !== state.me?.userId) {
    await connect();
    return;
  }

  const format = formatValue();
  const folder = safeFolder(ui.folder.value);
  state.busy = true;
  state.batch = { total: clips.length, finished: 0 };
  ui.jobs.replaceChildren();
  ui.summary.hidden = true;
  render();

  // Songs that share a title in this batch get a short clip id appended so
  // they do not overwrite one another; unique titles stay clean.
  const stems = new Map(clips.map((clip) => [clip.id, safeName(clip.title, clip.id)]));
  const stemCounts = new Map();
  for (const stem of stems.values()) stemCounts.set(stem, (stemCounts.get(stem) ?? 0) + 1);

  let done = 0;
  let failed = 0;
  // One at a time, like the web player's own license requests; a failure on
  // one song does not stop the rest.
  for (const clip of clips) {
    const job = addJob(clip);
    const stem = stemCounts.get(stems.get(clip.id)) > 1
      ? `${stems.get(clip.id)} [${clip.id.slice(0, 8)}]`
      : stems.get(clip.id);
    try {
      const { savedPath, warning } = await processClip(clip, { format, folder, stem }, job);
      job.done(warning ? `${warning}: ${savedPath}` : `Saved: ${savedPath}`);
      state.selected.delete(clip.id);
      done++;
    } catch (error) {
      job.fail(`Failed: ${error.message}`);
      failed++;
    }
    state.batch.finished++;
    render();
  }

  state.busy = false;
  state.batch = null;
  ui.summary.hidden = false;
  ui.summary.classList.toggle('has-failures', failed > 0);
  ui.summary.textContent = failed
    ? `Downloaded ${done} of ${clips.length}. ${failed} failed; the reason is under each song.`
    : `Downloaded ${done} song${done === 1 ? '' : 's'}.`;
  render();
}

// --------------------------------------------------------------------------- //
// Wiring
// --------------------------------------------------------------------------- //
// Remembered options are a convenience: if storage fails, use the defaults.
async function loadSettings() {
  const defaults = { format: 'original', folder: 'Suno', workspaceId: 'default' };
  try {
    return await chrome.storage.local.get(defaults);
  } catch (error) {
    console.warn('Could not read saved settings; using defaults.', error);
    return defaults;
  }
}

function saveSettings(values) {
  chrome.storage.local.set(values).catch((error) => console.warn('Could not save settings.', error));
}

function formatValue() {
  return ui.format.querySelector('input:checked')?.value === 'mp3' ? 'mp3' : 'original';
}

async function main() {
  const saved = await loadSettings();
  ui.format.querySelector(`input[value="${saved.format === 'mp3' ? 'mp3' : 'original'}"]`).checked = true;
  ui.folder.value = saved.folder;
  state.workspaceId = saved.workspaceId;

  ui.format.addEventListener('change', () => saveSettings({ format: formatValue() }));
  ui.folder.addEventListener('change', () => saveSettings({ folder: ui.folder.value }));
  ui.workspace.addEventListener('change', () => {
    state.workspaceId = ui.workspace.value;
    saveSettings({ workspaceId: state.workspaceId });
    loadSongs({ reset: true });
  });
  ui.refresh.addEventListener('click', () => loadSongs({ reset: true }));
  ui.loadMore.addEventListener('click', () => loadSongs({ reset: false }));
  ui.filter.addEventListener('input', render);
  ui.selectAll.addEventListener('change', () => {
    for (const clip of visibleClips()) {
      if (blockReason(clip)) continue;
      if (ui.selectAll.checked) state.selected.add(clip.id);
      else state.selected.delete(clip.id);
    }
    render();
  });
  ui.download.addEventListener('click', downloadSelected);
  ui.reconnect.addEventListener('click', connect);
  ui.openSuno.addEventListener('click', () => chrome.tabs.create({ url: 'https://suno.com/create' }));

  // Connect automatically once a suno.com tab finishes loading.
  chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
    if (!state.me && info.status === 'complete' && isSunoUrl(tab.url)) connect();
  });

  await connect();
}

main();
