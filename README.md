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

推荐使用 openrouter 平台提供的免费大模型：
stepfun/step-3.5-flash:free

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

python3 ai_studio_code.py \
   -i ./pdf文献路径 \
   --mode extract \
   --read-mode deep \
   --platform openrouter \
   --model stepfun/step-3.5-flash:free \
   --prompt-system-file prompt_system.txt \
   --rpm 0 \
   --api-key sk-or-v1-xxxxxxxxxxxxxxxxxxx
   
用于从 PDF 文献中提取关键信息，并构建或更新知识库与文献列表。

---

### 2. 仅构建文献关系网络图

python3 ai_studio_code.py \
   -i ./pdf文献路径 \
   --mode network \
   --read-mode deep \
   --platform openrouter \
   --model stepfun/step-3.5-flash:free \
   --prompt-system-file prompt_system.txt \
   --rpm 0 \
   --api-key sk-or-v1-xxxxxxxxxxxxxxxxxxx

基于已有的 RAG 知识库，重新生成文献知识网络图。

---

### 3. 全流程执行（推荐）

python3 ai_studio_code.py \
   -i ./pdf文献路径 \
   --mode both \
   --read-mode deep \
   --platform openrouter \
   --model stepfun/step-3.5-flash:free \
   --prompt-system-file prompt_system.txt \
   --rpm 0 \
   --api-key sk-or-v1-xxxxxxxxxxxxxxxxxxx
    
执行完整流程：
文献解析 → 知识提取 → 知识库更新 → 网络图构建

---

## 📁 结果文件（Outputs）

本脚本运行后将生成以下三类结果文件：

### 📚 1. 提取的文献知识库

`pathology_report.md`

* 格式：Markdown
* 说明：用于存储提取的文献知识点与结构化内容
* 打开方式：可使用 Markdown 编辑器（如 Obsidian、Typora 等）

---

### 🌐 2. 文献网络图

`plant_pathology_network.html`

* 格式：HTML（交互式）
* 说明：基于知识库构建的文献关系网络
* 打开方式：直接使用浏览器打开

---

### 📊 3. 文献信息表

`paper_summary_table.csv`

* 格式：CSV
* 说明：记录原始文献信息，并与任务中的文献编号建立对应关系

---


## 备注

本项目仍在持续优化中，欢迎根据自身科研需求进行二次开发与功能扩展。
