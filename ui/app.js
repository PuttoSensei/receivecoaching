// Receive Coaching — UI logic.
// Talks to the FastAPI backend over HTTP + WebSocket. No frameworks.

// ---------- Backend URL resolution ----------
// Electron passes the port + auth token via the URL hash:
//   index.html#port=XXXX&token=YYYY
// In browser-standalone mode (no Electron), we fall back to the default port
// and an empty token (the backend then runs without auth — dev only).

function getBackendOrigin() {
  const m = window.location.hash.match(/port=(\d+)/);
  const port = m ? m[1] : null;
  if (port) return `http://127.0.0.1:${port}`;
  return `http://127.0.0.1:7823`;
}

function getBackendToken() {
  const m = window.location.hash.match(/token=([A-Za-z0-9_-]+)/);
  return m ? m[1] : '';
}

const BACKEND = getBackendOrigin();
const BACKEND_TOKEN = getBackendToken();
const WS_ORIGIN = BACKEND.replace(/^http/, 'ws');
const AUTH_HEADERS = BACKEND_TOKEN ? { Authorization: `Bearer ${BACKEND_TOKEN}` } : {};
const WS_QUERY = BACKEND_TOKEN ? `?token=${encodeURIComponent(BACKEND_TOKEN)}` : '';

// ---------- State ----------

const state = {
  user: 'justin',
  coaches: [],
  activeCoach: null,
  ws: null,
  currentStream: null,         // ref to the DOM bubble currently streaming into
  manualStop: false,           // user clicked Stop — finalize partial text quietly
  lastWsActivity: 0,           // for the stale-stream watchdog
  staleTimer: null,
  config: null,                // /api/config snapshot (pdf_support, stt_url, ...)
};

// ---------- Theme (system / dark / light) ----------
// Mode is stored in localStorage under 'theme'. Default is 'system', which
// follows prefers-color-scheme. 'dark' and 'light' force a specific palette.

const THEME_MODES = ['system', 'dark', 'light'];
const THEME_ICONS = { system: '◐', dark: '☾', light: '☀' };
const THEME_LABELS = { system: 'System theme', dark: 'Dark theme', light: 'Light theme' };

function getThemeMode() {
  try { return localStorage.getItem('theme') || 'system'; }
  catch { return 'system'; }
}

function effectiveTheme(mode) {
  if (mode === 'system') {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  return mode;
}

function applyTheme(mode) {
  const effective = effectiveTheme(mode);
  document.documentElement.setAttribute('data-theme', effective);
  const icon = document.getElementById('btn-theme-icon');
  const btn = document.getElementById('btn-theme');
  if (icon) icon.textContent = THEME_ICONS[mode] || '◐';
  if (btn) btn.title = `${THEME_LABELS[mode]} — click to cycle`;
}

function setThemeMode(mode) {
  if (!THEME_MODES.includes(mode)) mode = 'system';
  try { localStorage.setItem('theme', mode); } catch {}
  applyTheme(mode);
}

function cycleTheme() {
  const current = getThemeMode();
  const next = THEME_MODES[(THEME_MODES.indexOf(current) + 1) % THEME_MODES.length];
  setThemeMode(next);
}

// Re-evaluate on OS change while in 'system' mode
try {
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (getThemeMode() === 'system') applyTheme('system');
  });
} catch {}

// Apply before anything renders so first paint matches
setThemeMode(getThemeMode());

// ---------- Chat persistence (localStorage) ----------
// Keyed per user:coach so switching coaches gives each its own scrollback.

function chatStorageKey() {
  return `chat:${state.user}:${state.activeCoach?.name || '_'}`;
}

// ---------- Chat archives (per user:coach, localStorage, capped) ----------

const MAX_ARCHIVES = 20;

function archivesKey() {
  return `chatArchives:${state.user}:${state.activeCoach?.name || '_'}`;
}

