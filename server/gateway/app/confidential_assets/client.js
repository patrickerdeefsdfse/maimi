(() => {
  'use strict';
  const select = document.querySelector('#skill');
  const status = document.querySelector('#status');
  const login = document.querySelector('#login');
  const retry = document.querySelector('#reload');
  const labels = new Map(Object.entries({
    'meeting-documents':'会议文档','drawing-cost-analysis':'图纸成本分析','server-operations':'运维服务',
    'infrastructure-monitor':'基础设施监控','sales-orders':'销售订单录入','sales-order-approval':'销售订单审核',
    'patent-review':'专利审核','onboarding-health-review':'入职体检材料核对','company-ppt-template':'公司演示模板'
  }));
  const requested = location.hash.slice(1);
  if (location.hash) history.replaceState(null, '', location.pathname);

  async function load() {
    select.replaceChildren();
    select.disabled = true;
    login.hidden = retry.hidden = true;
    status.textContent = '正在获取已授权技能…';
    try {
      const response = await fetch('/v1/plugins/confidential-skills?client=web', {
        credentials:'same-origin', mode:'same-origin', redirect:'error', cache:'no-store', signal:AbortSignal.timeout(15000)
      });
      if (response.status === 401) {
        login.hidden = false;
        status.textContent = '请登录后查看已授权技能。';
        return;
      }
      if (!response.ok) throw Error();
      const {skills} = await response.json();
      if (!Array.isArray(skills) || skills.some(key => typeof key !== 'string' || !/^[\w-]{1,180}$/.test(key))) throw Error();
      for (const key of new Set(skills)) select.add(new Option(labels.get(key) || key, key));
      if (skills.includes(requested)) select.value = requested;
      select.disabled = !skills.length;
      status.textContent = skills.length ? '离线执行环境尚未启用，暂不能提交任务或附件。' : '当前账号没有已审批的保密技能。';
    } catch {
      status.textContent = '暂时无法获取授权清单，请重试。';
      retry.hidden = false;
    }
  }
  retry.addEventListener('click', load);
  load();
})();
