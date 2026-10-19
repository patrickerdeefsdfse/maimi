'use strict';
const fs = require('node:fs');
const path = require('node:path');

function validateDeployment(value) {
  const endpoints = {
    'local-development': 'http://127.0.0.1:28090',
    'company': 'https://platform-ai.megmeet.com',
  };
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !Object.hasOwn(endpoints, value.mode) || value.gatewayOrigin !== endpoints[value.mode]
      || Object.keys(value).some(key => !['mode', 'gatewayOrigin'].includes(key))) {
    throw Error('桌面服务地址配置无效。');
  }
  const development = value.mode === 'local-development';
  return Object.freeze({
    GATEWAY: value.gatewayOrigin, DEVELOPMENT: development,
    MODE: development ? 'synthetic-local-development' : 'company',
    APP_NAME: development ? 'MEGMEET AI Local' : 'MEGMEET AI',
  });
}

module.exports = { ...validateDeployment(JSON.parse(fs.readFileSync(path.join(__dirname, 'deployment.json'), 'utf8'))),
  validateDeployment };