function readArchives() {
  try {
    const raw = localStorage.getItem(archivesKey());
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function writeArchives(list) {
  try { localStorage.setItem(archivesKey(), JSON.stringify(list.slice(-MAX_ARCHIVES))); } catch {}
}

// Move the current live thread into the archive list. Returns true if there
// was anything to archive.
function archiveCurrentChat() {
  const turns = readChat();
  if (!turns.length) return false;
  const list = readArchives();
  const firstUser = turns.find((t) => t.role === 'user');
  list.push({
    ts: Date.now(),
    title: (firstUser?.text || 'Chat').slice(0, 60),
    turns,
  });
  writeArchives(list);
  clearStoredChat();
  return true;
}

function readChat() {
  try {
    const raw = localStorage.getItem(chatStorageKey());
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function persistChat() {
  if (!state.activeCoach) return;
  const turns = [];
  for (const msg of document.querySelectorAll('#messages .msg')) {
    const bubble = msg.querySelector('.bubble');
    if (bubble?.dataset?.transient) continue;   // error/stopped status bubbles
    const role = msg.classList.contains('user') ? 'user' : 'coach';
    const text = bubble?.dataset?.rawText || bubble?.textContent || '';
    const fallback = !!msg.querySelector('.fallback-tag');
    if (text) turns.push({ role, text, fallback });
  }
  try {
    // Cap stored history per coach to keep localStorage small
    const MAX = 80;
    const trimmed = turns.slice(-MAX);
    localStorage.setItem(chatStorageKey(), JSON.stringify(trimmed));
  } catch {
    // localStorage may be full or disabled; ignore
  }
}

function clearStoredChat() {
  try { localStorage.removeItem(chatStorageKey()); } catch {}
}

function restoreChat() {
  const turns = readChat();
  if (!turns.length) return false;
  for (const t of turns) {
    if (t.role === 'user') {
      addUserMessage(t.text);
    } else {
      // Add a coach message with its final rendered content
      const bubble = createEl('div', { class: 'bubble' });
      bubble.dataset.rawText = t.text;
      bubble.innerHTML = renderMarkdown(t.text);
      const meta = createEl('div', { class: 'meta' },
        state.activeCoach?.display_name || 'Coach');
      if (t.fallback) {
        meta.appendChild(document.createTextNode(' '));
        meta.appendChild(createEl('span', { class: 'fallback-tag' }, 'FALLBACK'));
      }
      const actions = createEl('div', { class: 'msg-actions' }, [
        createEl('button', {
          class: 'msg-action-btn',
          title: 'Copy message',
          onclick: () => copyBubbleText(bubble),
        }, 'Copy'),
        createEl('button', {
          class: 'msg-action-btn',
          title: 'Read aloud (local voice)',
          onclick: () => speakText(bubble.dataset.rawText || ''),
        }, 'Say'),
        createEl('button', {
          class: 'msg-action-btn',
          title: 'Regenerate this response (only works on the latest message)',
          onclick: () => regenerateLast(),
        }, 'Regen'),
      ]);
      const msg = createEl('div', { class: 'msg coach' }, [
        createEl('div', { class: 'msg-col' }, [
          bubble,
          createEl('div', { class: 'meta-row' }, [meta, actions]),
        ]),
      ]);
      clearWelcome();
      $('messages').appendChild(msg);
    }
  }
  scrollToBottom();
  return true;
}

// ---------- DOM shorthand ----------

const $ = (id) => document.getElementById(id);
const createEl = (tag, attrs = {}, children = []) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    if (typeof c === 'string') el.appendChild(document.createTextNode(c));
    else el.appendChild(c);
  }
  return el;
};

const escapeHtml = (s) =>
  String(s || '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// --- Minimal markdown renderer ---
// Handles what our coaches actually produce: bold, italic, inline code,
// fenced code blocks, bullet and numbered lists, headers, links.
// Everything is HTML-escaped first so user/model content can't inject HTML.
function renderMarkdown(text) {
  if (!text) return '';
  // 1. Escape HTML first
  let src = escapeHtml(text);

  // 2. Extract fenced code blocks (so inline rules don't touch them)
  const codeBlocks = [];
  src = src.replace(/```([a-z]*)?\n?([\s\S]*?)```/gi, (_, _lang, body) => {
    codeBlocks.push(body);
    return `\x00CODEBLOCK${codeBlocks.length - 1}\x00`;
  });

  // 3. Inline: `code`, **bold**, *italic*, [text](url)
  src = src
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  // 4. Line-level: split, walk, combine lists
  const lines = src.split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Header
    const hm = line.match(/^(#{1,6})\s+(.*)$/);
    if (hm) {
      const level = Math.min(hm[1].length, 6);
      out.push(`<h${level + 2}>${hm[2]}</h${level + 2}>`); // h3..h8 — visually modest
      i++;
      continue;
    }

    // Bullet list (contiguous lines starting with "- " or "* ")
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i++;
      }
      out.push('<ul>' + items.map((x) => `<li>${x}</li>`).join('') + '</ul>');
      continue;
    }

    // Numbered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i++;
      }
      out.push('<ol>' + items.map((x) => `<li>${x}</li>`).join('') + '</ol>');
      continue;
    }

    // Blank line — paragraph break
    if (line.trim() === '') {
      out.push('');
      i++;
      continue;
    }

    // Paragraph — accumulate until blank/list/header
    const para = [line];
    i++;
    while (i < lines.length &&
           lines[i].trim() !== '' &&
           !/^(#{1,6})\s/.test(lines[i]) &&
           !/^\s*[-*]\s+/.test(lines[i]) &&
           !/^\s*\d+\.\s+/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    out.push(`<p>${para.join('<br>')}</p>`);
  }

  let html = out.filter(Boolean).join('\n');

  // 5. Restore code blocks
  html = html.replace(/\x00CODEBLOCK(\d+)\x00/g, (_, n) =>
    `<pre><code>${codeBlocks[+n]}</code></pre>`);

  // 6. Unwrap paragraphs that wholly contain a block element (pre)
  html = html.replace(/<p>\s*(<pre>[\s\S]*?<\/pre>)\s*<\/p>/g, '$1');

  return html;
}

// ---------- API helpers ----------

async function api(path, options = {}) {
  const headers = { ...AUTH_HEADERS, ...(options.headers || {}) };
  const r = await fetch(BACKEND + path, { ...options, headers });
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch {}
    const suffix = detail ? `: ${String(detail).slice(0, 140)}` : '';
    throw new Error(`${options.method || 'GET'} ${path} -> ${r.status}${suffix}`);
  }
  return r.json();
}

// ---------- Coach list (sidebar) ----------

async function loadCoaches() {
  try {
    state.coaches = await api('/api/coaches');
  } catch (e) {
    $('coach-list').innerHTML =
      `<div class="loading" style="color: var(--bad);">Backend not reachable.<br>${escapeHtml(e.message)}</div>`;
    return;
  }
  renderCoachList();

  // Pick a default coach: use the user's last_coach if available, else general.
  try {
    const mem = await api(`/api/memory/${encodeURIComponent(state.user)}`);
    const last = mem.last_coach;
    const pick = state.coaches.find((c) => c.name === last) ||
                 state.coaches.find((c) => c.name === 'general') ||
                 state.coaches[0];
    if (pick) setActiveCoach(pick.name);
  } catch (e) {
    const pick = state.coaches.find((c) => c.name === 'general') || state.coaches[0];
    if (pick) setActiveCoach(pick.name);
  }
}

function renderCoachList() {
  const q = $('coach-search').value.trim().toLowerCase();
  const list = $('coach-list');
  list.innerHTML = '';
  const filtered = q
    ? state.coaches.filter((c) =>
        c.name.toLowerCase().includes(q) ||
        c.display_name.toLowerCase().includes(q) ||
        c.description.toLowerCase().includes(q))
    : state.coaches;

  if (filtered.length === 0) {
    list.innerHTML = '<div class="loading">No coaches match.</div>';
    return;
  }

  for (const c of filtered) {
    const item = createEl('div', {
      class: 'coach-item' + (state.activeCoach?.name === c.name ? ' active' : ''),
      title: c.description,
      onclick: () => setActiveCoach(c.name),
    }, [
      createEl('div', { class: 'name' }, c.display_name),
      createEl('div', { class: 'desc' }, c.description),
      createEl('div', { class: 'srcs' },
        c.source_count > 0 ? `${c.source_count} source${c.source_count > 1 ? 's' : ''}` : 'no sources'),
    ]);
    list.appendChild(item);
  }
}

function setActiveCoach(name) {
  const coach = state.coaches.find((c) => c.name === name);
  if (!coach) return;
  stopSpeaking();
  state.activeCoach = coach;
  $('active-coach-name').textContent = coach.display_name;
  $('active-coach-desc').textContent = coach.description;
  $('active-coach-model').textContent = coach.model;
  $('active-coach-sources').textContent =
    coach.source_count > 0 ? `${coach.source_count} source${coach.source_count > 1 ? 's' : ''}` : '0 sources';

  // Clear the message list, then either restore saved history or show welcome
  $('messages').innerHTML = '';
  const restored = restoreChat();
  if (!restored) {
    const welcome = createEl('div', { class: 'welcome' }, [
      createEl('h3', {}, `${coach.display_name} is ready.`),
      createEl('p', { class: 'muted' }, coach.description),
    ]);
    $('messages').appendChild(welcome);
  }

  // Enable composer
  $('input').disabled = false;
  $('send-btn').disabled = false;
  $('input').focus();

  // Update sidebar highlight
  renderCoachList();
}

// ---------- Message rendering ----------

function clearWelcome() {
  const w = document.querySelector('.welcome');
  if (w) w.remove();
}

function addUserMessage(text) {
  clearWelcome();
  const bubble = createEl('div', { class: 'bubble' }, text);
  bubble.dataset.rawText = text;
  const meta = createEl('div', { class: 'meta' }, 'you');
  const actions = createEl('div', { class: 'msg-actions' }, [
    createEl('button', {
      class: 'msg-action-btn',
      title: 'Copy message',
      onclick: () => copyBubbleText(bubble),
    }, 'Copy'),
    createEl('button', {
      class: 'msg-action-btn',
      title: 'Edit and resend (latest turn only)',
      onclick: () => editUserMessage(msg),
    }, 'Edit'),
  ]);
  const msg = createEl('div', { class: 'msg user' }, [
    createEl('div', { class: 'msg-col' }, [
      bubble,
      createEl('div', { class: 'meta-row' }, [meta, actions]),
    ]),
  ]);
  $('messages').appendChild(msg);
  scrollToBottom();
}

// Pull the latest user turn back into the composer, discarding it and any
// reply it produced. Only the last exchange is editable (no mid-thread forks).
function editUserMessage(msgEl) {
  if (state.currentStream) return;
  const msgs = [...document.querySelectorAll('#messages .msg')];
  const i = msgs.indexOf(msgEl);
  const isLast = i === msgs.length - 1;
  const isPenultimateWithReply =
    i === msgs.length - 2 && msgs[msgs.length - 1].classList.contains('coach');
  if (!isLast && !isPenultimateWithReply) return;
  const text = msgEl.querySelector('.bubble')?.dataset?.rawText || '';
  if (!text) return;
  if (isPenultimateWithReply) msgs[msgs.length - 1].remove();
  msgEl.remove();
  persistChat();
  const input = $('input');
  input.value = text;
  input.focus();
  input.dispatchEvent(new Event('input'));   // trigger auto-resize
}

function addCoachMessagePlaceholder() {
  clearWelcome();
  const bubble = createEl('div', { class: 'bubble streaming' }, [
    createEl('div', { class: 'thinking-indicator' }, [
      createEl('span', {}), createEl('span', {}), createEl('span', {}),
    ]),
  ]);
  // Track streamed text on the element itself so we can re-render markdown
  bubble.dataset.rawText = '';

  const meta = createEl('div', { class: 'meta' }, state.activeCoach?.display_name || 'Coach');
  const actions = createEl('div', { class: 'msg-actions' }, [
    createEl('button', {
      class: 'msg-action-btn',
      title: 'Copy message',
      onclick: () => copyBubbleText(bubble),
    }, 'Copy'),
    createEl('button', {
      class: 'msg-action-btn',
      title: 'Read aloud (local voice)',
      onclick: () => speakText(bubble.dataset.rawText || ''),
    }, 'Say'),
    createEl('button', {
      class: 'msg-action-btn',
      title: 'Regenerate this response (only works on the latest message)',
      onclick: () => regenerateLast(),
    }, 'Regen'),
  ]);

  const msg = createEl('div', { class: 'msg coach' }, [
    createEl('div', { class: 'msg-col' }, [
      bubble,
      createEl('div', { class: 'meta-row' }, [meta, actions]),
    ]),
  ]);
  $('messages').appendChild(msg);
  scrollToBottom();
  return { bubble, msg };
}

function appendToCoachBubble(bubble, text) {
  // First token: clear the thinking indicator
  if (bubble.querySelector('.thinking-indicator')) {
    bubble.innerHTML = '';
  }
  bubble.dataset.rawText = (bubble.dataset.rawText || '') + text;
  bubble.innerHTML = renderMarkdown(bubble.dataset.rawText);
  scrollToBottom();
}

function finaliseCoachBubble(bubble, { used_llm }) {
  bubble.classList.remove('streaming');
  if (used_llm === false) {
    const meta = bubble.parentElement.querySelector('.meta');
    if (meta && !meta.querySelector('.fallback-tag')) {
      const tag = createEl('span', { class: 'fallback-tag' }, 'FALLBACK');
      meta.appendChild(document.createTextNode(' '));
      meta.appendChild(tag);
    }
  }
  // Persist this turn to localStorage for the active user:coach
  persistChat();
  if (autoReadEnabled() && bubble.dataset.rawText) {
    speakText(bubble.dataset.rawText);
  }
}

// ---------- Voice output: local TTS via speechSynthesis (SAPI voices) ----------

function autoReadEnabled() {
  try { return localStorage.getItem('autoRead') === '1'; } catch { return false; }
}

function stripForSpeech(md) {
  return String(md || '')
    .replace(/```[\s\S]*?```/g, ' code block omitted. ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*?([^*\n]+)\*\*?/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function speakText(text) {
  // speechSynthesis on Windows uses the locally installed SAPI/OneCore
  // voices — nothing is sent anywhere.
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(stripForSpeech(text));
    speechSynthesis.speak(u);
  } catch {}
}

function stopSpeaking() {
  try { speechSynthesis.cancel(); } catch {}
}

function copyBubbleText(bubble) {
  const text = bubble.dataset.rawText || bubble.textContent || '';
  navigator.clipboard.writeText(text).then(() => {
    // Brief visual ack
    const btn = bubble.parentElement.querySelector('.msg-action-btn');
    if (!btn) return;
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }).catch(() => {});
}

function scrollToBottom() {
  const m = $('messages');
  m.scrollTop = m.scrollHeight;
}

// ---------- Streaming lifecycle helpers ----------

function setSendButtonMode(mode) {
  const btn = $('send-btn');
  if (mode === 'stop') {
    btn.textContent = 'Stop';
    btn.classList.add('stop');
    btn.disabled = false;           // must stay clickable to allow Stop
  } else {
    btn.textContent = 'Send';
    btn.classList.remove('stop');
    btn.disabled = !state.activeCoach;
  }
}

function endStreamUi() {
  stopStaleWatchdog();
  state.currentStream = null;
  $('input').disabled = false;
  setSendButtonMode('send');
  $('input').focus();
}

function stopStreaming() {
  if (!state.currentStream) return;
  state.manualStop = true;
  // Closing the socket is the abort mechanism: the server's next send fails
  // and the UI finalizes with whatever text already arrived (in ws.onclose).
  try { state.ws?.close(); } catch {}
}

// Server heartbeats every 10s during generation; a long silence means the
// backend or model died mid-stream. Close the socket so onclose cleans up
// instead of leaving the thinking dots spinning forever.
const STALE_STREAM_MS = 45000;

function startStaleWatchdog() {
  state.lastWsActivity = Date.now();
  stopStaleWatchdog();
  state.staleTimer = setInterval(() => {
    if (!state.currentStream) { stopStaleWatchdog(); return; }
    if (Date.now() - state.lastWsActivity > STALE_STREAM_MS) {
      try { state.ws?.close(); } catch {}
    }
  }, 5000);
}

function stopStaleWatchdog() {
  if (state.staleTimer) {
    clearInterval(state.staleTimer);
    state.staleTimer = null;
  }
}

// ---------- Streaming chat via WebSocket ----------

function ensureWebSocket() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) return state.ws;
  if (state.ws && state.ws.readyState === WebSocket.CONNECTING) return state.ws;

  const ws = new WebSocket(WS_ORIGIN + '/ws/chat' + WS_QUERY);
  ws.onopen = () => { /* ready */ };
  ws.onmessage = (ev) => {
    state.lastWsActivity = Date.now();
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (!state.currentStream) return;
    const { bubble } = state.currentStream;
    switch (data.type) {
      case 'thinking':
      case 'heartbeat':
        return;
      case 'token':
        appendToCoachBubble(bubble, data.text || '');
        return;
      case 'done':
        finaliseCoachBubble(bubble, { used_llm: !!data.used_llm });
        endStreamUi();
        return;
      case 'error': {
        bubble.innerHTML = '';
        bubble.textContent = `Error: ${data.message}`;
        bubble.dataset.transient = '1';   // not a real coach turn — don't persist
        bubble.classList.remove('streaming');
        endStreamUi();
        return;
      }
    }
  };
  ws.onclose = () => {
    state.ws = null;
    if (state.currentStream) {
      const { bubble } = state.currentStream;
      const partial = bubble.dataset.rawText || '';
      if (state.manualStop && partial) {
        // User hit Stop mid-stream: keep the partial text as the message.
        finaliseCoachBubble(bubble, { used_llm: true });
      } else {
        bubble.innerHTML = '';
        bubble.textContent = state.manualStop
          ? 'Stopped.'
          : 'Connection closed before the response finished.';
        bubble.dataset.transient = '1';   // status text, not a coach turn
        bubble.classList.remove('streaming');
      }
      endStreamUi();
    }
    state.manualStop = false;
  };
  ws.onerror = () => { /* surfaced via onclose */ };
  state.ws = ws;
  return ws;
}

