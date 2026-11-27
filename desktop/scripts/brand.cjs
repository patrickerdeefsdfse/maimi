const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const load = createRequire(process.env.MAIMI_BUILD_TOOLS_PACKAGE??path.join(__dirname,'package.json'));
const ResEdit = load('resedit');
const PE = load('pe-library');
const stage = path.resolve(process.env.MAIMI_WORK,'windows/win-unpacked');
const file = path.join(stage,'Maimi.exe');
const original = path.resolve(process.env.MAIMI_WORK,'prepared/windows/electron/electron.exe');
const exe = PE.NtExecutable.from(fs.readFileSync(original),{ignoreCert:true});
const res = PE.NtExecutableResource.from(exe);
const icon = ResEdit.Data.IconFile.from(fs.readFileSync(path.join(stage,'resources/tray.ico')));
for (const group of [...res.entries].filter(entry=>entry.type===14)) {
  ResEdit.Resource.IconGroupEntry.replaceIconsForResource(res.entries,group.id,group.lang,icon.icons.map(item=>item.data));
}
const version=JSON.parse(fs.readFileSync(path.join(stage,'resources/app/package.json'))).version.split('.').map(Number);
if(version.length!==3||!version.every(Number.isInteger))throw Error('Invalid product version');
for (const vi of ResEdit.Resource.VersionInfo.fromEntries(res.entries)) {
  vi.setFileVersion(...version,0,1033);
  vi.setProductVersion(...version,0,1033);
  vi.setStringValues({lang:1033,codepage:1200},{ProductName:'麦米办公助手', FileDescription:'麦米办公助手',
    OriginalFilename:'Maimi.exe',InternalName:'Maimi',CompanyName:'Megmeet'});
  vi.outputToResourceEntries(res.entries);
}
res.outputResource(exe);
fs.writeFileSync(file,Buffer.from(exe.generate()));
console.log('MAIMI_PRODUCT_ICON_AND_VERSION_READY');
