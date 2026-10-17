'use strict';

const CHAT_MODEL = 'deepseek-v4.1-flash';
const CHAT_PROVIDER = 'deepseek-official';
const MAX_CHAT_BYTES = 16 * 1024 * 1024;
const fail = (code, status) => Object.assign(Error(code), {code, status});

function validateChatBody(bytes, authorized) {
  if (!Buffer.isBuffer(bytes) || bytes.length > MAX_CHAT_BYTES) throw fail('CHAT_REQUEST_TOO_LARGE', 413);
  let value;
  try { value = JSON.parse(bytes.toString('utf8')); } catch { throw fail('CHAT_REQUEST_INVALID', 400); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw fail('CHAT_REQUEST_INVALID', 400);
  if (value.model !== CHAT_MODEL) throw fail('CHAT_MODEL_NOT_ALLOWED', 403);
  if (!authorized) throw fail('CHAT_MODEL_NOT_AUTHORIZED', 403);
  return value;
}

function readChatBody(req, authorized) {
  return new Promise((resolve, reject) => {
    let length = 0, done = false; const chunks = [];
    const finish = (error, bytes) => {
      if (done) return; done = true; clearTimeout(timer);
      req.removeListener('data', onData); req.removeListener('end', onEnd);
      req.removeListener('error', onError); req.removeListener('aborted', onAbort);
      if (error) {
        // IncomingMessage may emit ECONNRESET after its aborted event. The
        // request has already failed; keep that transport error contained.
        req.once('error', () => {}); req.resume(); reject(error);
      } else resolve(bytes);
    };
    const onData = chunk => { length += chunk.length; if (length > MAX_CHAT_BYTES) finish(fail('CHAT_REQUEST_TOO_LARGE', 413)); else chunks.push(chunk); };
    const onEnd = () => { try { const bytes = Buffer.concat(chunks); validateChatBody(bytes, authorized()); finish(null, bytes); } catch (error) { finish(error); } };
    const onError = () => finish(fail('CHAT_REQUEST_INCOMPLETE', 400));
    const onAbort = () => finish(fail('CHAT_REQUEST_INCOMPLETE', 400));
    const timer = setTimeout(() => { finish(fail('CHAT_REQUEST_TIMEOUT', 408)); req.destroy(); }, 15000);
    req.on('data', onData); req.once('end', onEnd); req.once('error', onError); req.once('aborted', onAbort);
  });
}

function modelCatalog(value) {

  if (!value || !Array.isArray(value.data) || value.data.length > 1000)

    throw Object.assign(Error('当前账号没有可用模型，请联系管理员。'), { code: 'NO_PERSONAL_MODELS' });

  if (!value.data.length) return { models: [], defaultModel: null, preferredAvailable: false,

    notice: '当前账号暂无授权模型，请联系管理员。' };

  const models = value.data.map(row => {

    if (!row || typeof row.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(row.id))

      throw Error('账号模型目录无效。');

    const name = typeof row.name === 'string' && row.name.trim() && row.name.length <= 200 ? row.name : row.id;

    const modalities=row.inputModalities;

    if(modalities!==undefined && (!Array.isArray(modalities)||!modalities.includes('text')||modalities.some(x=>x!=='text'&&x!=='image')||new Set(modalities).size!==modalities.length))throw Error('账号模型图片能力声明无效。');

    return { id: row.id, name, ...(modalities?{inputModalities:['text','image'].filter(x=>modalities.includes(x))}:{}) };

  });

  const ids = new Set(models.map(row => row.id));

  if (ids.size !== models.length) throw Error('账号模型目录重复。');

  if (value.default_model != null && !ids.has(value.default_model)) throw Error('默认模型不在当前账号授权范围。');

  const authorized = models.filter(row => row.id === CHAT_MODEL);
  return { models: authorized, defaultModel: authorized.length ? CHAT_MODEL : null,
    preferredAvailable: authorized.length === 1,
    notice: authorized.length ? null : '当前账号暂无指定企业模型的授权，请联系管理员。' };

}

module.exports = {modelCatalog, CHAT_MODEL, CHAT_PROVIDER, MAX_CHAT_BYTES, validateChatBody, readChatBody};