async function sendMessage(text) {
  if (!state.activeCoach) return;

  // Gather prior turns (before adding the new user message) to send as history.
  // We cap at 10 turns (~5 pairs) to keep context size sane; the backend applies
  // its own cap as a safety net.
  const history = collectHistoryForRequest(10);

  addUserMessage(text);
  persistChat(); // save user turn immediately
  requestCoachResponse(text, { history });
}

// Shared streaming request path for both a fresh send and a regenerate.
function requestCoachResponse(text, { history = [], regenerate = false } = {}) {
  const coach = state.activeCoach;
  if (!coach) return;

  const { bubble, msg } = addCoachMessagePlaceholder();
  state.currentStream = { bubble, msg };
  state.manualStop = false;

  // Lock the input while streaming; the send button becomes Stop.
  $('input').disabled = true;
  setSendButtonMode('stop');
  startStaleWatchdog();

  const ws = ensureWebSocket();
  const payload = JSON.stringify({
    user: state.user,
    coach: coach.name,
    message: text,
    history,
    regenerate,
  });

  if (ws.readyState === WebSocket.OPEN) {
    ws.send(payload);
  } else {
    ws.addEventListener('open', () => ws.send(payload), { once: true });
  }
}

// Regenerate the LAST coach response: remove its bubble and replay the user
// message that produced it (without re-recording the turn in memory).
function regenerateLast() {
  if (state.currentStream || !state.activeCoach) return;
  const msgs = [...document.querySelectorAll('#messages .msg')];
  if (msgs.length < 2) return;
  const last = msgs[msgs.length - 1];
  const prev = msgs[msgs.length - 2];
  if (!last.classList.contains('coach') || !prev.classList.contains('user')) return;
  const userText = prev.querySelector('.bubble')?.dataset?.rawText || '';
  if (!userText) return;

  // History = everything before the user message being replayed.
  const all = collectHistoryForRequest(1000);
  // Drop the trailing coach reply + the user prompt from history.
  let history = all;
  if (history.length && history[history.length - 1].role === 'coach') history = history.slice(0, -1);
  if (history.length && history[history.length - 1].role === 'user') history = history.slice(0, -1);
  history = history.slice(-10);

  last.remove();
  persistChat();
  requestCoachResponse(userText, { history, regenerate: true });
}

