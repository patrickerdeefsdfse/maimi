'use strict';
(async () => {
  const button = document.getElementById('download');
  const state = document.getElementById('release-state');
  try {
    const response = await fetch('/maimi/release.json', { cache: 'no-store', credentials: 'omit' });
    if (!response.ok) throw new Error('release unavailable');
    const release = await response.json();
    if (release.status === 'unpublished') {
      state.textContent = '安装包正在准备中，请稍后再来。';
      return;
    }
    if (release.schema_version !== 1 || release.status !== 'published'
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.exe$/.test(release.filename)
        || release.filename.includes('..') || !/^[a-f0-9]{64}$/.test(release.sha256)) throw new Error('release invalid');
    button.href = '/maimi/releases/' + encodeURIComponent(release.filename);
    button.setAttribute('download', release.filename);
    button.removeAttribute('aria-disabled');
    button.classList.remove('disabled');
    state.textContent = '版本 ' + release.version + ' · ' + (release.size_bytes / 1000 / 1000).toFixed(1) + ' MB';
    document.getElementById('checksum').textContent = release.sha256;
    document.getElementById('checksum-details').hidden = false;
  } catch {
    state.textContent = '暂时无法读取下载信息，请稍后刷新页面。';
  }
})();
