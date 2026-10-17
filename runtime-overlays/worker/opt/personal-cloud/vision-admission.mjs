// Image capability is verified for the actual session selection, never guessed
// from a model's display name and never inherited from another employee.
const MESSAGES = Object.freeze({
  personal_model_image_not_supported: '公司接口标明当前模型不支持图片，请选择支持识图的模型。',
  personal_model_vision_unverified: '当前模型未通过图片识别验证，请换用支持识图的模型后重试。',
  personal_model_vision_busy: '正在验证该模型的图片能力，请稍后重试。',
  personal_model_upstream_unavailable: '图片模型接口暂时不可用，请稍后重试。',
  personal_model_vision_check_unavailable: '图片能力验证暂时不可用，请稍后重试。',
  personal_model_not_authorized: '当前账号未获准使用此模型，请刷新模型列表。',
  personal_key_reauthorization_required: '模型授权已失效，请重新登录。',
});
export function visionError(code) {
  return Object.assign(new Error(MESSAGES[code] || '暂时无法确认模型的图片能力，请稍后重试。'),
    {nativeCode: 'session/image-capability-failed', reason: MESSAGES[code] ? code : 'VISION_UNAVAILABLE'});
}
export function hasImage(request) {
  return Array.isArray(request?.content) && request.content.some(part => part?.type === 'image');
}
export function createVisionAdmission({selection, negotiate, applyCatalog, validate}) {
  return async (task, request, signal) => {
    if (!hasImage(request)) return;
    const images = request.content.filter(part => part?.type === 'image');
    if (images.length > 8) throw Object.assign(new Error('每条消息最多发送8张图片。'),
      {nativeCode: 'session/image-capability-failed', reason: 'IMAGE_LIMIT'});
    signal?.throwIfAborted();
    const first = await selection(request.sessionId, signal);
    if (first?.protocol !== 1 || first.provider !== 'deepseek-official' || typeof first.model !== 'string')
      throw visionError('VISION_SELECTION_UNAVAILABLE');
    await validate(task);
    const catalog = await negotiate(task, first.model, signal);
    signal?.throwIfAborted();
    if (task.revoked) throw visionError('personal_key_reauthorization_required');
    await validate(task);
    const admitted = catalog?.data?.find(model => model.id === first.model);
    if (!admitted?.inputModalities?.includes('image')) throw visionError('personal_model_vision_unverified');
    await applyCatalog(catalog);
    const latest = await selection(request.sessionId, signal);
    signal?.throwIfAborted();
    if (latest?.provider !== first.provider || latest?.model !== first.model)
      throw Object.assign(new Error('图片准备期间模型已切换，请重新发送。'),
        {nativeCode: 'session/image-capability-failed', reason: 'MODEL_SELECTION_CHANGED'});
  };
}