function exportChat() {
  if (!state.activeCoach) return;
  const turns = collectHistoryForRequest(1000); // effectively all
  if (!turns.length) {
    alert('Nothing to export yet.');
    return;
  }
  const now = new Date();
  const stamp = now.toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const lines = [
    `# ${state.activeCoach.display_name}`,
    '',
    `_Conversation with ${state.activeCoach.display_name} on ${now.toLocaleString()}._`,
    `_User: ${state.user} · Coach: \`${state.activeCoach.name}\` · Model: \`${state.activeCoach.model}\`_`,
    '',
    '---',
    '',
  ];
  for (const t of turns) {
    const who = t.role === 'user' ? '**You**' : `**${state.activeCoach.display_name}**`;
    lines.push(`### ${who}`);
    lines.push('');
    lines.push(t.content);
    lines.push('');
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `coach-${state.activeCoach.name}-${stamp}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function collectHistoryForRequest(maxTurns) {
  // Walk the DOM (which reflects both restored + live messages) and collect
  // up to maxTurns of {role, content}. Skip any message currently streaming
  // (placeholder with no text).
  const turns = [];
  for (const el of document.querySelectorAll('#messages .msg')) {
    const bubble = el.querySelector('.bubble');
    if (bubble?.dataset?.transient) continue;   // error/stopped status bubbles
    const role = el.classList.contains('user') ? 'user' : 'coach';
    const text = (bubble?.dataset?.rawText || '').trim();
    if (!text) continue;
    turns.push({ role, content: text });
  }
  return turns.slice(-maxTurns);
}

// ---------- Voice input: optional local whisper.cpp server ----------
// Enabled only when the backend reports a configured RECEIVE_COACH_STT_URL
// (must be a 127.0.0.1 URL — the CSP blocks anything else by design).

let micSession = null;   // { ctx, proc, srcNode, stream, chunks, sampleRate }

function sttConfigured() {
  return !!(state.config && state.config.stt_url);
}

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new AudioContext();
  const srcNode = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  srcNode.connect(proc);
  proc.connect(ctx.destination);
  micSession = { ctx, proc, srcNode, stream, chunks, sampleRate: ctx.sampleRate };
}

async function stopRecordingAndTranscribe() {
  const m = micSession;
  micSession = null;
  if (!m) return '';
  try { m.proc.disconnect(); m.srcNode.disconnect(); } catch {}
  m.stream.getTracks().forEach((t) => t.stop());
  await m.ctx.close();

  const total = m.chunks.reduce((n, c) => n + c.length, 0);
  const samples = new Float32Array(total);
  let off = 0;
  for (const c of m.chunks) { samples.set(c, off); off += c.length; }

  const wav = encodeWav16kMono(samples, m.sampleRate);
  const form = new FormData();
  form.append('file', new Blob([wav], { type: 'audio/wav' }), 'speech.wav');
  form.append('response_format', 'json');
  const r = await fetch(state.config.stt_url, { method: 'POST', body: form });
  if (!r.ok) throw new Error(`STT server ${r.status}`);
  const j = await r.json();
  return (j.text || '').trim();
}

// Linear-resample to 16 kHz mono 16-bit PCM WAV (what whisper.cpp expects).
function encodeWav16kMono(samples, inRate) {
  const outRate = 16000;
  const ratio = inRate / outRate;
  const outLen = Math.floor(samples.length / ratio);
  const pcm = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, samples.length - 1);
    const frac = pos - i0;
    const v = samples[i0] * (1 - frac) + samples[i1] * frac;
    pcm[i] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const dv = new DataView(buf);
  const writeStr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  dv.setUint32(4, 36 + pcm.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true);          // PCM
  dv.setUint16(22, 1, true);          // mono
  dv.setUint32(24, outRate, true);
  dv.setUint32(28, outRate * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  writeStr(36, 'data');
  dv.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return buf;
}

function initMic() {
  const btn = $('mic-btn');
  if (!btn) return;
  if (!sttConfigured()) { btn.style.display = 'none'; return; }
  btn.style.display = '';
  btn.addEventListener('click', async () => {
    if (micSession) {
      // Stop → transcribe → insert
      btn.classList.remove('recording');
      btn.textContent = '…';
      btn.disabled = true;
      try {
        const text = await stopRecordingAndTranscribe();
        const input = $('input');
        if (text) {
          input.value = input.value ? `${input.value.trimEnd()} ${text}` : text;
          input.dispatchEvent(new Event('input'));
          input.focus();
        }
      } catch (e) {
        alert(`Transcription failed: ${e.message}\nIs your whisper server running at ${state.config.stt_url}?`);
      } finally {
        btn.textContent = '🎤';
        btn.disabled = false;
      }
    } else {
      try {
        await startRecording();
        btn.classList.add('recording');
        btn.textContent = '⏹';
      } catch (e) {
        alert(`Microphone unavailable: ${e.message}`);
      }
    }
  });
}

// ---------- Composer ----------

function setupComposer() {
  const input = $('input');
  const send = $('send-btn');

  const autoResize = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  };

  input.addEventListener('input', autoResize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      tryToSend();
    }
  });
  send.addEventListener('click', tryToSend);

  function tryToSend() {
    // While a response is streaming, the button is a Stop control.
    if (state.currentStream) {
      stopStreaming();
      return;
    }
    const text = input.value.trim();
    if (!text || input.disabled) return;
    input.value = '';
    autoResize();
    sendMessage(text);
  }
}

// ---------- Drawer (memory / sources) ----------

function openDrawer(title, renderFn) {
  $('drawer-title').textContent = title;
  $('drawer-body').innerHTML = '';
  $('drawer').classList.remove('drawer-closed');
  renderFn($('drawer-body'));
}

function closeDrawer() {
  $('drawer').classList.add('drawer-closed');
}

async function renderMemoryDrawer(container) {
  container.innerHTML = '<div class="empty-state">Loading…</div>';
  let mem;
  try {
    mem = await api(`/api/memory/${encodeURIComponent(state.user)}`);
  } catch (e) {
    container.innerHTML = `<div class="empty-state" style="color: var(--bad);">${escapeHtml(e.message)}</div>`;
    return;
  }

  container.innerHTML = '';

  const sessions = mem.sessions || [];
  const coaches = {};
  sessions.forEach((s) => { if (s.coach) coaches[s.coach] = (coaches[s.coach] || 0) + 1; });

  // Stats
  container.appendChild(createEl('div', { class: 'stats-grid' }, [
    createEl('div', { class: 'stat-box' }, [
      createEl('div', { class: 'stat-num' }, String(sessions.length)),
      createEl('div', { class: 'stat-label' }, 'Sessions'),
    ]),
    createEl('div', { class: 'stat-box' }, [
      createEl('div', { class: 'stat-num' }, String(Object.keys(coaches).length)),
      createEl('div', { class: 'stat-label' }, 'Coaches used'),
    ]),
    createEl('div', { class: 'stat-box' }, [
      createEl('div', { class: 'stat-num' },
        String((mem.accountability?.active_commitments || []).length)),
      createEl('div', { class: 'stat-label' }, 'Commitments'),
    ]),
    createEl('div', { class: 'stat-box' }, [
      createEl('div', { class: 'stat-num' },
        String((mem.patterns?.recurring_blocks || []).length)),
      createEl('div', { class: 'stat-label' }, 'Patterns'),
    ]),
  ]));

  // Mood over time — one cell per session (last 30), colored by emotional state
  const MOOD_COLORS = {
    hopeful: '#9ece6a', overwhelmed: '#e0af68', stuck: '#7aa2f7',
    sad: '#7dcfff', angry: '#f7768e', confused: '#bb9af7', unclear: '#565f89',
  };
  const recentMood = sessions.slice(-30);
  if (recentMood.length >= 3) {
    const strip = createEl('div', { class: 'mood-strip' });
    for (const s of recentMood) {
      const emo = s.emotional_state || 'unclear';
      strip.appendChild(createEl('span', {
        class: 'mood-cell',
        title: `${s.date || ''} · ${emo}${s.coach ? ' · ' + s.coach : ''}`,
        style: `background:${MOOD_COLORS[emo] || MOOD_COLORS.unclear}`,
      }));
    }
    const legend = createEl('div', { class: 'mood-legend' },
      Object.entries(MOOD_COLORS).map(([name, color]) =>
        createEl('span', { class: 'mood-legend-item' }, [
          createEl('span', { class: 'mood-dot', style: `background:${color}` }),
          name,
        ])));
    container.appendChild(createEl('div', { class: 'drawer-section' }, [
      createEl('h4', {}, 'Mood over time'),
      strip,
      legend,
    ]));
  }

  // Insights — on-demand LLM recap of recent sessions (local model)
  const insightsOut = createEl('div', { class: 'drawer-card insights-output', style: 'display:none;' });
  const insightsBtn = createEl('button', {
    class: 'icon-btn',
    onclick: async () => {
      insightsBtn.disabled = true;
      insightsBtn.textContent = 'Analyzing recent sessions…';
      try {
        const r = await api(`/api/insights/${encodeURIComponent(state.user)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        insightsOut.style.display = '';
        insightsOut.innerHTML = renderMarkdown(r.insights || '');
        insightsBtn.textContent = `Regenerate insights (${r.sessions_analyzed} sessions)`;
      } catch (e) {
        insightsOut.style.display = '';
        insightsOut.textContent = `Could not generate insights: ${e.message}`;
        insightsBtn.textContent = 'Generate insights';
      } finally {
        insightsBtn.disabled = false;
      }
    },
  }, 'Generate insights');
  container.appendChild(createEl('div', { class: 'drawer-section' }, [
    createEl('h4', {}, 'Insights'),
    createEl('div', { class: 'drawer-card' }, [
      createEl('div', { class: 'body' },
        'A short report over your recent sessions — themes, emotional trend, progress, and one suggestion. Generated by your local model; nothing leaves this machine.'),
    ]),
    insightsBtn,
    insightsOut,
  ]));

  // Profile
  const profile = mem.user_profile || {};
  const profileSection = createEl('div', { class: 'drawer-section' }, [
    createEl('h4', {}, 'Profile'),
    createEl('div', { class: 'drawer-card' }, [
      createEl('div', { class: 'label' }, 'Name'),
      createEl('div', { class: 'body' }, profile.name || 'not set'),
    ]),
    createEl('div', { class: 'drawer-card' }, [
      createEl('div', { class: 'label' }, 'Current focus'),
      createEl('div', { class: 'body' }, profile.current_focus || 'not set'),
    ]),
    createEl('div', { class: 'drawer-card' }, [
      createEl('div', { class: 'label' }, 'Last coach'),
      createEl('div', { class: 'body' }, mem.last_coach || 'none'),
    ]),
  ]);
  container.appendChild(profileSection);

  // Goals
  if ((profile.goals || []).length) {
    const goalsDiv = createEl('div', { class: 'drawer-section' }, [createEl('h4', {}, 'Goals')]);
    for (const g of profile.goals) {
      goalsDiv.appendChild(createEl('div', { class: 'drawer-card' }, [
        createEl('div', { class: 'body' }, g),
      ]));
    }
    container.appendChild(goalsDiv);
  }

  // Commitments
  const commitments = mem.accountability?.active_commitments || [];
  if (commitments.length) {
    const cDiv = createEl('div', { class: 'drawer-section' }, [
      createEl('h4', {}, 'Active commitments'),
    ]);
    for (const c of commitments.slice(-10).reverse()) {
      cDiv.appendChild(createEl('div', { class: 'drawer-card' }, [
        createEl('div', { class: 'body' }, c),
      ]));
    }
    container.appendChild(cDiv);
  }

  // Recent sessions
  const recent = sessions.slice(-10).reverse();
  const sDiv = createEl('div', { class: 'drawer-section' }, [
    createEl('h4', {}, `Recent sessions (${recent.length}${sessions.length > recent.length ? ` of ${sessions.length}` : ''})`),
  ]);
  if (!recent.length) {
    sDiv.appendChild(createEl('div', { class: 'empty-state' }, 'No sessions yet.'));
  } else {
    for (const s of recent) {
      const item = createEl('div', { class: 'session-item' }, [
        createEl('div', { class: 'top' }, [
          createEl('div', { class: 'chip coach' }, s.coach || '?'),
          createEl('div', { class: 'date' }, s.date || ''),
        ]),
        createEl('div', { class: 'issue' }, s.main_issue || s.summary || ''),
      ]);
      if (s.action_step) {
        item.appendChild(createEl('div', { class: 'action' }, '→ ' + s.action_step));
      }
      sDiv.appendChild(item);
    }
  }
  container.appendChild(sDiv);

  // Patterns
  const patterns = mem.patterns?.recurring_blocks || [];
  if (patterns.length) {
    const pDiv = createEl('div', { class: 'drawer-section' }, [
      createEl('h4', {}, `Recurring patterns (${patterns.length})`),
    ]);
    for (const p of patterns.slice(-10).reverse()) {
      pDiv.appendChild(createEl('div', { class: 'drawer-card' }, [
        createEl('div', { class: 'label' }, p.pattern),
        createEl('div', { class: 'body' }, '"' + (p.example || '') + '"'),
      ]));
    }
    container.appendChild(pDiv);
  }
}

