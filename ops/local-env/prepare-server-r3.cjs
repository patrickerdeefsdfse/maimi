const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../..');
const files=['ops/local-env/export-changsha-images.py','ops/local-env/package-changsha.py',
 'dev-local/rehearse_final_package.py','dev-local/rehearse_changsha.py',
 'desktop-local/scripts/assert-release-separation.ps1',
 'ops/changsha-desktop/compose.yaml','ops/changsha-desktop/personal-cloud.compose.yaml',
 'ops/changsha-desktop/verify-images.py','ops/changsha-desktop/README.md','ops/changsha-desktop/FIRST-PILOT.md'];
for(const file of files){const p=path.join(root,file);let s=fs.readFileSync(p,'utf8');
 s=s.replaceAll('Personal-Cloud-20260918-r2','Personal-Cloud-20260918-r3').replaceAll('personal-cloud-20260918-r2','personal-cloud-20260918-r3')
 .replaceAll('megmeet-desktop-release-20260918-r2','megmeet-desktop-release-20260918-r3')
 .replaceAll('megmeet-ai-gateway:personal-cloud-20260918\'','megmeet-ai-gateway:personal-cloud-20260918-r3\'')
 .replaceAll('megmeet-ai-gateway:personal-cloud-20260918`','megmeet-ai-gateway:personal-cloud-20260918-r3`')
 .replaceAll('image: megmeet-ai-gateway:personal-cloud-20260918\n','image: megmeet-ai-gateway:personal-cloud-20260918-r3\n')
 .replaceAll('image: megmeet-ai-gateway:personal-cloud-20260918\r\n','image: megmeet-ai-gateway:personal-cloud-20260918-r3\r\n')
 .replaceAll('megmeet-personal-cloud-manager:20260918-r1','megmeet-personal-cloud-manager:20260918-r3')
 .replaceAll('megmeet-personal-dsh:20260918-r1','megmeet-personal-dsh:20260918-r3')
 .replaceAll('FINAL_R2_','FINAL_R3_').replaceAll('EXACT_R2_','EXACT_R3_').replaceAll('桌面0.4.1','桌面0.4.2');
 fs.writeFileSync(p,s);
}
