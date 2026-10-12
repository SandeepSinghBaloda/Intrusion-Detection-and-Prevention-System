'use strict';
// Telegram Bot Hub - zero-dependency backend (Node 18+).
// Runs multiple Telegram bots (long polling) and serves the dashboard + REST API.

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || '127.0.0.1';
const DASH_USER = process.env.DASHBOARD_USER || 'admin';
const DASH_PASS = process.env.DASHBOARD_PASSWORD || '';
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const STORE_FILE = path.join(DATA_DIR, 'store.json');
const PUBLIC_DIR = path.join(__dirname, 'public');
const INVITE_LINK = process.env.TELEGRAM_INVITE_LINK || 'https://t.me/+SW4aCwaA0mNiZWZk';

// ---------- persistence ----------
fs.mkdirSync(DATA_DIR, { recursive: true });
let store = { bots: [], rules: [], chats: {}, inviteLink: INVITE_LINK };
try {
  store = { ...store, ...JSON.parse(fs.readFileSync(STORE_FILE, 'utf8')) };
} catch { /* first run */ }
let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.writeFileSync(STORE_FILE + '.tmp', JSON.stringify(store, null, 2), { mode: 0o600 });
    fs.renameSync(STORE_FILE + '.tmp', STORE_FILE);
  }, 100);
}

// ---------- logging + SSE ----------
const logs = [];
const sseClients = new Set();
function log(level, botName, message) {
  const entry = { t: new Date().toISOString(), level, bot: botName || 'system', message };
  logs.push(entry);
  if (logs.length > 500) logs.shift();
  console.log(`[${entry.t}] ${level.toUpperCase()} ${entry.bot}: ${message}`);
  for (const res of sseClients) res.write(`data: ${JSON.stringify(entry)}\n\n`);
}

// ---------- Telegram API ----------
async function tg(token, method, params = {}, signal) {
  const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(params),
    signal,
  });
  const json = await res.json().catch(() => ({}));
  if (!json.ok) {
    const err = new Error(json.description || `HTTP ${res.status}`);
    err.code = json.error_code || res.status;
    throw err;
  }
  return json.result;
}

// ---------- bot runtime ----------
const runtime = new Map(); // botId -> { running, abort, status, error, username }

function publicBot(b) {
  const rt = runtime.get(b.id) || {};
  return {
    id: b.id, name: b.name, username: b.username || null,
    tokenMasked: b.token.slice(0, 4) + '…' + b.token.slice(-4),
    autoStart: b.autoStart !== false,
    status: rt.running ? 'running' : 'stopped',
    error: rt.error || null,
    handled: rt.handled || 0,
  };
}

function matchRule(text, botId) {
  const t = text.toLowerCase();
  for (const r of store.rules) {
    if (r.botId && r.botId !== botId) continue;
    const trig = r.trigger.toLowerCase();
    if (r.match === 'exact' ? t === trig : t.includes(trig)) return r;
  }
  return null;
}

async function handleMessage(bot, msg) {
  const chat = msg.chat;
  store.chats[chat.id] = {
    id: chat.id, type: chat.type,
    title: chat.title || [chat.first_name, chat.last_name].filter(Boolean).join(' ') || chat.username || String(chat.id),
    lastSeen: new Date().toISOString(),
  };
  save();
  const text = (msg.text || '').trim();
  if (!text) return;
  const from = msg.from?.username || msg.from?.first_name || 'user';
  log('info', bot.name, `← ${from} in "${store.chats[chat.id].title}": ${text.slice(0, 120)}`);

  const cmd = text.split(/[\s@]/)[0].toLowerCase();
  let reply = null;
  if (cmd === '/start') reply = `Hello! I'm ${bot.name}. Send /help to see what I can do.`;
  else if (cmd === '/help') {
    reply = 'Commands:\n/start - greet\n/ping - check bot\n/id - show chat id\n/rules - list auto-replies';
  } else if (cmd === '/ping') reply = 'pong 🏓';
  else if (cmd === '/id') reply = `Chat ID: ${chat.id}`;
  else if (cmd === '/rules') {
    const rs = store.rules.filter(r => !r.botId || r.botId === bot.id);
    reply = rs.length ? rs.map(r => `• "${r.trigger}"`).join('\n') : 'No auto-replies configured.';
  } else {
    const rule = matchRule(text, bot.id);
    if (rule) reply = rule.reply;
  }
  if (reply) {
    await tg(bot.token, 'sendMessage', { chat_id: chat.id, text: reply, reply_to_message_id: msg.message_id, allow_sending_without_reply: true });
    log('info', bot.name, `→ replied in "${store.chats[chat.id].title}"`);
  }
}

