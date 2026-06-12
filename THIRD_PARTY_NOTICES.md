# 第三方开源项目说明

本仓库不内置、不复制、不重新分发第三方项目源码。下面列出的项目是运行时依赖或示例依赖，用户需要按需自行安装和配置。

## Microsoft MarkItDown

- 项目地址：https://github.com/microsoft/markitdown
- 本项目中的用途：`examples/ocr_adapter.py` 示例适配器使用它把文档转换成 Markdown
- 许可证：MIT License

用户通过 Python 包管理工具自行安装 MarkItDown。本仓库不重新分发 MarkItDown。

## LibreOffice

- 项目地址：https://www.libreoffice.org/
- 本项目中的用途：当遇到 `.doc`、`.ppt`、`.xls` 等旧版 Office 文件时，运行时调用 LibreOffice 转成 `.docx`、`.pptx`、`.xlsx`
- 许可证：LibreOffice 主要基于 MPL-2.0 等开源许可证发布，并包含若干第三方组件声明。权威许可证信息以 LibreOffice 官方项目为准。

用户自行安装 LibreOffice。本仓库不重新分发 LibreOffice。

## OpenAI 兼容 API、客户端和模型服务

- 示例 Python 客户端：https://github.com/openai/openai-python
- 本项目中的用途：AI 文件重命名、目录整理，以及示例 OCR 适配器中的可选 LLM 能力
- openai-python 许可证：Apache-2.0

不同模型服务、网关和 API 提供方可能有不同的服务条款和数据使用政策。用户需要自行确认所选服务是否适合处理自己的文档内容。