function renderHistoryDrawer(container) {
  if (!state.activeCoach) {
    container.innerHTML = '<div class="empty-state">Select a coach first.</div>';
    return;
  }
  container.innerHTML = '';
  const archives = readArchives().slice().reverse();   // newest first

  container.appendChild(createEl('div', { class: 'drawer-section' }, [
    createEl('h4', {}, `Archived chats with ${state.activeCoach.display_name} (${archives.length})`),
    createEl('div', { class: 'drawer-card' }, [
      createEl('div', { class: 'body' },
        '"New chat" archives the current conversation here. Restoring swaps it with the current one (which gets archived).'),
    ]),
  ]));

  if (!archives.length) {
    container.appendChild(createEl('div', { class: 'empty-state' }, 'No archived chats yet.'));
    return;
  }

  const listDiv = createEl('div', { class: 'drawer-section' });
  for (const a of archives) {
    const when = new Date(a.ts).toLocaleString();
    const item = createEl('div', { class: 'session-item' }, [
      createEl('div', { class: 'top' }, [
        createEl('div', { class: 'chip coach' }, `${a.turns.length} turns`),
        createEl('div', { class: 'date' }, when),
      ]),
      createEl('div', { class: 'issue' }, a.title || 'Chat'),
      createEl('div', { class: 'msg-actions', style: 'margin-top:6px;' }, [
        createEl('button', {
          class: 'msg-action-btn',
          onclick: () => {
            // Current thread (if any) gets archived, selected one becomes live.
            const remaining = readArchives().filter((x) => x.ts !== a.ts);
            writeArchives(remaining);
            archiveCurrentChat();
            try { localStorage.setItem(chatStorageKey(), JSON.stringify(a.turns)); } catch {}
            setActiveCoach(state.activeCoach.name);
            closeDrawer();
          },
        }, 'Restore'),
        createEl('button', {
          class: 'msg-action-btn',
          onclick: () => {
            if (!confirm(`Delete this archived chat? (${a.turns.length} turns)`)) return;
            writeArchives(readArchives().filter((x) => x.ts !== a.ts));
            renderHistoryDrawer(container);
          },
        }, 'Delete'),
      ]),
    ]);
    listDiv.appendChild(item);
  }
  container.appendChild(listDiv);
}