async function pollLoop(bot) {
  const rt = runtime.get(bot.id);
  let offset = 0;
  let backoff = 1000;
  while (rt.running) {
    try {
      const updates = await tg(bot.token, 'getUpdates', {
        offset, timeout: 25, allowed_updates: ['message', 'channel_post'],
      }, rt.abort.signal);
      backoff = 1000;
      rt.error = null;
      for (const u of updates) {
        offset = u.update_id + 1;
        const msg = u.message || u.channel_post;
        if (!msg) continue;
        rt.handled = (rt.handled || 0) + 1;
        try { await handleMessage(bot, msg); }
        catch (e) { log('error', bot.name, `handler failed: ${e.message}`); }
      }
    } catch (e) {
      if (!rt.running) break;
      rt.error = e.message;
      log('error', bot.name, `poll error: ${e.message}`);
      if (e.code === 401) { rt.running = false; log('error', bot.name, 'invalid token - stopped'); break; }
      await new Promise(r => setTimeout(r, backoff));
      backoff = Math.min(backoff * 2, 30000);
    }
  }
  rt.running = false;
}

async function startBot(bot) {
  const existing = runtime.get(bot.id);
  if (existing?.running) return;
  const me = await tg(bot.token, 'getMe');
  bot.username = me.username;
  await tg(bot.token, 'deleteWebhook').catch(() => {});
  runtime.set(bot.id, { running: true, abort: new AbortController(), handled: existing?.handled || 0 });
  save();
  log('info', bot.name, `started as @${me.username}`);
  pollLoop(bot);
}

function stopBot(bot) {
  const rt = runtime.get(bot.id);
  if (!rt?.running) return;
  rt.running = false;
  rt.abort.abort();
  log('info', bot.name, 'stopped');
}

// ---------- HTTP helpers ----------
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

