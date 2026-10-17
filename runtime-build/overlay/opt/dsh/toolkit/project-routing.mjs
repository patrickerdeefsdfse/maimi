import { createHash, randomUUID } from 'node:crypto';
import { emptyDrawingFields } from './drawing-extraction.mjs';

export const FORCED_PROJECTS = Object.freeze(['meeting', 'employee-cards', 'heatcost', 'infrastructure-monitor']);
const NAMES = {meeting:'会议文档', 'employee-cards':'工卡制作', heatcost:'图纸成本分析', 'infrastructure-monitor':'vmware'};
const SID = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const OPAQUE = /^pjf_[A-Za-z0-9_-]{32}$/;
const fail = code => { throw Object.assign(Error(code), {code}); };
const safe = text => String(text || '').replace(/[\x00-\x1f<>]/g, ' ').slice(0, 2000);
const textOf = content => typeof content === 'string' ? content : Array.isArray(content) ? content.filter(p=>p?.type==='text').map(p=>p.text).join('\n') : '';
function titleCall(body) {
  return !body.tools?.length && body.messages?.length === 2 && body.messages[0].role === 'system'
    && textOf(body.messages[0].content).startsWith('Create a concise title for an AI coding-assistant session from the supplied human messages.\n')
    && body.messages[1].role === 'user' && textOf(body.messages[1].content).startsWith('Generate the session title from this JSON array of human messages:\n');
}
function idempotency(request, connector, index) {
  const hex = createHash('sha256').update(request + '\0' + connector + '\0' + index).digest('hex');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-4${hex.slice(13,16)}-8${hex.slice(17,20)}-${hex.slice(20,32)}`;
}

// Selection is captured at submission. Only an accepted user/message with the
// same native request ID can activate it. No LLM tool choice, filename inference,
// transcript instruction, staged upload, or historical attachment triggers jobs.
export function createProjectRouter({snapshot, current, currentStep, execute, results, extractDrawing, now=Date.now}) {
  const admitted = new Map();
  const key = (task, id) => `${task.lease}:${task.sessionId}:${task.generation}:${id}`;
  function register(task, request) {
    if (!SID.test(task.sessionId || '') || request.sessionId !== task.sessionId || !UUID.test(request.requestId || '')) fail('PROJECT_ROUTE_REQUEST_INVALID');
    const selection = snapshot(task.sessionId, {clientKey:task.lease});
    const ids = selection.ids.filter(id=>FORCED_PROJECTS.includes(id));
    if (!ids.length) return;
    for (const [k, row] of admitted) if (row.created + 3600000 < now() && !row.running) admitted.delete(k);
    const k = key(task, request.requestId);
    if (admitted.has(k)) return;
    if (admitted.size >= 512) fail('PROJECT_ROUTE_QUEUE_FULL');
    admitted.set(k, {task, requestId:request.requestId, ids, revision:selection.revision, created:now(), controller:new AbortController(), running:false, promise:null});
  }
  function has(task) { return [...admitted.values()].some(row=>row.task===task); }
  function check(row) {
    row.controller.signal.throwIfAborted();
    const live = snapshot(row.task.sessionId, {clientKey:row.task.lease});
    if (current(row.task.sessionId) !== row.task || row.task.revoked || live.revision !== row.revision || row.ids.some(id=>!live.ids.includes(id))) fail('PROJECT_ROUTE_SELECTION_CHANGED');
  }
  async function invoke(row, connector, action, args, index) {
    check(row);
    const value = await execute({operation:'project-invoke', session_id:row.task.sessionId, connector, action, arguments:args,
      request_id:idempotency(row.requestId, connector, index)}, {clientKey:row.task.lease, signal:row.controller.signal});
    check(row); return value;
  }
  function summary(connector, value) {
    const card = value?.project_result;
    if (!card || card.connector !== connector || !['completed','needs_input','failed'].includes(card.status)) fail('PROJECT_ROUTE_RESULT_INVALID');
    if (!UUID.test(value.job_id || '')) fail('PROJECT_ROUTE_JOB_ID_REQUIRED');
    const state=card.status==='completed'?'已完成':card.status==='needs_input'?'请补充必要信息':'本次未完成';
    return `${NAMES[connector]}${state}。\n\n<!-- maimi-project-v1:${value.job_id.toLowerCase()} -->`;
  }
  async function run(row, attachments, body) {
    check(row); row.running = true;
    try {
      const replies = []; let visionUsed=false, usageKnown=true;
      const usage={prompt_tokens:0,completion_tokens:0,total_tokens:0};
      if (!Array.isArray(attachments) || attachments.length > 1024 || attachments.some(a=>!OPAQUE.test(a.attachment_id || '') || typeof a.name !== 'string')) fail('PROJECT_ROUTE_ATTACHMENTS_INVALID');
      for (const connector of row.ids) {
        check(row);
        try {
          if (connector === 'infrastructure-monitor') {
            replies.push(summary(connector, await invoke(row, connector, 'status', {}, 0))); continue;
          }
          const matches = attachments.filter(a=>(connector === 'meeting' ? /\.(xlsx|xlsm)$/i : connector === 'employee-cards' ? /\.xlsx$/i : /\.(pdf|png|jpe?g|webp|bmp|tiff?)$/i).test(a.name));
          if (matches.length > 4) { replies.push(`${NAMES[connector]}：本次文件较多，请每次上传不超过 4 份对应文件后发送。`); continue; }
          if (!matches.length) {
            if (connector === 'meeting') replies.push(summary(connector, await invoke(row, connector, 'requirements', {}, 0)));
            else if (connector === 'employee-cards') {
              const previous = await results(row.task.sessionId, {clientKey:row.task.lease, signal:row.controller.signal}); check(row);
              const card = previous.results?.findLast(r=>r.connector===connector && r.actions?.some(a=>a.action==='generate'));
              replies.push(summary(connector, await invoke(row, connector, card?'review':'template', card?{job_id:card.id}:{}, 0)));
            } else replies.push(summary(connector, await invoke(row, connector, 'review', {}, 0)));
            if (attachments.length) replies.push(`${NAMES[connector]}：本次附件格式不符合该项目要求，请按结果卡片说明重新上传。`);
            continue;
          }
          for (let i=0; i<matches.length; i++) {
            const attachment = matches[i]; let value;
            if (connector !== 'heatcost') value = await invoke(row, connector, connector==='meeting'?'generate_from_workbook':'import_workbook', {attachment_id:attachment.attachment_id}, i);
            else {
              const prepared = await invoke(row, connector, 'prepare_drawing', {attachment_id:attachment.attachment_id}, i*2);
              check(row);
              const priorUsageKnown=usageKnown;visionUsed=true;usageKnown=false;
              let extracted;
              try {
                extracted = await extractDrawing(prepared, {body, task:row.task, signal:row.controller.signal});
              } catch(error) {
                check(row);
                if(!['PROJECT_VISION_REQUIRED','PROJECT_DRAWING_EXTRACTION_INVALID','PROJECT_DRAWING_EXTRACTION_FAILED','PROJECT_DRAWING_EXTRACTION_TIMEOUT'].includes(error?.code))throw error;
                // A failed recognition is not a successful quote. Persist only
                // empty observations so the user gets a review form and CSV.
                extracted={fields:emptyDrawingFields(),processes:{},usage:null,warnings:['recognition_failed']};
              }
              if(extracted.usage){usageKnown=priorUsageKnown;for(const key of Object.keys(usage))usage[key]+=extracted.usage[key];}
              check(row);
              // The extraction supplies observations only; no model-created
              // rates, file references, URLs or project actions are accepted.
              value = await invoke(row, connector, 'calculate', {attachment_id:attachment.attachment_id,
                fields:extracted.fields, processes:extracted.processes}, i*2+1);
              if(extracted.warnings?.includes('recognition_failed'))replies.push('图纸自动识别未完成，未填入估计数值；已生成待核对表和明细文件，请在结果卡片补充字段。');
              else if(extracted.warnings?.length)replies.push('图纸部分字段尚未确认，已保留有依据的识别值；请在结果卡片核对缺失字段，当前结果不是正式报价。');
              if(prepared.drawing?.text_warning)replies.push('辅助文字提取暂时不可用，本次保留图像识别与原有文字层；请核对图纸标注。');
              if(prepared.drawing?.text_pages?.some(p=>p.truncated))replies.push('部分页面辅助文字较长，已限制识别文本长度；请核对原图中的完整标注。');
              if(prepared.drawing?.truncated) replies.push('该图纸超过 3 页，本次只分析前 3 页；其余页面请分批上传。');
            }
            replies.push(summary(connector, value));
          }
        } catch (error) {
          check(row);
          const messages = {PROJECT_VISION_REQUIRED:'当前模型无法识别图纸，请切换支持图片的模型后重试。', PROJECT_DRAWING_EXTRACTION_INVALID:'图纸字段识别结果不完整，请重试或换一份清晰的图纸。'};
          replies.push(`${NAMES[connector]}：${messages[error.code] || '本次执行未完成，请检查附件后重试。若下方结果卡片有提示，请按提示修正。'}`);
        }
      }
      check(row); return {text:replies.join('\n\n'),visionUsed,usage:usageKnown?usage:null};
    } finally { row.running = false; }
  }
  async function respond(task, body) {
    if (!has(task)) return null;
    if (titleCall(body)) return [...new Set([...admitted.values()].filter(r=>r.task===task).flatMap(r=>r.ids))].map(id=>NAMES[id]).join('、');
    const projection = await currentStep(task.sessionId, {clientKey:task.lease});
    if (!Array.isArray(projection.messages) || projection.messages.length > 32) fail('PROJECT_ROUTE_STEP_INVALID');
    const routed = projection.messages.map(message=>({message,row:admitted.get(key(task,message.request_id))})).filter(x=>x.row);
    if (!routed.length) return null;
    const replies = [];
    for (const {row,message} of routed) {
      check(row);
      // Concurrent native retries share exactly the same real project run.
      row.promise ||= run(row,message.attachments,body);
      replies.push(await row.promise);
    }
    const text=replies.map(reply=>reply.text).join('\n\n');
    if(!replies.some(reply=>reply.visionUsed))return text;
    const usage=replies.every(reply=>reply.usage)?{prompt_tokens:0,completion_tokens:0,total_tokens:0}:null;
    if(usage)for(const reply of replies)for(const key of Object.keys(usage))usage[key]+=reply.usage[key];
    return {text,usage};
  }
  function revoke(task) { for (const row of admitted.values()) if (row.task===task) row.controller.abort(); }
  return {register,has,respond,revoke};
}

export function writeProjectCompletion(res, body, text, measured={prompt_tokens:0,completion_tokens:0,total_tokens:0}) {
  const common = {id:'project-'+randomUUID(), created:Math.floor(Date.now()/1000), model:String(body.model || 'project-workflow')};
  const usage = measured;
  if (body.stream) {
    res.writeHead(200, {'Content-Type':'text/event-stream', 'Cache-Control':'no-store'});
    for (const [delta,finish] of [[{role:'assistant',content:text},null],[{},'stop']])
      res.write('data: '+JSON.stringify({...common,object:'chat.completion.chunk',choices:[{index:0,delta,finish_reason:finish}],...(finish&&usage?{usage}:{})})+'\n\n');
    res.end('data: [DONE]\n\n');
  } else {
    res.writeHead(200, {'Content-Type':'application/json', 'Cache-Control':'no-store'});
    res.end(JSON.stringify({...common,object:'chat.completion',choices:[{index:0,message:{role:'assistant',content:text},finish_reason:'stop'}],...(usage?{usage}:{})}));
  }
}
