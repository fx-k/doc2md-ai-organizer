# doc2md-ai-organizer

把一个文件夹里的资料批量转换成 Markdown，再用 AI 自动重命名文件、整理二级目录，最终得到一个更适合 AI 检索和知识库导入的资料文件夹。

这个工具适合个人知识库整理、历史资料迁移、RAG 语料准备、团队文档归档等场景。它不会修改原始文件夹，所有转换结果都会写入一个新的输出目录，目录名前缀为 `👌🤖`。

## 功能特点

- 批量把文件夹下的资料转换成 Markdown。
- OCR/文档转换能力通过适配器接入，不强绑定某一个 OCR 服务。
- 旧版 Office 文件会先自动转成新版格式再处理：
  - `.doc`、`.dot` -> `.docx`
  - `.ppt`、`.pps`、`.pot` -> `.pptx`
  - `.xls`、`.xlt` -> `.xlsx`
- 使用 OpenAI 兼容的 Chat Completions 接口自动生成更适合检索的文件名。
- 使用同一个 AI 接口把 Markdown 文件归类到若干二级目录。
- 不改动原始资料。
- 支持中断后续跑。
- 会记录运行日志、重试异常、失败文件和中间产物。
- 自动跳过 `.DS_Store` 和 Word/WPS 常见临时锁文件，例如 `~$xxx.docx`。

## 仓库定位

本仓库只提供“流程编排脚本”和一个示例 OCR 适配器。

本仓库不包含：

- OCR 引擎源码
- LibreOffice 安装包
- 模型权重
- API Key
- 私有模型服务
- 示例业务资料

换句话说：这个项目负责把“文件转换、AI 重命名、AI 目录整理、重试、日志、续跑”串起来；具体 OCR 和模型服务由用户自己配置。

## 环境要求

- Python 3.10 或更高版本
- 一个 OpenAI 兼容的 Chat Completions 接口，用于 AI 重命名和目录整理
- 可选但推荐：LibreOffice，用于把旧版 Office 文件转换成新版 Office 文件
- 一个 OCR/转换适配器：接收一个文件路径参数，并把 Markdown 输出到 stdout

仓库内置的示例适配器使用 Microsoft MarkItDown。你也可以换成自己的 OCR 脚本，只要满足相同的命令约定即可。

注意：MarkItDown 要求 Python 3.10 或更高版本。建议使用虚拟环境，避免和系统 Python 或其他项目依赖冲突。

## 快速开始

克隆仓库并创建虚拟环境：

```bash
git clone https://github.com/fx-k/doc2md-ai-organizer.git
cd doc2md-ai-organizer
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements-ocr-example.txt
```

如果需要处理 `.doc`、`.ppt`、`.xls` 等旧版 Office 文件，建议安装 LibreOffice：

```bash
brew install --cask libreoffice
```

配置 AI 接口。脚本会自动读取项目目录下的 `.env`，也可以读取当前 shell 中已经 export 的环境变量。命令行参数优先级最高，其次是环境变量，最后才是 `.env`：

```bash
cp .env.example .env
```

然后编辑 `.env`，填入你的 AI 接口配置：

```env
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=你的 API Key
AI_MODEL=gpt-4o-mini
```

运行转换：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder"
```

输出目录会生成在原始目录旁边：

```text
/path/to/👌🤖source-folder
```

## 使用本地 OpenAI 兼容网关

如果你使用本地模型网关或公司内部模型网关，可以这样配置：

```env
AI_BASE_URL=http://127.0.0.1:8317/v1
AI_MODEL=你的模型名称
AI_API_KEY=如果网关需要 key，就填这里
```

如果本地网关不校验 key，可以让 `AI_API_KEY` 为空。

## OCR 适配器约定

主流程会这样调用 OCR 适配器：

```bash
python path/to/adapter.py "/path/to/input-file" > output.md
```

适配器需要满足：

- 接收一个输入文件路径
- 把 Markdown 输出到 stdout
- 把错误信息输出到 stderr
- 失败时返回非 0 退出码

默认适配器是：

```text
examples/ocr_adapter.py
```

如果你有自己的 OCR 脚本，可以这样指定：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" \
  --ocr-python "/path/to/python" \
  --ocr-script "/path/to/your_adapter.py"
```

## 中断后如何续跑

脚本会在输出目录里保存状态：

```text
👌🤖source-folder/._ocr_markdown_pipeline/
```

常用文件说明：

- `run.log`：运行日志
- `errors.jsonl`：每次重试和异常记录
- `manifest.json`：可续跑的文件级状态
- `failed_files.json`：重试后仍失败的文件
- `office_converted/`：旧版 Office 转新版 Office 后的中间文件
- `converted_flat/`：AI 重命名前的 Markdown
- `renamed_flat/`：AI 重命名后的 Markdown

如果脚本被中断，重新执行同一条命令即可。已经成功的中间文件会被复用。

## 常用命令

只检查输入和输出路径，不真正转换：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" --dry-run
```

如果默认输出目录已经存在，创建一个带时间戳的新输出目录：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" --force-new-output
```

指定 LibreOffice 路径：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" \
  --soffice "/Applications/LibreOffice.app/Contents/MacOS/soffice"
```

增加重试次数：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" --retries 5 --retry-wait 20
```

## 常见问题

### 已经安装 MarkItDown，但仍提示 `No module named 'markitdown'`

这通常是因为 OCR 适配器使用的 Python 不是当前虚拟环境里的 Python。

先确认已经激活虚拟环境：

```bash
source .venv/bin/activate
which python
python -c "import markitdown; print('markitdown ok')"
```

如果仍然失败，可以显式指定 OCR 适配器使用虚拟环境里的 Python：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" \
  --ocr-python ".venv/bin/python"
```

### AI 重命名阶段提示 `Remote end closed connection without response`

这说明 OCR 已经完成，问题发生在连接 AI 接口时。常见原因：

- 没有配置 `AI_API_KEY`
- `AI_BASE_URL` 仍是默认的 `https://api.openai.com/v1`，但当前网络无法访问
- 本地或公司模型网关地址填错
- 代理、证书或网关服务临时断开

先用 dry-run 检查脚本实际读到的配置：

```bash
./ocr_markdown_pipeline.py "/path/to/source-folder" --dry-run
```

重点看输出里的：

```text
AI base URL: ...
AI model: ...
AI API key configured: True/False
```

如果你使用本地网关，请把 `.env` 改成你的网关地址，例如：

```env
AI_BASE_URL=http://127.0.0.1:8317/v1
AI_MODEL=你的模型名称
AI_API_KEY=如果需要就填写
```

## 第三方开源项目说明

本项目是一个编排层，会调用第三方工具，但不会把这些第三方项目的源码打包进本仓库。

- MarkItDown：示例 OCR 适配器使用它把文档转换成 Markdown。
- LibreOffice：运行时可选，用于旧版 Office 文件格式转换。
- OpenAI 兼容接口：用于 AI 重命名和目录整理，也可以用于 OCR 辅助。

详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 安全提醒

- 不要提交 `.env`。
- 不要把 API Key 写死在脚本或适配器里。
- 这个工具会把文档片段发送给 AI 接口，用于重命名和目录整理。请只使用适合处理你资料内容的模型服务。
- 如果原始资料包含隐私、商业秘密、受监管信息，发布转换结果前请人工复核。

## 许可证

MIT。见 [LICENSE](LICENSE)。