function send(res, code, body) {
  const data = JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(data);
}
function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', c => { size += c.length; if (size > 1e6) { reject(new Error('Body too large')); req.destroy(); } else chunks.push(c); });
    req.on('end', () => { try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks)) : {}); } catch { reject(new Error('Invalid JSON')); } });
    req.on('error', reject);
  });
}
function authorized(req) {
  if (!DASH_PASS) return true;
  const h = req.headers.authorization || '';
  if (!h.startsWith('Basic ')) return false;
  const [u, ...p] = Buffer.from(h.slice(6), 'base64').toString().split(':');
  const a = Buffer.from(u + ':' + p.join(':')), b = Buffer.from(`${DASH_USER}:${DASH_PASS}`);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
const findBot = id => store.bots.find(b => b.id === id);

// ---------- API ----------
async function api(req, res, url) {
  const parts = url.pathname.split('/').filter(Boolean).slice(1); // drop 'api'
  const m = req.method;
  const [a, id, action] = parts;

  if (a === 'events' && m === 'GET') {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
    res.write(': connected\n\n');
    sseClients.add(res);
    req.on('close', () => sseClients.delete(res));
    return;
  }
  if (a === 'state' && m === 'GET') {
    return send(res, 200, {
      inviteLink: store.inviteLink,
      bots: store.bots.map(publicBot),
      rules: store.rules,
      chats: Object.values(store.chats),
      logs: logs.slice(-100),
    });
  }
  if (a === 'config' && m === 'POST') {
    const { inviteLink } = await readJson(req);
    store.inviteLink = String(inviteLink || '').slice(0, 300);
    save();
    return send(res, 200, { ok: true });
  }
  if (a === 'bots' && !id && m === 'POST') {
    const { name, token } = await readJson(req);
    if (!token || !/^\d+:[\w-]{30,}$/.test(token.trim())) return send(res, 400, { error: 'Invalid bot token format (expected 123456:ABC...)' });
    if (store.bots.some(b => b.token === token.trim())) return send(res, 409, { error: 'This bot token is already added' });
    let me;
    try { me = await tg(token.trim(), 'getMe'); }
    catch (e) { return send(res, 400, { error: `Telegram rejected the token: ${e.message}` }); }
    const bot = { id: crypto.randomUUID(), name: (name || me.first_name || me.username).slice(0, 60), token: token.trim(), username: me.username, autoStart: true };
    store.bots.push(bot);
    save();
    log('info', bot.name, `added @${me.username}`);
    await startBot(bot).catch(e => log('error', bot.name, e.message));
    return send(res, 201, publicBot(bot));
  }
  if (a === 'bots' && id) {
    const bot = findBot(id);
    if (!bot) return send(res, 404, { error: 'Bot not found' });
    if (m === 'DELETE') {
      stopBot(bot);
      store.bots = store.bots.filter(b => b.id !== id);
      store.rules = store.rules.filter(r => r.botId !== id);
      runtime.delete(id);
      save();
      log('info', bot.name, 'removed');
      return send(res, 200, { ok: true });
    }
    if (m === 'POST' && action === 'start') {
      bot.autoStart = true; save();
      try { await startBot(bot); } catch (e) { return send(res, 400, { error: e.message }); }
      return send(res, 200, publicBot(bot));
    }
    if (m === 'POST' && action === 'stop') {
      bot.autoStart = false; save();
      stopBot(bot);
      return send(res, 200, publicBot(bot));
    }
    if (m === 'POST' && action === 'send') {
      const { chatId, text } = await readJson(req);
      if (!chatId || !text) return send(res, 400, { error: 'chatId and text are required' });
      try { await tg(bot.token, 'sendMessage', { chat_id: chatId, text: String(text).slice(0, 4096) }); }
      catch (e) { return send(res, 502, { error: e.message }); }
      log('info', bot.name, `→ sent message to ${chatId}`);
      return send(res, 200, { ok: true });
    }
  }
  if (a === 'broadcast' && m === 'POST') {
    const { text, botId } = await readJson(req);
    if (!text) return send(res, 400, { error: 'text is required' });
    const bots = botId ? [findBot(botId)].filter(Boolean) : store.bots;
    const results = [];
    for (const bot of bots) {
      for (const chat of Object.values(store.chats)) {
        try { await tg(bot.token, 'sendMessage', { chat_id: chat.id, text: String(text).slice(0, 4096) }); results.push({ bot: bot.name, chat: chat.title, ok: true }); }
        catch (e) { results.push({ bot: bot.name, chat: chat.title, ok: false, error: e.message }); }
      }
    }
    log('info', 'system', `broadcast: ${results.filter(r => r.ok).length}/${results.length} delivered`);
    return send(res, 200, { results });
  }
  if (a === 'rules' && !id && m === 'POST') {
    const { trigger, reply, match, botId } = await readJson(req);
    if (!trigger?.trim() || !reply?.trim()) return send(res, 400, { error: 'trigger and reply are required' });
    const rule = { id: crypto.randomUUID(), trigger: trigger.trim().slice(0, 200), reply: reply.trim().slice(0, 4096), match: match === 'exact' ? 'exact' : 'contains', botId: botId || null };
    store.rules.push(rule); save();
    return send(res, 201, rule);
  }
  if (a === 'rules' && id && m === 'DELETE') {
    store.rules = store.rules.filter(r => r.id !== id); save();
    return send(res, 200, { ok: true });
  }
  if (a === 'chats' && id && m === 'DELETE') {
    delete store.chats[id]; save();
    return send(res, 200, { ok: true });
  }
  send(res, 404, { error: 'Not found' });
}

// ---------- server ----------
const server = http.createServer(async (req, res) => {
  if (!authorized(req)) {
    res.writeHead(401, { 'www-authenticate': 'Basic realm="Telegram Bot Hub"' });
    return res.end('Authentication required');
  }
  const url = new URL(req.url, 'http://localhost');
  try {
    if (url.pathname.startsWith('/api/')) return await api(req, res, url);
    const rel = url.pathname === '/' ? 'index.html' : path.normalize(url.pathname).replace(/^(\.\.[/\\])+/, '');
    const file = path.join(PUBLIC_DIR, rel);
    if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end('Not found');
    }
    res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  } catch (e) {
    log('error', 'api', e.message);
    if (!res.headersSent) send(res, 500, { error: e.message });
  }
});

server.listen(PORT, HOST, () => {
  log('info', 'system', `Dashboard on http://${HOST}:${PORT}${DASH_PASS ? ' (password protected)' : ' (no password set)'}`);
  for (const bot of store.bots) {
    if (bot.autoStart !== false) startBot(bot).catch(e => log('error', bot.name, `start failed: ${e.message}`));
  }
});

function shutdown() {
  for (const bot of store.bots) stopBot(bot);
  clearTimeout(saveTimer);
  fs.writeFileSync(STORE_FILE, JSON.stringify(store, null, 2), { mode: 0o600 });
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
