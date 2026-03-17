# ai_summary_pdf

## 项目简介

本项目旨在帮助植物病理学科研工作者实现批量文献阅读、关键信息提取以及文献信息网络图的自动构建。
当前版本仅面向科研用途，不涉及任何商业应用场景。

本项目核心脚本由 ChatGPT Codex v5.2 辅助编写完成。

⚠️ 注意：
目前该工具主要针对植物病理学领域进行优化，暂不支持其他学科的自动化知识提取。
如需适配其他研究方向，可根据主脚本 `ai_studio_code.py` 中的第2部分与第5部分进行自定义修改。

---

## 使用前准备

请将所有待处理的 PDF 文献文件统一存放于同一目录下，并将主脚本 `ai_studio_code.py` 放置于该目录中。

随后，在终端中进入该目录并运行脚本即可。

---

## 大模型配置

在运行脚本前，请务必在 `ai_studio_code.py` 中正确填写您的 API Key。

推荐使用硅基流动平台提供的免费大模型：
THUDM/glm-4-9b-chat

---

## 环境依赖

在运行脚本前，请确保已安装以下系统工具及 Python 依赖：

### 系统依赖

brew install tesseract tesseract-lang

### Python 依赖

pip install pdfplumber openai networkx pyvis pdf2image pytesseract

---

## 运行模式

本脚本提供三种运行模式，可根据需求选择：

### 1. 仅提取知识并更新知识库

python3 ai_studio_code.py -i ./pdf文献路径 --mode extract

用于从 PDF 文献中提取关键信息，并构建或更新知识库与文献列表。

---

### 2. 仅构建文献关系网络图

python3 ai_studio_code.py -i ./pdf文献路径 --mode network

基于已有的 RAG 知识库，重新生成文献知识网络图。

---

### 3. 全流程执行（推荐）

python3 ai_studio_code.py -i ./pdf文献路径 --mode both

执行完整流程：
文献解析 → 知识提取 → 知识库更新 → 网络图构建

---

## 备注

本项目仍在持续优化中，欢迎根据自身科研需求进行二次开发与功能扩展。
