# ai_summary_pdf
本项目主要用于植物病理学科研工作者批量阅读文献，提取知识点后绘制文献信息网络图，暂时不用于商业用途；
脚本由 chatgpt codex v5.2编写；
目前暂不支持其他专业科研工作者用于文献知识点提取

# 使用前注意
将待阅读的pdf文件存储在一个文件夹下，将主脚本ai_studio_code.py至于该文件夹下，直接从终端开启运行

# 大模型选择，务必在主脚本ai_studio_code.py中填写好自己的硅基流动 API key
硅基流动平台免费大模型：THUDM/glm-4-9b-chat \n

# 脚本运行前务必安装以下python扩展包
brew install tesseract tesseract-lang
pip install pdfplumber openai networkx pyvis pdf2image pytesseract

# 脚本三种运行模式：
只新建或更新知识库与文献列表：python3 ai_studio_code.py -i ./pdf文献路径 --mode extract
只根据最新 RAG 知识库重绘文献网络图：python3 ai_studio_code.py -i ./pdf文献路径 --mode network
提取知识点后+绘制文献网络：python3 ai_studio_code.py -i ./pdf文献路径 --mode both

