import test from 'node:test'
import assert from 'node:assert/strict'
import { companyUpdateFeed, isCompanyInstaller, isCompanyUpdateRequest } from '../shell/src/updates/company-feed.ts'

test('both channels, legacy latest, installer, and differential updates use the company feed', () => {
  for (const file of ['stable.yml?noCache=1','nightly.yml','latest.yml','Maimi-Setup-0.15.4-x64.exe','Maimi-Setup-0.15.3-x64.exe.blockmap']) {
    assert.equal(isCompanyUpdateRequest(companyUpdateFeed+file), true)
  }
  for (const url of ['https://github.com/deepseek-ai/deepseek-harness/releases','https://desktop-gateway.example.invalid.example.org/maimi/updates/windows/stable.yml','http://desktop-gateway.example.invalid/maimi/updates/windows/stable.yml','https://user@desktop-gateway.example.invalid/maimi/updates/windows/stable.yml',companyUpdateFeed+'../other/stable.yml',companyUpdateFeed+'DeepSeek-Setup-0.15.4-x64.exe']) {
    assert.equal(isCompanyUpdateRequest(url), false)
  }
})

test('the advertised release matches the installer name, SHA512 and sole download', () => {
  const file = {url: 'Maimi-Setup-0.15.4-x64.exe', sha512: Buffer.alloc(64).toString('base64')}
  assert.ok(isCompanyInstaller('0.15.4',[file]))
  assert.ok(isCompanyInstaller('0.15.4',[{...file, url:companyUpdateFeed+file.url}]))
  assert.equal(isCompanyInstaller('0.15.3',[file]),false)
  assert.equal(isCompanyInstaller('0.15.4',[{...file, url:'https://github.com/'+file.url}]),false)
  assert.equal(isCompanyInstaller('0.15.4',[{...file, sha512:'invalid'}]),false)
  assert.equal(isCompanyInstaller('0.15.4',[file,file]),false)
})