async function renderSourcesDrawer(container) {
  if (!state.activeCoach) {
    container.innerHTML = '<div class="empty-state">Select a coach first.</div>';
    return;
  }
  const coachName = state.activeCoach.name;
  container.innerHTML = '<div class="empty-state">Loading…</div>';

  let sources;
  try {
    sources = await api(`/api/coaches/${coachName}/sources`);
  } catch (e) {
    container.innerHTML = `<div class="empty-state" style="color: var(--bad);">${escapeHtml(e.message)}</div>`;
    return;
  }

  container.innerHTML = '';

  // Info
  const pdfOk = !!state.config?.pdf_support;
  const extLabel = pdfOk ? '.txt, .md, or .pdf' : '.txt or .md';
  const extRegex = pdfOk ? /\.(txt|md|pdf)$/i : /\.(txt|md)$/i;

  container.appendChild(createEl('div', { class: 'drawer-section' }, [
    createEl('h4', {}, `Sources for ${state.activeCoach.display_name}`),
    createEl('div', { class: 'drawer-card' }, [
      createEl('div', { class: 'body' },
        `Drop ${extLabel} files to ground this coach in your own material. On the next question you ask, the top relevant chunks are automatically retrieved.`
        + (pdfOk ? '' : ' (PDF support: pip install pypdf, then restart.)')),
    ]),
  ]));

  // Drop zone
  const drop = createEl('div', { class: 'source-drop' }, [
    createEl('h4', {}, 'Add source files'),
    createEl('p', {}, `Click to pick, or drag ${extLabel} files here`),
  ]);
  container.appendChild(drop);

  const handleFiles = async (files) => {
    for (const f of files) {
      if (!extRegex.test(f.name)) {
        alert(`Skipping ${f.name}: only ${extLabel} supported`);
        continue;
      }
      const form = new FormData();
      form.append('file', f);
      try {
        const r = await fetch(`${BACKEND}/api/coaches/${coachName}/sources/upload`, {
          method: 'POST',
          body: form,
          headers: { ...AUTH_HEADERS },
        });
        if (!r.ok) throw new Error(`Upload ${r.status}`);
      } catch (err) {
        alert(`Upload failed: ${err.message}`);
      }
    }
    await renderSourcesDrawer(container);
    // Also refresh coach list so source counts update
    await refreshCoachCounts();
  };

  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('dragover'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('dragover');
    handleFiles([...e.dataTransfer.files]);
  });
  drop.addEventListener('click', async () => {
    if (window.coachApi?.pickFilesToUpload) {
      const picks = await window.coachApi.pickFilesToUpload(coachName);
      if (!picks.length) return;
      const fs = [];
      for (const p of picks) {
        const res = await window.coachApi.readFile(p.path);
        const bytes = Uint8Array.from(atob(res.data), (c) => c.charCodeAt(0));
        const type = /\.pdf$/i.test(res.name) ? 'application/pdf' : 'text/plain';
        fs.push(new File([bytes], res.name, { type }));
      }
      handleFiles(fs);
    } else {
      // Browser fallback: create a hidden input
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = pdfOk ? '.txt,.md,.pdf' : '.txt,.md';
      inp.multiple = true;
      inp.onchange = () => handleFiles([...inp.files]);
      inp.click();
    }
  });

  // Existing files
  const filesSection = createEl('div', { class: 'drawer-section' }, [
    createEl('h4', {}, `Current source files (${sources.files.length})`),
  ]);
  if (!sources.files.length) {
    filesSection.appendChild(createEl('div', { class: 'empty-state' }, 'No files yet.'));
  } else {
    for (const f of sources.files) {
      const item = createEl('div', { class: 'source-item' + (f.is_readme ? ' readme' : '') });
      item.appendChild(createEl('div', { class: 'name' }, f.name));
      item.appendChild(createEl('div', { class: 'size' }, `${(f.size / 1024).toFixed(1)} KB`));
      if (!f.is_readme) {
        item.appendChild(createEl('button', {
          class: 'source-delete',
          onclick: async () => {
            if (!confirm(`Delete ${f.name}?`)) return;
            try {
              await fetch(`${BACKEND}/api/coaches/${coachName}/sources/${encodeURIComponent(f.name)}`, {
                method: 'DELETE',
                headers: { ...AUTH_HEADERS },
              });
              await renderSourcesDrawer(container);
              await refreshCoachCounts();
            } catch (err) {
              alert(`Delete failed: ${err.message}`);
            }
          },
        }, 'Delete'));
      }
      filesSection.appendChild(item);
    }
  }
  container.appendChild(filesSection);

  // Reindex button
  const reidx = createEl('button', {
    class: 'icon-btn',
    style: 'margin-top: 12px;',
    onclick: async () => {
      reidx.textContent = 'Re-embedding…';
      try {
        const r = await api(`/api/coaches/${coachName}/reindex`, { method: 'POST' });
        reidx.textContent = `Re-indexed: ${r.chunks} chunks`;
        setTimeout(() => { reidx.textContent = 'Re-index sources'; }, 2500);
      } catch (e) {
        reidx.textContent = `Error: ${e.message}`;
      }
    },
  }, 'Re-index sources');
  container.appendChild(reidx);
}

