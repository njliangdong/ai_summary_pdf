# ai_summary_pdf
# 脚本运行前务必安装以下python扩展包

brew install tesseract tesseract-lang

pip install pdfplumber openai networkx pyvis pdf2image pytesseract

三种运行模式：
--mode extract：只提取知识点，更新 RAG 知识库、Markdown、文献列表（默认）。
--mode network：只根据最新 RAG 知识库重绘网络图。
--mode both：提取后重绘网络。

# 只新建或更新知识库与文献列表
python3 ai_studio_code.py -i ./pdf文献路径 --mode extract

# 只绘制文献网络
python3 ai_studio_code.py -i ./pdf文献路径 --mode network

# 提取知识点+绘制文献网络
python3 ai_studio_code.py -i ./pdf文献路径 --mode both

