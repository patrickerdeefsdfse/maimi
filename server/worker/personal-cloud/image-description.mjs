// The attachment and intent must belong to this admitted native prompt. A prior
// turn's image, a filename or words inside an attachment do not set its mode.
const imageNoun = '(?:(?:这|那|上面|以下|所附|上传|这个)?(?:一?[张幅个]|份)?(?:图片|图像|照片|截图|画面|图)|它)';
const subject = '(?:的?(?:里面|里|上|中|内)?(?:的)?(?:内容|信息|文字|画面|东西|情况)?)';
const chinese = [
  new RegExp('^(?:你)?(?:能不能|能否|可以|能|看得见|看得到)?(?:读懂|读到|读|看懂|看到|看见|看清|看|识别|辨认)' + imageNoun + subject + '(?:吗|么|嘛)?$'),
  new RegExp('^(?:能读的话|可以的话|如果可以|如果能读|若可以)?(?:请|麻烦|帮我|给我)?(?:用)?(?:简单|简要|简短|详细|大致|简洁)?(?:用)?(?:文字|中文|一句话|几句话)?(?:来)?(?:描述|介绍|概括|总结|说明|说说|讲讲|看看|看|解读|识别|读出)(?:一下|下)?' + imageNoun + subject + '(?:吗|吧)?$'),
  new RegExp('^' + imageNoun + '(?:里|里面|中|上|上面|内|的内容)?(?:有|写了|说了|显示了|画了|是|有什么|讲了|是什么)?(?:些)?(?:什么|啥)?(?:内容|信息|文字|东西|意思)?$'),
  /^(?:这是(?:什么|啥)|(?:请)?(?:简单|简要)?(?:用文字)?描述(?:一下|下)?)$/,
];
const english = /^(?:what do you see|(?:(?:please|can you|could you) )?(?:(?:briefly|simply) )?(?:describe|read|explain|summari[sz]e|tell me (?:what is|what's)|what is|what's)(?: (?:shown|visible|written))?(?: (?:is|in|on))? (?:this|the|these|attached)(?: attached)? (?:image|picture|photo|screenshot)s?(?: (?:in (?:plain )?(?:text|words)|briefly|please))?)$/i;

export function imagePromptMode(request, stagedImages, selectedIds) {
  if (selectedIds.length || !Array.isArray(request?.content)) return null;
  const receipts = request.content.filter(part => part.type === 'file');
  if (receipts.length !== stagedImages || !stagedImages && !request.content.some(part => part.type === 'image')) return null;
  const prompt = request.content.filter(part => part.type === 'text').map(part => part.text).join('\n').trim();
  const clauses = prompt.toLowerCase().split(/[。！？；，.!?;,\n]+/u).map(part => part.trim()).filter(Boolean);
  // Only lock a direct description if every clause asks for observation.
  // Mixed/unknown requests retain the agent's usual tools, including export,
  // editing, external lookup, native commands and explicitly selected skills.
  if (prompt.length <= 256 && clauses.length && clauses.every(clause =>
    english.test(clause) || chinese.some(pattern => pattern.test(clause.replace(/\s+/gu, ''))))) return 'describe';
  return 'attached';
}

const guidance = 'The attached image is already present as input. Answer questions about its visible contents directly from that image. Use workspace commands, file tools or enterprise connectors only when the user asks for work that needs them. If details cannot be read, explain that limitation.';

export function prepareImageModelRequest(body, mode, scope) {
  if (!mode || scope.purpose !== 'conversation' || scope.sessionId !== scope.rootSessionId || !Array.isArray(body.messages)) return body;
  // DSH appends several text-only context messages as role=user after the
  // actual prompt. The task mode above belongs to this specific generation.
  if (!body.messages.some(message => message.role === 'user' && Array.isArray(message.content)
    && message.content.some(part => part.type === 'image_url' || part.type === 'input_image'))) return body;
  const messages = [...body.messages];
  const system = messages.findIndex(message => message.role === 'system');
  if (system >= 0 && typeof messages[system].content === 'string') {
    messages[system] = {...messages[system], content:messages[system].content + '\n\n' + guidance};
  } else {
    messages.unshift({role:'system',content:guidance});
  }
  const request = {...body, messages};
  if (mode === 'describe' && [undefined,'auto','none'].includes(request.tool_choice)) {
    delete request.tools;
    delete request.tool_choice;
    delete request.parallel_tool_calls;
  }
  return request;
}