async function refreshCoachCounts() {
  try {
    state.coaches = await api('/api/coaches');
  } catch { return; }
  // Preserve active coach
  const active = state.activeCoach?.name;
  renderCoachList();
  if (active) {
    const found = state.coaches.find((c) => c.name === active);
    if (found) {
      state.activeCoach = found;
      $('active-coach-sources').textContent =
        found.source_count > 0 ? `${found.source_count} source${found.source_count > 1 ? 's' : ''}` : '0 sources';
    }
  }
}

// ---------- Settings modal ----------

async function openSettings() {
  let cfg;
  try {
    cfg = await api('/api/config');
  } catch (e) {
    cfg = { error: e.message };
  }

  $('modal-title').textContent = 'Settings';
  const c = $('modal-content');
  c.innerHTML = '';

  const rows = [
    ['Backend URL', cfg.base_url || '?'],
    ['Embed model', cfg.embed_model || '?'],
    ['Embed format', cfg.embed_format || 'auto'],
    ['API key set', cfg.has_api_key ? 'yes' : 'no'],
  ];
  for (const [k, v] of rows) {
    c.appendChild(createEl('div', { class: 'modal-row' }, [
      createEl('label', {}, k),
      createEl('code', {}, String(v)),
    ]));
  }

  // Chat model override (applies to all coaches until cleared)
  const modelSel = createEl('select', {
    id: 'settings-model-select',
    style: 'padding:8px 10px; background: var(--panel-2); border-radius:6px; width:100%;',
  });
  modelSel.appendChild(createEl('option', { value: '' }, '(per-coach default)'));
  const modelStatus = createEl('div', {
    style: 'font-size:11.5px; color: var(--text-muted); margin-top:4px;',
  }, 'Loading model list…');
  c.appendChild(createEl('div', { class: 'modal-row' }, [
    createEl('label', {}, 'Chat model (overrides every coach until cleared)'),
    modelSel,
    modelStatus,
  ]));
  (async () => {
    try {
      const m = await api('/api/models');
      for (const name of m.models) modelSel.appendChild(createEl('option', { value: name }, name));
      if (m.override) modelSel.value = m.override;
      modelStatus.textContent = m.override
        ? `Override active: ${m.override}`
        : 'Using each coach\'s own model.';
      modelSel.addEventListener('change', async () => {
        try {
          const res = await api('/api/config/model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: modelSel.value || null }),
          });
          modelStatus.textContent = res.override
            ? `Override active: ${res.override}`
            : 'Using each coach\'s own model.';
        } catch (e) {
          modelStatus.textContent = `Failed to set model: ${e.message}`;
        }
      });
    } catch (e) {
      modelStatus.textContent = `Model list unavailable: ${e.message}`;
    }
  })();

  // Auto-read replies aloud (local voices)
  const arCheckbox = createEl('input', { type: 'checkbox', id: 'settings-autoread' });
  arCheckbox.checked = autoReadEnabled();
  arCheckbox.addEventListener('change', () => {
    try { localStorage.setItem('autoRead', arCheckbox.checked ? '1' : '0'); } catch {}
    if (!arCheckbox.checked) stopSpeaking();
  });
  c.appendChild(createEl('div', { class: 'modal-row' }, [
    createEl('label', {}, 'Voice'),
    createEl('label', { style: 'display:flex; align-items:center; gap:8px; font-size:13px; cursor:pointer;' }, [
      arCheckbox,
      'Read replies aloud automatically (local Windows voices)',
    ]),
  ]));

  c.appendChild(createEl('div', { class: 'modal-row' }, [
    createEl('label', {}, 'User ID (for memory storage)'),
    createEl('input', {
      type: 'text',
      value: state.user,
      style: 'padding:8px 10px; background: var(--panel-2); border-radius: 6px; width:100%;',
      id: 'settings-user-input',
    }),
  ]));

  c.appendChild(createEl('div', { class: 'modal-row' }, [
    createEl('button', {
      class: 'ghost-btn',
      style: 'border:1px solid var(--border); padding:8px 16px;',
      onclick: () => {
        const v = $('settings-user-input').value.trim() || 'justin';
        state.user = v;
        $('user-input').value = v;
        closeModal();
        loadCoaches();
      },
    }, 'Save and reload'),
  ]));

  c.appendChild(createEl('p', {
    style: 'font-size: 11.5px; color: var(--text-muted); margin-top:16px;',
  }, 'Backend URL and embed config are set via environment variables before starting the app. See README.'));

  $('modal').classList.remove('modal-hidden');
}

