'use strict';
// User-account (MTProto) helper: logs in with api_id/api_hash, joins the group via invite link,
// and adds the configured bots to that group. Uses GramJS ("telegram" package).

const API_ID_ENV = process.env.TELEGRAM_API_ID;
const API_HASH_ENV = process.env.TELEGRAM_API_HASH;

function createAccount({ store, save, log, getBots, registerChat }) {
  const acc = () => (store.account ||= {});
  let client = null;
  // idle | connecting | awaiting_code | awaiting_password | ready | error
  let state = 'idle';
  let lastError = null;
  let me = null;
  let pending = { code: null, password: null };

  const creds = () => ({
    apiId: Number(API_ID_ENV || acc().apiId || 0),
    apiHash: API_HASH_ENV || acc().apiHash || '',
  });

  const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
  const lib = () => ({ ...require('telegram'), ...require('telegram/sessions') });

  async function connect(phone) {
    const { apiId, apiHash } = creds();
    if (!apiId || !apiHash) throw new Error('Set API ID and API hash first');
    const { TelegramClient, StringSession } = lib();
    if (client) await client.disconnect().catch(() => {});
    client = new TelegramClient(new StringSession(acc().session || ''), apiId, apiHash, { connectionRetries: 3 });
    client.setLogLevel?.('none');
    state = 'connecting'; lastError = null;
    const hasSession = !!acc().session;

    const run = client.start({
      phoneNumber: async () => phone,
      phoneCode: async () => { pending.code = deferred(); state = 'awaiting_code'; return pending.code.promise; },
      password: async () => { pending.password = deferred(); state = 'awaiting_password'; return pending.password.promise; },
      onError: err => { lastError = err.message; log('error', 'account', err.message); return false; }, // false = let user retry
    }).then(async () => {
      acc().session = client.session.save();
      if (phone) acc().phone = phone;
      save();
      me = await client.getMe();
      state = 'ready';
      log('info', 'account', `logged in as ${me.username ? '@' + me.username : me.firstName}`);
    }).catch(e => { state = 'error'; lastError = e.message; log('error', 'account', `login failed: ${e.message}`); });

    // A stored session resolves quickly; a fresh login waits for the user, so don't block the request.
    if (hasSession) await run;
  }

  async function resume() {
    if (acc().session && creds().apiId) {
      try { await connect(acc().phone); } catch (e) { state = 'error'; lastError = e.message; }
    }
  }

  const need = () => { if (state !== 'ready') throw new Error('Account is not logged in'); };

  function inviteHash(link) {
    const m = /(?:t\.me|telegram\.me)\/(?:joinchat\/|\+)([\w-]+)/i.exec(link || '');
    if (!m) throw new Error('Invite link must look like https://t.me/+XXXX');
    return m[1];
  }

  async function resolveGroup(link, join) {
    const { Api } = lib();
    const hash = inviteHash(link);
    const info = await client.invoke(new Api.messages.CheckChatInvite({ hash }));
    if (info.className === 'ChatInviteAlready' || info.className === 'ChatInvitePeek') return info.chat;
    if (!join) throw new Error('Account has not joined this group yet');
    const res = await client.invoke(new Api.messages.ImportChatInvite({ hash }));
    const chat = (res.chats || [])[0];
    if (!chat) throw new Error('Joined, but Telegram returned no chat');
    return chat;
  }

  const botApiId = chat => (chat.className === 'Channel' ? `-100${chat.id}` : `-${chat.id}`);

  function remember(chat) {
    acc().group = { id: botApiId(chat), title: chat.title };
    registerChat({ id: botApiId(chat), type: chat.className === 'Channel' ? (chat.megagroup ? 'supergroup' : 'channel') : 'group', title: chat.title });
    save();
  }

  return {
    resume,
    status: () => ({
      hasCreds: !!(creds().apiId && creds().apiHash),
      credsFromEnv: !!(API_ID_ENV && API_HASH_ENV),
      apiId: creds().apiId || null,
      state, error: lastError, phone: acc().phone || null,
      user: me ? (me.username ? '@' + me.username : me.firstName) : null,
      group: acc().group || null,
    }),
    setCreds(apiId, apiHash) {
      if (!/^\d+$/.test(String(apiId)) || !/^[0-9a-f]{32}$/i.test(String(apiHash))) throw new Error('API ID must be digits and API hash a 32-char hex string');
      acc().apiId = Number(apiId); acc().apiHash = String(apiHash);
      delete acc().session; state = 'idle'; me = null; save();
    },
    async startLogin(phone) {
      if (!/^\+\d{7,15}$/.test(phone || '')) throw new Error('Phone must be in international format, e.g. +919876543210');
      delete acc().session;
      await connect(phone);
    },
    submitCode(code) {
      if (state !== 'awaiting_code' || !pending.code) throw new Error('Not waiting for a code');
      lastError = null; state = 'connecting'; pending.code.resolve(String(code).trim());
    },
    submitPassword(pw) {
      if (state !== 'awaiting_password' || !pending.password) throw new Error('Not waiting for a password');
      lastError = null; state = 'connecting'; pending.password.resolve(String(pw));
    },
    async logout() {
      if (client) { await client.invoke(new (lib().Api.auth.LogOut)()).catch(() => {}); await client.disconnect().catch(() => {}); }
      client = null; me = null; state = 'idle'; delete acc().session; delete acc().group; save();
    },
    async joinGroup(link) {
      need();
      const chat = await resolveGroup(link, true);
      remember(chat);
      log('info', 'account', `joined "${chat.title}"`);
      return acc().group;
    },
    // Adds every configured bot (by @username) to the group. Requires "add members" admin right.
    async addBots(link) {
      need();
      const { Api } = lib();
      const chat = await resolveGroup(link, true);
      remember(chat);
      const results = [];
      for (const bot of getBots()) {
        if (!bot.username) { results.push({ bot: bot.name, ok: false, error: 'bot has no username' }); continue; }
        try {
          const user = await client.getInputEntity(bot.username);
          if (chat.className === 'Channel') {
            await client.invoke(new Api.channels.InviteToChannel({ channel: chat, users: [user] }));
          } else {
            await client.invoke(new Api.messages.AddChatUser({ chatId: chat.id, userId: user, fwdLimit: 0 }));
          }
          results.push({ bot: bot.name, ok: true });
        } catch (e) {
          const msg = /USER_ALREADY_PARTICIPANT/.test(e.message) ? 'already in group' : e.message;
          results.push({ bot: bot.name, ok: /already/.test(msg), error: msg });
        }
      }
      log('info', 'account', `add bots: ${results.filter(r => r.ok).length}/${results.length} ok`);
      return results;
    },
  };
}

module.exports = { createAccount };
