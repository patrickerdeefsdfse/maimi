---
name: company-ppt-template
description: 使用实际麦米电气公司PPT模板制作、复用版式或改写可编辑演示文稿。用户要求公司PPT模板、麦米模板、MEGMEET模板或相近表达时使用，尊重否定表达及用户指定的其他模板。
---

# 公司 PPT 模板

用户在普通对话中要求按公司、麦米、麦格米特、MEGMEET 模板制作演示文稿时使用。按语义判断，不要求固定口令或手动选择技能。用户指定另一个模板、要求保留上传文件原有版式或明确不用公司模板时，遵循本次选择。仅提到公司名称不等于要求生成 PPT。继续使用当前员工的模型、会话和现有文件工具，不索取或切换 TokenKey。

本技能资源相对于返回的 `resourceBase.path`：

- `assets/company-template-v5.3.pptx`：实际公司模板，只读，SHA256 `0e742339972cbfb37f9e5c10c8f907f0f6fd95cfcf5409118a076c7258869e99`，22275510 字节。
- `scripts/company_ppt.py`：本部署已提供的标准库模板复用工具，不需要 python-pptx、lxml、PIL、zip 或 unzip。
- `scripts/preview_pdf.py`：使用已安装的 Office Python 把工作区 PDF 顺序渲染为 PNG，不启动 LibreOffice。
- `references/template-guide.md`：版式、可执行步骤和检查要求。制作前读取。

如果用户只要原模板，复制到工作区的新文件名并交付，不改样例。制作成品时，按用户主题和材料编排内容，页数包含封面与结尾。模板共 24 页是样式库页数，不是每份报告的目标页数。

## 在当前麦米环境制作

1. 读取本次实际加载的本技能指引和 `office-pptx`，按当前部署路径执行；不要沿用历史对话里关于依赖缺失的结论。Office Python 固定为 `/opt/office/venv/bin/python -I -B`。管理员只读检查已确认 Office 库文件存在，本地对应运行环境已验证 `pptx`、`lxml.etree`、`PIL.Image`、`pypdfium2` 可导入；默认 `python3` 属于不同环境，其导入失败不能证明上述环境没有库。仅在该配置路径实际失败时做一次导入检查并报告具体结果。当前企业配置已禁用 LibreOffice Kit，不探测或安装 WASM／Kit CLI；PDF 转换使用现有 `office_document`，PDF 预览使用随技能提供的 `preview_pdf.py`。
2. 将模板复制到本任务工作区的新文件。使用现有执行工具运行随本技能提供的 `company_ppt.py inspect`，读取需要页的对象编号和文本 run。按 `template-guide.md` 编写内容计划，然后调用 `compose` 生成新的 `.pptx`。工具保留母版、布局、主题、媒体和文字格式，并将每次复用的来源页制作成独立幻灯片，避免重复来源页互相覆盖。
3. 替换目标为“输出页顺序 + 来源页 + 本页 shape_id”。不要靠 shape 名字或全局文本替换定位。逐项替换、保留或清空模板正文，移除样例数据、备注和不需要的说明页。按实际来源核对产品名和数据；模板示例不是业务事实。
4. 先检查结构和最终正文，再调用现有 `office_document` 的 `convert` 把工作区 PPTX 转为新 PDF。使用 `/opt/office/venv/bin/python -I -B` 运行 `scripts/preview_pdf.py`，把 PDF 逐页渲染为 PNG。具体参数见指引。工作区相对路径才是 `office_document` 的文件输入，不能直接传共享资源的 `/opt/...` 路径。有图片查看能力时逐页查看真实渲染图，修复溢出和遮挡，保留可编辑 PPTX。
5. 同一制作任务的转换按顺序执行，不并行启动多个 LibreOffice。已确认不可用的能力不反复探测。遇到进程名额耗尽、转换失败时停止追加子进程，保留成品和清晰错误，不通过反复换文件名或重复安装掩盖环境故障，也不结束其他聊天的进程。
6. 通过现有 `present` 或会话文件方式交付实际工作区成品。只完成结构／文字检查时，明确尚未完成视觉验证，不说“每页排版已确认”。

本工具只处理已核对的公司模板的页面选择与文字替换。新增图表、替换图片、改变复杂图形等需求，使用当前真实可用的 DSH／Office 能力，按原模板风格完成并另行检查。不能将整页截图、空白 PPT 或仅保留主题色的文件冒充已保留模板的可编辑成品。
