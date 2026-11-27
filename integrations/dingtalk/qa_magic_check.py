"""Compile the MagicAPI fragment with real Java/MagicScript and ephemeral synthetic credentials."""
from pathlib import Path
import os
import secrets
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parent
cp=os.environ.get("MAIMI_MAGIC_TEST_CLASSPATH")
if not cp:
    raise SystemExit("Set MAIMI_MAGIC_TEST_CLASSPATH to the installed QA MagicScript 1.9.0 / MagicAPI 2.2.2 + Spring 5.3 and Fastjson JAR classpath.")
with tempfile.TemporaryDirectory(prefix="maimi-im-magic-") as work:
    tmp=Path(work)
    baseline=tmp/"synthetic-legacy.ms"
    baseline.write_text('var lgInputText = body.text.content; var wt = lgInputText.trim(); return "legacy";',encoding="utf8")
    classpath=cp+os.pathsep+str(tmp)
    subprocess.run(["javac","-encoding","UTF-8","-cp",classpath,"-d",str(tmp),str(ROOT/"MagicBranchTest.java")],check=True)
    for name in ('provider.txt','forward.txt'):
        (tmp/name).write_text(secrets.token_hex(32),encoding="ascii")
    env=dict(os.environ,MAIMI_IM_CALLBACK_MODE="direct-https",MAIMI_IM_QA_DIAGNOSTICS="1",
             MAIMI_IM_DING_SECRET_FILE=str(tmp/"provider.txt"),MAIMI_IM_FORWARD_KEY_FILE=str(tmp/"forward.txt"),
             MAIMI_IM_RELAY_URL="https://relay.invalid/internal/maimi-im/v1/dingtalk")
    subprocess.run(["java","-Dfile.encoding=UTF-8","-cp",classpath,"MagicBranchTest",str(baseline),str(ROOT/"MagicAPI-麦米私聊分流.ms")],check=True,env=env)

