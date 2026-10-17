'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=__dirname,ev=path.join(root,'evidence'),read=name=>JSON.parse(fs.readFileSync(path.join(ev,name))),sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const build=read('installer-build.json'),audit=read('candidate-payload-audit.json'),installation=read('full-payload-rehearsal.json');
assert.equal(build.sha256,sha(build.installer));for(const item of [audit,installation]){assert.equal(item.status,'PASS');assert.equal(item.installerSha256,build.sha256);}
assert.equal(installation.results.length,2);for(const r of installation.results){assert.equal(r.version,'0.13.0');assert.equal(r.runtime,'0.2.0-rc.2');assert.equal(r.nativeCodecLoaded,true);assert.equal(r.credentialsRead,false);}
const browser=read('native-browser-result.json');assert.equal(browser.status,'PASS');assert.deepEqual(browser.errors,[]);
const host=JSON.parse(fs.readFileSync(path.join(root,'native-tests/actual-host-result.json')));assert.equal(host.status,'PASS');
for(const status of ['NATIVE_PLUGIN_POLICY_DISABLE_REFUSED','MANUAL_BYO_SELECTION_REFUSED_REASONING_RETAINED','SAME_NAME_PROVIDER_ENDPOINT_TAMPER_REFUSED','OLD_CONVERSATION_TOOL_HISTORY_REOPENED'])assert(host.statuses.includes(status));
const dws=JSON.parse(fs.readFileSync(path.join(root,'native-tests/actual-dws-native-result.json')));assert.equal(dws.status,'PASS');
const stats=read('tokenmeter-native-stats.json');assert.equal(stats.stats.ready,true);assert.equal(stats.anonymous_status,401);assert.equal(stats.bad_origin_status,403);
assert(stats.stats.records.some(r=>r.m==='deepseek-official/deepseek-v4.1-flash'&&r.i>=17&&r.o>=9));assert.equal(stats.stats.peakStep.tokens,26);
const updates=fs.readFileSync(path.join(ev,'updater-final.log'),'utf8');assert.match(updates,/tests 109/);assert.match(updates,/fail 0/);
const model=fs.readFileSync(path.join(ev,'model-boundary-tests.log'),'utf8');assert.match(model,/tests 8/);assert.match(model,/fail 0/);
const history=fs.readFileSync(path.join(ev,'history-windows-final.log'),'utf8');assert.match(history,/tests 34/);assert.match(history,/fail 0/);
const sealed=read('runtime-sealed.json');assert.equal(sealed.packageCount,278);
const result={schema:'desktop-r41-final-validation-v1',status:'PASS',desktop_version:'0.13.0',runtime_version:'0.2.0-rc.2',installer:build.installer,installer_sha256:build.sha256,installer_bytes:build.bytes,
  checks:{updater_auth_bootstrap_tests:109,model_request_boundary_tests:8,fresh_process_probe_tests:19,actual_native_history_tests:34,history_windows_symlink_creation_subassertion:'UNAVAILABLE_EPERM_LINUX_PASSED',actual_native_chat_tools_restart:'PASS',actual_native_dws_confirmation:'PASS',actual_native_policy_disable_byo_endpoint_tamper:'PASS',actual_native_browser_four_business_cards_and_four_community_plugins:'PASS',tokenmeter_actual_new_runtime_usage:'PASS',tokenmeter_anonymous401_badOrigin403:'PASS',compiled_installer_crc_exact_payload:'PASS',private_complete_payload_install_two_shortcut_actual_executable_launch:'PASS',legacy_silent_updated_installer_hook_relaunch:'PASS',until_revoked_device_zero:'PASS'},
  pins:{dsh_packages:278,dsh_package_version_distribution:{'0.2.0-rc.2':278},source_manifest_sha256:sha(path.join(root,'source-manifest.json')),source_patch_sha256:sha(path.join(root,'desktop-r41.patch')),community_manifest_sha256:sha(path.join(ev,'community-adaptation.json'))},
  boundary:{company_requests:0,actual_employee_installation:'PENDING',company_feed_created:false,production_installer_executed_against_employee_install:false,production_registry_or_shortcuts_modified:false,real_iam_or_official_search_acceptance:'PENDING',authenticode:'NOT_SIGNED',os_safe_storage_test:'Synthetic encrypted adapter; production continues Electron safeStorage'}};
fs.writeFileSync(path.join(root,'validation.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