function closeModal() {
  $('modal').classList.add('modal-hidden');
}

// ---------- Wiring ----------

function wire() {
  $('coach-search').addEventListener('input', renderCoachList);
  $('user-input').addEventListener('change', (e) => {
    state.user = e.target.value.trim() || 'justin';
    loadCoaches();
  });
  $('btn-memory').addEventListener('click', () => openDrawer('Memory', renderMemoryDrawer));
  $('btn-sources').addEventListener('click', () => openDrawer('Sources', renderSourcesDrawer));
  $('btn-settings').addEventListener('click', openSettings);
  $('btn-theme').addEventListener('click', cycleTheme);
  $('drawer-close').addEventListener('click', closeDrawer);
  $('modal-close').addEventListener('click', closeModal);
  document.querySelector('#modal .modal-backdrop').addEventListener('click', closeModal);
  $('btn-history').addEventListener('click', () => openDrawer('Chat history', renderHistoryDrawer));
  $('btn-new-chat').addEventListener('click', () => {
    if (!state.activeCoach) return;
    stopSpeaking();
    archiveCurrentChat();   // keep it recoverable via History instead of wiping
    setActiveCoach(state.activeCoach.name);
  });
  $('btn-export-chat').addEventListener('click', exportChat);
  setupComposer();

  document.addEventListener('keydown', (e) => {
    const inInput = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || '');

    if (e.key === 'Escape') {
      closeModal();
      closeDrawer();
      return;
    }

    // "/" focuses the coach search (unless you're already typing)
    if (e.key === '/' && !inInput && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      $('coach-search').focus();
      return;
    }

    // Ctrl/Cmd+N: new chat
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
      e.preventDefault();
      if (state.activeCoach) {
        stopSpeaking();
        archiveCurrentChat();
        setActiveCoach(state.activeCoach.name);
      }
      return;
    }

    // Ctrl/Cmd+,: settings
    if ((e.ctrlKey || e.metaKey) && e.key === ',') {
      e.preventDefault();
      openSettings();
      return;
    }

    // Ctrl/Cmd+M: memory drawer
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'm') {
      e.preventDefault();
      openDrawer('Memory', renderMemoryDrawer);
      return;
    }

    // Ctrl/Cmd+T: cycle theme
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 't') {
      e.preventDefault();
      cycleTheme();
      return;
    }
  });
}

// ---------- Init ----------

// Surface a fatal banner if the Python backend process dies — otherwise every
// request just starts failing with no explanation.
if (window.coachApi?.onBackendExit) {
  window.coachApi.onBackendExit((code) => {
    if (!document.getElementById('backend-dead-banner')) {
      const banner = createEl('div', { id: 'backend-dead-banner', class: 'backend-dead-banner' },
        `The local backend stopped (exit code ${code}). Close and relaunch the app.`);
      document.body.prepend(banner);
    }
    $('input').disabled = true;
    $('send-btn').disabled = true;
  });
}

(async function init() {
  wire();
  try { state.config = await api('/api/config'); } catch { state.config = null; }
  initMic();
  await loadCoaches();
})();
