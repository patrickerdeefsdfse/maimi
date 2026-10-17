'use strict';
const { spawn } = require('node:child_process');
// The caller retains this exact ChildProcess and its exit event. Never search by
// executable name or terminate unrelated Node/Electron processes.
async function terminateOwnedTree(child, alreadyExited) {
  if (!child?.pid || alreadyExited()) return;
  if (process.platform !== 'win32') { child.kill('SIGTERM'); return; }
  await new Promise(resolve => {
    const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true, shell: false, stdio: 'ignore',
    });
    const timer = setTimeout(() => { killer.kill(); resolve(); }, 8000);
    const done = () => { clearTimeout(timer); resolve(); };
    killer.once('error', done); killer.once('exit', done);
  });
  if (!alreadyExited()) child.kill('SIGKILL');
}
module.exports = { terminateOwnedTree };
