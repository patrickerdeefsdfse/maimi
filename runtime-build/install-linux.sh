set -euo pipefail
TASK=/home/megmeet/dsh020-r41
mkdir -p "$TASK/runtime-linux-x64" "$TASK/npm-cache" "$TASK/evidence"
cd "$TASK/runtime-linux-x64"
node -e 'require("fs").writeFileSync("package.json", JSON.stringify({name:"megmeet-dsh020-linux-candidate",private:true,dependencies:{"@deepseek-ai/dsh":"0.2.0-rc.2"}},null,2)+"\n")'
npm install --ignore-scripts --no-audit --no-fund --cache "$TASK/npm-cache" > "$TASK/evidence/npm-install.log" 2>&1
node -e 'const fs=require("fs");let v=require("./node_modules/@deepseek-ai/dsh/package.json").version;if(v!=="0.2.0-rc.2")throw Error(v);console.log(JSON.stringify({status:"INSTALLED",version:v,platform:process.platform,arch:process.arch}))'

