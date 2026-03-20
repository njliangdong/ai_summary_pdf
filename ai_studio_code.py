import os
import json
import re
import time
import argparse
import logging
import csv  # 新增：用于导出 Excel 兼容的表格
import getpass
import shutil
import pdfplumber
from pyvis.network import Network
from openai import OpenAI
import pytesseract
from pdf2image import convert_from_path

# ==========================================
# 1. 大模型配置
# ==========================================
MODEL_NAME = "openrouter/free"
MODEL_PLATFORM = "openrouter"
PLATFORM_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "siliconflow": "https://api.siliconflow.cn/v1"
}

PROMPT_SYSTEM_TEXT = None
client = None
RATE_LIMIT_RPM = 0
LAST_REQUEST_TS = 0.0
ALLOW_JSON_REPAIR = False

# ==========================================
# 2. 实体分类与颜色映射字典
# ==========================================
ENTITY_COLORS = {
    "微生物": "#8e44ad",      # 紫色 
    "植物宿主": "#27ae60",    # 绿色 
    "蛋白分子": "#e74c3c",    # 红色 
    "核酸元件": "#e67e22",    # 橙色 
    "代谢物": "#f1c40f",      # 黄色 
    "化合物": "#3498db",      # 蓝色 
    "生物过程": "#16a085",    # 蓝绿色 
    "未知分类": "#bdc3c7"     # 灰色 
}

ALLOWED_MECHANISM_TYPES = {"蛋白分子", "核酸元件", "代谢物", "化合物"}
EXCLUDED_NETWORK_TYPES = {"微生物", "植物宿主", "生物过程"}
EXCLUDED_NAME_TOKENS = {"植物", "病原", "病原菌", "真菌", "细菌", "病毒", "微生物", "宿主", "生物过程", "免疫", "感染"}

# ==========================================
# 3. 系统与工具函数
# ==========================================

DEBUG_DIR = None

def extract_prompt_block(content, tag):
    if not content:
        return None
    pattern = re.compile(rf"\[{tag}\](.*?)\[/\s*{tag}\]", re.DOTALL | re.IGNORECASE)
    match = pattern.search(content)
    if match:
        return match.group(1).strip()
    return None

def load_prompt_system(prompt_file, read_mode):
    if not prompt_file:
        logging.error("❌ 未提供 --prompt-system-file，无法继续。")
        return None
    if not os.path.exists(prompt_file):
        logging.error(f"❌ 提示词系统文件不存在: {prompt_file}")
        return None
    try:
        with open(prompt_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            logging.error("❌ 提示词系统文件为空。")
            return None
        deep_block = extract_prompt_block(content, "DEEP")
        quick_block = extract_prompt_block(content, "QUICK")
        if read_mode == "quick" and quick_block:
            logging.info(f"✅ 已加载快速阅读提示词: {prompt_file}")
            return quick_block
        if read_mode == "deep" and deep_block:
            logging.info(f"✅ 已加载深度阅读提示词: {prompt_file}")
            return deep_block
        logging.warning("⚠️ 提示词系统未检测到 [DEEP]/[QUICK] 区块，已使用全文。")
        return content
    except Exception as e:
        logging.error(f"❌ 读取提示词系统失败: {e}")
        return None

def prompt_for_api_key(platform):
    env_key = os.getenv("MODEL_API_KEY", "").strip()
    key = getpass.getpass(f"请输入 {platform} API Key（回车使用环境变量 MODEL_API_KEY）: ").strip()
    if not key:
        key = env_key
    return key

def resolve_api_key(args, platform):
    if getattr(args, "api_key", ""):
        return args.api_key.strip()
    return prompt_for_api_key(platform)

def init_client(api_key, platform):
    global client
    base_url = PLATFORM_BASE_URLS.get(platform, PLATFORM_BASE_URLS["openrouter"])
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=150.0
    )

def resolve_platform_model(args):
    platform = (args.platform or "openrouter").strip().lower()
    if platform not in PLATFORM_BASE_URLS:
        logging.warning(f"⚠️ 未识别的平台: {platform}，已回退到 openrouter。")
        platform = "openrouter"
    model = (args.model or "").strip()
    if not model:
        model = "openrouter/free" if platform == "openrouter" else "THUDM/glm-4-9b-chat"
    return platform, model

def log_doc_progress(current_idx, total_count, file_id):
    if total_count <= 0:
        return
    bar_len = 24
    filled = int(bar_len * current_idx / total_count)
    bar = "#" * filled + "-" * (bar_len - filled)
    logging.info(f"📊 阅读进度: [{bar}] {current_idx}/{total_count} {file_id}")

def preflight_model_check():
    if client is None:
        return False
    test_messages = [
        {"role": "system", "content": "你是一个严格的JSON生成器，只输出合法JSON对象。"},
        {"role": "user", "content": "仅输出 JSON：{\"ok\": true}，不要任何多余内容。"}
    ]
    try:
        throttle_by_rpm()
        raw_resp = client.chat.completions.with_raw_response.create(
            model=MODEL_NAME,
            messages=test_messages,
            temperature=0.0
        )
        response = raw_resp.parse()
        raw_output = response.choices[0].message.content.strip()
        parsed = safe_json_loads(raw_output)
        if parsed is None or parsed.get("ok") is not True:
            logging.error("❌ 预检失败：模型未按要求输出 JSON，可能不是聊天模型或不支持严格 JSON 输出。")
            logging.error(f"   输出样例: {raw_output[:200]}")
            return False
        logging.info(f"✅ 模型可用性预检通过: {MODEL_PLATFORM}/{MODEL_NAME}")
        return True
    except Exception as e:
        status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
        payload = getattr(getattr(e, "response", None), "text", None)
        if status == 404:
            logging.error("❌ 预检失败(404)：模型不可用或被 Guardrails/隐私策略拦截。")
            logging.error("   建议检查 OpenRouter 的 Privacy & Guardrails 设置、Provider/Model allowlist。")
        elif status == 401:
            logging.error("❌ 预检失败(401)：API Key 无效或权限不足。")
        elif status == 429:
            logging.error("❌ 预检失败(429)：请求过于频繁，请降低 --rpm 或稍后重试。")
        else:
            logging.error(f"❌ 预检失败：{e}")
        if payload:
            logging.error(f"   原始响应: {payload}")
        return False

def throttle_by_rpm():
    global LAST_REQUEST_TS
    if RATE_LIMIT_RPM <= 0:
        return
    interval = 60.0 / float(RATE_LIMIT_RPM)
    now = time.monotonic()
    if LAST_REQUEST_TS == 0.0:
        LAST_REQUEST_TS = now
        return
    elapsed = now - LAST_REQUEST_TS
    if elapsed < interval:
        time.sleep(interval - elapsed)
    LAST_REQUEST_TS = time.monotonic()

def parse_rate_limit_wait(headers, attempt):
    if not headers:
        return None
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except Exception:
            pass
    reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if reset:
        try:
            reset_val = float(reset)
            now = time.time()
            if reset_val > now:
                return max(0.0, reset_val - now)
            return max(0.0, reset_val)
        except Exception:
            pass
    return min(60.0, (2 ** attempt) + 0.5)

def load_json(filepath, default_val):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return default_val
    return default_val

def save_json(data, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def clean_json_output(raw_text):
    """强力剥离云端模型可能附加的 Markdown 标记"""
    raw_text = raw_text.strip()
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
    if match: return match.group(1)
    match = re.search(r'(\{.*\})', raw_text, re.DOTALL)
    if match: return match.group(1)
    return raw_text

def extract_json_block(text):
    if not text:
        return text
    start = None
    open_char = None
    close_char = None
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in "{[":
            start = i
            open_char = ch
            close_char = "}" if ch == "{" else "]"
            break
    if start is None:
        return text
    depth = 0
    in_str = False
    escape = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:j+1]
    return text

def safe_json_loads(raw_text):
    cleaned = clean_json_output(raw_text)
    cleaned = extract_json_block(cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    cleaned2 = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned2)
    except Exception:
        pass

    try:
        import ast
        cleaned_py = cleaned2.replace("null", "None").replace("true", "True").replace("false", "False")
        parsed = ast.literal_eval(cleaned_py)
        if isinstance(parsed, (dict, list)):
            return parsed
    except Exception:
        pass
    return None

def standardize_entity_name(name):
    """简单的数据清洗，帮助跨文献节点融合"""
    return name.strip().replace("\n", "").replace(" ", "")

def backup_file(filepath):
    if not os.path.exists(filepath):
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = f"{filepath}.bak_{ts}"
    try:
        shutil.copy2(filepath, backup_path)
        logging.info(f"🗄️ 已备份旧数据文件: {backup_path}")
        return backup_path
    except Exception as e:
        logging.warning(f"⚠️ 备份失败: {e}")
        return None

def mechanism_fingerprint(item):
    src = standardize_entity_name(item.get("canonical_source", "")) if isinstance(item, dict) else ""
    tgt = standardize_entity_name(item.get("canonical_target", "")) if isinstance(item, dict) else ""
    rel = (item.get("relation", "") or "").strip() if isinstance(item, dict) else ""
    stance = (item.get("stance", "") or "support").strip()
    return f"{src}||{rel}||{tgt}||{stance}"

def merge_method_lists(methods_a, methods_b):
    merged = []
    seen = set()
    for entry in (methods_a or []) + (methods_b or []):
        if not isinstance(entry, dict):
            continue
        method = (entry.get("method", "") or "").strip()
        result = (entry.get("result", "") or "").strip()
        if not method and not result:
            continue
        key = f"{method}||{result}"
        if key in seen:
            continue
        seen.add(key)
        merged.append({"method": method, "result": result})
    return merged

def convert_stage1_facts_to_mechanisms(stage1_data):
    mechanisms = []
    facts = stage1_data.get("facts", []) if isinstance(stage1_data, dict) else []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        mechanisms.append({
            "canonical_source": standardize_entity_name(fact.get("source_name", "")),
            "canonical_source_type": normalize_entity_type(fact.get("source_type", "未知分类")),
            "canonical_source_species": normalize_species_list(fact.get("source_species", [])),
            "canonical_target": standardize_entity_name(fact.get("target_name", "")),
            "canonical_target_type": normalize_entity_type(fact.get("target_type", "未知分类")),
            "canonical_target_species": normalize_species_list(fact.get("target_species", [])),
            "relation": fact.get("relation", "互作"),
            "stance": fact.get("stance", "support"),
            "mechanism_summary": fact.get("mechanism_context", "") or fact.get("significance", ""),
            "evidence": {
                "context": fact.get("mechanism_context", ""),
                "quote": fact.get("original_quote", ""),
                "significance": fact.get("significance", "")
            },
            "methods": merge_method_lists([], fact.get("methods", []))
        })
    return mechanisms

def merge_species_lists(species_a, species_b):
    merged = []
    seen = set()
    for item in (species_a or []) + (species_b or []):
        if not item:
            continue
        s = str(item).strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)
    return merged

def merge_category_lists(cat_a, cat_b):
    merged = []
    seen = set()
    for item in (cat_a or []) + (cat_b or []):
        if not item:
            continue
        s = str(item).strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)
    return merged

def doc_id_number(doc_id):
    match = re.search(r'Doc_(\d+)', doc_id or "")
    if match:
        return int(match.group(1))
    return 10**9

def next_available_doc_id(used_ids):
    candidate = 1
    while candidate in used_ids:
        candidate += 1
    used_ids.add(candidate)
    return candidate

def migrate_old_knowledge(all_knowledge, output_json_path):
    if not isinstance(all_knowledge, list) or not all_knowledge:
        return all_knowledge, False
    has_old = any(isinstance(item, dict) and ("source_name" in item or "target_name" in item) for item in all_knowledge)
    if not has_old:
        return all_knowledge, False

    backup_file(output_json_path)
    migrated = []
    for item in all_knowledge:
        if not isinstance(item, dict):
            continue
        if "canonical_source" in item and "canonical_target" in item:
            migrated.append(item)
            continue
        if "source_name" in item or "target_name" in item:
            migrated.append({
                "canonical_source": standardize_entity_name(item.get("source_name", "")),
                "canonical_source_type": normalize_entity_type(item.get("source_type", "未知分类")),
                "canonical_source_species": [],
                "canonical_target": standardize_entity_name(item.get("target_name", "")),
                "canonical_target_type": normalize_entity_type(item.get("target_type", "未知分类")),
                "canonical_target_species": [],
                "relation": item.get("relation", "互作"),
                "stance": "support",
                "mechanism_summary": item.get("mechanism_context", "") or item.get("significance", ""),
                "evidence": {
                    "context": item.get("mechanism_context", ""),
                    "quote": item.get("original_quote", ""),
                    "significance": item.get("significance", "")
                },
                "methods": [],
                "category": "mechanism",
                "ref": item.get("ref")
            })
            continue
        migrated.append(item)
    logging.info("✅ 旧版 insights 已迁移为 mechanisms 结构。")
    return migrated, True

def is_allowed_mechanism_type(entity_type):
    return entity_type in ALLOWED_MECHANISM_TYPES

def is_network_entity_type(entity_type):
    return entity_type not in EXCLUDED_NETWORK_TYPES

def is_excluded_name(name):
    if not name:
        return True
    for token in EXCLUDED_NAME_TOKENS:
        if token in name:
            return True
    return False

def normalize_entity_type(entity_type):
    if not entity_type:
        return "未知分类"
    t = str(entity_type).strip()
    mapping = {
        "蛋白质": "蛋白分子",
        "蛋白": "蛋白分子",
        "基因": "核酸元件",
        "核酸": "核酸元件",
        "DNA": "核酸元件",
        "RNA": "核酸元件",
        "代谢产物": "代谢物",
        "小分子": "化合物"
    }
    return mapping.get(t, t)

def normalize_species_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        # Split by common separators
        parts = re.split(r'[;,/，；]+', raw)
        return [p.strip() for p in parts if p.strip()]
    return []

def is_missing_value(value):
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text == "未提供"

def has_species_info_for_ref(all_knowledge, ref_id):
    for item in all_knowledge:
        if not isinstance(item, dict):
            continue
        if item.get("ref") != ref_id:
            continue
        if normalize_species_list(item.get("canonical_source_species", [])) or normalize_species_list(item.get("canonical_target_species", [])):
            return True
    return False

def needs_refresh_metadata(info, all_knowledge, ref_id):
    if not isinstance(info, dict):
        return True
    for key in ["mechanism_status", "phenotype_status", "bioinfo_status"]:
        if info.get(key) not in {"has", "none"}:
            return True
    return False

def format_doc_index(ref_id, metadata):
    info = metadata.get(ref_id, {}) if isinstance(metadata, dict) else {}
    p_info = info.get("paper_info", {}) if isinstance(info, dict) else {}
    title = p_info.get("title")
    name = title if title and title != "未提供" else info.get("original_name", ref_id)
    return f"{name}（{ref_id}）"

def total_knowledge_count(info):
    if not isinstance(info, dict):
        return 0
    return (
        info.get("mechanisms_count", info.get("insights_count", 0)) +
        info.get("phenotype_count", 0) +
        info.get("bioinfo_count", 0)
    )

def category_status_from_counts(info, zero_status="unknown"):
    status = {}
    status["mechanism_status"] = "has" if info.get("mechanisms_count", info.get("insights_count", 0)) > 0 else zero_status
    status["phenotype_status"] = "has" if info.get("phenotype_count", 0) > 0 else zero_status
    status["bioinfo_status"] = "has" if info.get("bioinfo_count", 0) > 0 else zero_status
    return status

def missing_categories(info):
    missing = []
    if info.get("mechanisms_count", info.get("insights_count", 0)) == 0 and info.get("mechanism_status") != "none":
        missing.append("mechanism")
    if info.get("phenotype_count", 0) == 0 and info.get("phenotype_status") != "none":
        missing.append("phenotype")
    if info.get("bioinfo_count", 0) == 0 and info.get("bioinfo_status") != "none":
        missing.append("bioinfo")
    return missing

def count_by_ref_for_doc(all_knowledge, ref_id):
    counts = {"mechanism": 0, "phenotype": 0, "bioinfo": 0}
    for item in all_knowledge:
        if not isinstance(item, dict):
            continue
        if item.get("ref") != ref_id:
            continue
        category = item.get("category", "mechanism")
        if category == "phenotype":
            counts["phenotype"] += 1
        elif category == "bioinfo":
            counts["bioinfo"] += 1
        else:
            counts["mechanism"] += 1
    return counts

def compute_counts_by_ref(all_knowledge):
    counts = {}
    for item in all_knowledge:
        if not isinstance(item, dict):
            continue
        ref_id = item.get("ref")
        if not ref_id:
            continue
        if ref_id not in counts:
            counts[ref_id] = {"mechanism": 0, "phenotype": 0, "bioinfo": 0}
        category = item.get("category", "mechanism")
        if category == "phenotype":
            counts[ref_id]["phenotype"] += 1
        elif category == "bioinfo":
            counts[ref_id]["bioinfo"] += 1
        else:
            counts[ref_id]["mechanism"] += 1
    return counts

def normalize_generic_points(points, category, ref_id):
    normalized = []
    for item in points or []:
        if not isinstance(item, dict):
            continue
        src = standardize_entity_name(item.get("source_name", ""))
        tgt = standardize_entity_name(item.get("target_name", ""))
        if not src or not tgt:
            continue
        normalized.append({
            "canonical_source": src,
            "canonical_source_type": normalize_entity_type(item.get("source_type", "未知分类")),
            "canonical_source_species": normalize_species_list(item.get("source_species", [])),
            "canonical_target": tgt,
            "canonical_target_type": normalize_entity_type(item.get("target_type", "未知分类")),
            "canonical_target_species": normalize_species_list(item.get("target_species", [])),
            "relation": (item.get("relation", "") or "相关").strip(),
            "stance": "support",
            "mechanism_summary": "",
            "evidence": {
                "context": item.get("evidence", ""),
                "quote": "",
                "significance": ""
            },
            "methods": merge_method_lists([], item.get("methods", [])),
            "category": category,
            "ref": ref_id
        })
    return normalized

def build_species_map_from_stage1(stage1_data):
    species_map = {}
    facts = stage1_data.get("facts", []) if isinstance(stage1_data, dict) else []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        src = standardize_entity_name(fact.get("source_name", ""))
        tgt = standardize_entity_name(fact.get("target_name", ""))
        src_species = normalize_species_list(fact.get("source_species", []))
        tgt_species = normalize_species_list(fact.get("target_species", []))
        if src:
            species_map[src] = merge_species_lists(species_map.get(src, []), src_species)
        if tgt:
            species_map[tgt] = merge_species_lists(species_map.get(tgt, []), tgt_species)
    return species_map

# ==========================================
# 4. 核心功能：文献管理与文本提取
# ==========================================

def manage_and_rename_files(input_folder, metadata):
    all_files = [f for f in os.listdir(input_folder) if f.lower().endswith('.pdf')]
    existing_ids = set()
    for k in metadata.keys():
        match = re.search(r'Doc_(\d+)', k)
        if match:
            existing_ids.add(int(match.group(1)))
    for f in all_files:
        match = re.match(r'^Doc_(\d+)\.pdf$', f, re.IGNORECASE)
        if match:
            existing_ids.add(int(match.group(1)))
    
    new_files_count = 0
    for filename in all_files:
        if re.match(r'^Doc_\d+\.pdf$', filename, re.IGNORECASE):
            if filename not in metadata:
                metadata[filename] = {
                    "original_name": filename,
                    "add_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "pending",
                    "mechanisms_count": 0,
                    "phenotype_count": 0,
                    "bioinfo_count": 0,
                    "mechanism_status": "unknown",
                    "phenotype_status": "unknown",
                    "bioinfo_status": "unknown",
                    "key_references": [],
                    "paper_info": {}
                }
                logging.info(f"📌 保留原编号文献: [{filename}]")
                new_files_count += 1
            continue
            
        next_id = next_available_doc_id(existing_ids)
        new_name = f"Doc_{next_id:04d}.pdf"
        old_path = os.path.join(input_folder, filename)
        new_path = os.path.join(input_folder, new_name)
        
        try:
            os.rename(old_path, new_path)
            metadata[new_name] = {
                "original_name": filename,
                "add_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "pending",
                "mechanisms_count": 0,
                "phenotype_count": 0,
                "bioinfo_count": 0,
                "mechanism_status": "unknown",
                "phenotype_status": "unknown",
                "bioinfo_status": "unknown",
                "key_references": [],
                "paper_info": {} # 新增：用于存储文献基本信息
            }
            logging.info(f"🏷️ 文件重命名: [{filename}] -> [{new_name}]")
            next_id += 1
            new_files_count += 1
        except Exception as e:
            logging.error(f"❌ 重命名失败 {filename}: {e}")
            
    return metadata, new_files_count

def extract_text_hybrid(file_path, max_pages=12):
    logging.info(f"📄 开始解析: {os.path.basename(file_path)}")
    text = ""
    try:
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]): 
                content = page.extract_text()
                if content: text += f"\n[--- Page {i+1} ---]\n" + content + "\n"
    except Exception as e:
        logging.error(f"❌ 原生提取失败: {e}")

    text = re.sub(r'\s+', ' ', text)

    if len(text.strip()) < 500:
        logging.info("🔍 启动 OCR 引擎...")
        try:
            images = convert_from_path(file_path, first_page=1, last_page=6)
            for i, img in enumerate(images):
                ocr_text = pytesseract.image_to_string(img, lang='eng+chi_sim')
                text += f"\n[--- Scanned Page {i+1} ---]\n" + ocr_text + "\n"
        except Exception as e:
            logging.error(f"❌ OCR 崩溃: {e}")
            
    return text

# ==========================================
# 5. 大模型交互 (两阶段：候选事实 -> 机制凝练)
# ==========================================

def run_model(messages, temperature=0.1, debug_tag=None):
    try:
        if client is None:
            raise ValueError("OpenRouter client 未初始化，请先输入 API Key。")
        raw_output = None
        max_retries = 5
        for attempt in range(max_retries + 1):
            try:
                throttle_by_rpm()
                raw_resp = client.chat.completions.with_raw_response.create(
                    model=MODEL_NAME,
                    messages=messages,
                    temperature=temperature
                )
                response = raw_resp.parse()
                raw_output = response.choices[0].message.content.strip()
                break
            except Exception as e:
                status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
                headers = getattr(getattr(e, "response", None), "headers", None)
                if status == 429 and attempt < max_retries:
                    wait = parse_rate_limit_wait(headers, attempt)
                    logging.warning(f"⚠️ 触发限流(429)，等待 {wait:.2f}s 后重试 ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                raise
        if raw_output is None:
            raise ValueError("模型返回为空，无法解析。")
        parsed = safe_json_loads(raw_output)
        if parsed is not None:
            return parsed

        if not ALLOW_JSON_REPAIR:
            if DEBUG_DIR and debug_tag:
                os.makedirs(DEBUG_DIR, exist_ok=True)
                raw_path = os.path.join(DEBUG_DIR, f"{debug_tag}_raw.txt")
                try:
                    with open(raw_path, "w", encoding="utf-8") as f:
                        f.write(raw_output)
                    logging.warning(f"⚠️ 已保存原始模型输出: {raw_path}")
                except Exception as e:
                    logging.warning(f"⚠️ 保存原始输出失败: {e}")
            raise ValueError("JSON parsing failed (repair disabled).")

        # Attempt one-time repair with the model (optional)
        repair_system = "你是JSON修复器。请将输入内容修复为严格有效的JSON，只输出JSON。"
        repair_user = f"请修复以下内容为严格JSON，仅输出JSON：\n{raw_output}"
        repair_output = None
        for attempt in range(max_retries + 1):
            try:
                throttle_by_rpm()
                repair_raw = client.chat.completions.with_raw_response.create(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": repair_system},
                        {"role": "user", "content": repair_user}
                    ],
                    temperature=0.0
                )
                repair_response = repair_raw.parse()
                repair_output = repair_response.choices[0].message.content.strip()
                break
            except Exception as e:
                status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
                headers = getattr(getattr(e, "response", None), "headers", None)
                if status == 429 and attempt < max_retries:
                    wait = parse_rate_limit_wait(headers, attempt)
                    logging.warning(f"⚠️ 触发限流(429)，等待 {wait:.2f}s 后重试 ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                raise
        if repair_output is None:
            raise ValueError("JSON 修复输出为空。")
        parsed = safe_json_loads(repair_output)
        if parsed is not None:
            return parsed
        if DEBUG_DIR and debug_tag:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            raw_path = os.path.join(DEBUG_DIR, f"{debug_tag}_raw.txt")
            repair_path = os.path.join(DEBUG_DIR, f"{debug_tag}_repair.txt")
            try:
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(raw_output)
                with open(repair_path, "w", encoding="utf-8") as f:
                    f.write(repair_output)
                logging.warning(f"⚠️ 已保存原始模型输出: {raw_path}")
            except Exception as e:
                logging.warning(f"⚠️ 保存原始输出失败: {e}")
        raise ValueError("JSON parsing failed after repair attempt.")
    except Exception as e:
        logging.error(f"❌ 模型请求/解析失败: {e}")
        return None

def stage1_extract(input_text, file_id, read_mode="deep", refresh_mode=False, focus_categories=None):
    logging.info(f"🧠 阶段1候选事实抽取 (编号: {file_id})...")
    system_msg = PROMPT_SYSTEM_TEXT or ""
    json_example = """必须输出 JSON 结构：
{
    "paper_info": {
        "title": "论文官方原名(最好是原始英文/中文名)",
        "journal": "期刊名称",
        "year": "出版年份(如 2023)",
        "doi": "DOI号码(如 10.1038/s41586...)",
        "keywords": "关键词(用逗号分隔)"
    },
    "key_references": [
        "第一篇重点参考文献原始格式 (如：Zhang et al., 2021...)",
        "第二篇重点参考文献原始格式"
    ],
    "facts": [
        {
            "source_name": "上游实体标准中文名",
            "source_type": "从7种分类中严格选一",
            "source_species": ["物种名称(如 Fusarium graminearum)", "物种名称2"],
            "target_name": "下游实体标准中文名",
            "target_type": "从7种分类中严格选一",
            "target_species": ["物种名称(如 Triticum aestivum)", "物种名称2"],
            "relation": "精准动作词(如: 催化合成/泛素化降解)",
            "stance": "support/contradict/uncertain",
            "mechanism_context": "来龙去脉详细描述(中文)",
            "original_quote": "从原文摘抄1句最能证明此事实的英文原句",
            "significance": "该事实的生物学意义(中文)",
            "methods": [
                {"method": "方法名", "result": "对应结果或结论"}
            ]
        }
    ],
    "phenotype_points": [
        {
            "source_name": "研究对象/基因/处理/环境因素",
            "source_type": "实体类型(如 基因/物种/处理/环境因子/性状)",
            "source_species": ["物种名称1", "物种名称2"],
            "target_name": "表型/宏观结果/生态结论",
            "target_type": "表型/性状/生态指标/宏观结果",
            "relation": "影响/提高/降低/相关/导致",
            "evidence": "关键结论描述(中文)",
            "methods": [
                {"method": "实验或观察方法", "result": "对应结果"}
            ]
        }
    ],
    "bioinfo_points": [
        {
            "source_name": "基因/蛋白/通路/家族/特征",
            "source_type": "实体类型(如 基因/蛋白/通路/家族/特征)",
            "source_species": ["物种名称1", "物种名称2"],
            "target_name": "生信结论/进化关系/组学结果",
            "target_type": "生信结论/进化/组学/关联结果",
            "relation": "富集/扩张/缺失/相关/保守",
            "evidence": "关键结论描述(中文)",
            "methods": [
                {"method": "生信方法(如 组学分析/系统发育/基因家族分析)", "result": "对应结论"}
            ]
        }
    ]
}
"""
    focus_text = ""
    if refresh_mode and focus_categories:
        focus_text = "本次为查漏补缺，请优先补充以下缺失类别：" + "、".join(focus_categories) + "。避免重复已提取内容。\n"
    mode_note = "快速阅读：一次性输出完整结构化结果。" if read_mode == "quick" else "深度阅读第一阶段：先完成基础信息与候选事实提取。"
    user_msg = (
        f"文献编号：{file_id}\n"
        f"{mode_note}\n"
        "请严格只输出 JSON，不要附加解释或 Markdown。\n\n"
        "任务一：提取文献元数据。如果找不到填“未提供”。\n"
        "任务二：提取作者重点讨论或关键结论支撑的 1-3 篇核心参考文献。\n"
        "任务三：提取 10-20 条候选分子机制事实（允许更细碎）。实体分类仅限：\n"
        "[微生物, 植物宿主, 蛋白分子, 核酸元件, 代谢物, 化合物, 生物过程]。\n"
        "机制功能实验必须有实验验证；生信结论不要求实验验证。\n"
        "同时提取宏观表型/生态结果知识点，以及生物信息学知识点。\n"
        "每条知识点需给出方法与结论（方法名+对应结果）。\n"
        "对于基因/蛋白/核酸元件，尽量标注对应物种（可多物种）。\n"
        f"{focus_text}\n"
    )
    user_msg += json_example
    user_msg += f"\n文本：{input_text}\n"
    return run_model([
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg}
    ], debug_tag=f"{file_id}_stage1")

def stage2_summarize(stage1_data, file_id):
    logging.info(f"🧠 阶段2机制凝练 (编号: {file_id})...")
    system_msg = PROMPT_SYSTEM_TEXT or ""
    compact_stage1 = json.dumps(stage1_data, ensure_ascii=False)
    json_example = """请输出 JSON 结构：
{
    "paper_info": {...},
    "key_references": [...],
    "deep_analysis": {
        "main_results": "作者主要结果的深度解析（中文）",
        "why": "作者为什么这么做（中文）",
        "significance": "这些结果的意义（中文）"
    },
    "mechanisms": [
        {
            "canonical_source": "归一化后的上游实体标准中文名",
            "canonical_source_type": "必须为 [蛋白分子, 核酸元件, 代谢物, 化合物] 之一",
            "canonical_source_species": ["物种名称1", "物种名称2"],
            "canonical_target": "归一化后的下游实体标准中文名",
            "canonical_target_type": "必须为 [蛋白分子, 核酸元件, 代谢物, 化合物] 之一",
            "canonical_target_species": ["物种名称1", "物种名称2"],
            "relation": "精准动作词(如: 催化合成/泛素化降解)",
            "stance": "support/contradict",
            "mechanism_summary": "凝练后的机制摘要(中文，避免宽泛词)",
            "evidence": {
                "context": "机制脉络(中文)",
                "quote": "最有力的英文原句",
                "significance": "生物学意义(中文)"
            },
            "methods": [
                {"method": "方法名", "result": "对应结果或结论"}
            ]
        }
    ]
}
"""
    user_msg = (
        f"文献编号：{file_id}\n"
        "请严格只输出 JSON，不要附加解释或 Markdown。\n"
        "以下为阶段1候选事实 JSON：\n"
        f"{compact_stage1}\n\n"
        "任务：对作者主要结果进行深度解析，并说明作者为什么这么做、这些结果的意义。\n"
        "同时凝练并去重分子机制（保持 stance 冲突）。\n"
    )
    user_msg += json_example
    return run_model([
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg}
    ], debug_tag=f"{file_id}_stage2")

def get_expert_insights(text, file_id, read_mode="deep", refresh_mode=False, focus_categories=None):
    if not text.strip():
        return None
    input_text = text[:18000]
    stage1_data = stage1_extract(input_text, file_id, read_mode=read_mode, refresh_mode=refresh_mode, focus_categories=focus_categories)
    if not stage1_data:
        return None
    run_mechanism = not focus_categories or "mechanism" in (focus_categories or [])
    stage2_data = None
    if read_mode == "quick":
        stage2_data = {
            "paper_info": stage1_data.get("paper_info", {}),
            "key_references": stage1_data.get("key_references", []),
            "mechanisms": convert_stage1_facts_to_mechanisms(stage1_data) if run_mechanism else []
        }
    else:
        stage2_data = stage2_summarize(stage1_data, file_id)
        if not stage2_data:
            return None
        if not run_mechanism:
            stage2_data["mechanisms"] = []

    paper_info = stage2_data.get("paper_info") or stage1_data.get("paper_info", {})
    key_refs = stage2_data.get("key_references") or stage1_data.get("key_references", [])
    mechanisms = stage2_data.get("mechanisms", [])
    deep_analysis = stage2_data.get("deep_analysis", {}) if read_mode == "deep" else {}
    phenotype_points = normalize_generic_points(stage1_data.get("phenotype_points", []), "phenotype", file_id)
    bioinfo_points = normalize_generic_points(stage1_data.get("bioinfo_points", []), "bioinfo", file_id)
    species_map = build_species_map_from_stage1(stage1_data)

    cleaned = []
    for item in mechanisms:
        if not isinstance(item, dict):
            continue
        item["ref"] = file_id
        item["canonical_source"] = standardize_entity_name(item.get("canonical_source", ""))
        item["canonical_target"] = standardize_entity_name(item.get("canonical_target", ""))
        item["canonical_source_type"] = normalize_entity_type(item.get("canonical_source_type", "未知分类"))
        item["canonical_target_type"] = normalize_entity_type(item.get("canonical_target_type", "未知分类"))
        item["canonical_source_species"] = normalize_species_list(item.get("canonical_source_species", []))
        item["canonical_target_species"] = normalize_species_list(item.get("canonical_target_species", []))
        if not item["canonical_source_species"]:
            item["canonical_source_species"] = species_map.get(item["canonical_source"], [])
        if not item["canonical_target_species"]:
            item["canonical_target_species"] = species_map.get(item["canonical_target"], [])
        item["relation"] = (item.get("relation", "") or "").strip()
        stance = (item.get("stance", "") or "support").strip().lower()
        item["stance"] = "contradict" if stance == "contradict" else "support"

        evidence = item.get("evidence", {}) if isinstance(item.get("evidence"), dict) else {}
        item["evidence"] = {
            "context": evidence.get("context", ""),
            "quote": evidence.get("quote", ""),
            "significance": evidence.get("significance", "")
        }

        methods = item.get("methods", [])
        item["methods"] = merge_method_lists([], methods)

        if not item["canonical_source"] or not item["canonical_target"]:
            continue
        if not is_allowed_mechanism_type(item["canonical_source_type"]) or not is_allowed_mechanism_type(item["canonical_target_type"]):
            continue
        cleaned.append(item)

    merged = {}
    for item in cleaned:
        fp = mechanism_fingerprint(item)
        if fp not in merged:
            item["category"] = "mechanism"
            merged[fp] = item
            continue
        merged_item = merged[fp]
        merged_item["methods"] = merge_method_lists(merged_item.get("methods", []), item.get("methods", []))
        if not merged_item.get("mechanism_summary") and item.get("mechanism_summary"):
            merged_item["mechanism_summary"] = item.get("mechanism_summary")
        for field in ["context", "quote", "significance"]:
            if not merged_item.get("evidence", {}).get(field) and item.get("evidence", {}).get(field):
                merged_item["evidence"][field] = item.get("evidence", {}).get(field)

    return {
        "paper_info": paper_info,
        "key_references": key_refs,
        "mechanisms": list(merged.values()),
        "phenotype_points": phenotype_points,
        "bioinfo_points": bioinfo_points,
        "deep_analysis": deep_analysis
    }

# ==========================================
# 6. 可视化、Markdown与Excel导出模块
# ==========================================

def export_to_csv(metadata, output_csv):
    """导出为 Excel 完美兼容的 CSV 格式（带 BOM 的 UTF-8 编码，防止中文乱码）"""
    logging.info("📊 正在生成 Excel 文献信息总表...")
    
    headers = ['文献索引', '系统编号', '原始文件名', '论文原名(Title)', '出版时间(Year)', '期刊名(Journal)', 'DOI号码', '关键词(Keywords)', '机制数量', '表型数量', '生信数量', '知识点总数']
    existing_rows = {}
    if os.path.exists(output_csv):
        try:
            with open(output_csv, 'r', newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                _ = next(reader, None)
                for row in reader:
                    if not row:
                        continue
                    key = row[1] if len(row) > 1 else row[0]
                    if key:
                        existing_rows[key] = row
        except Exception as e:
            logging.warning(f"⚠️ 读取旧表失败，将重建: {e}")

    new_rows = {}
    for file_id in sorted(metadata.keys(), key=doc_id_number):
        info = metadata[file_id]
        if info.get('status') != 'processed':
            continue
        p_info = info.get('paper_info', {})
        new_rows[file_id] = [
            format_doc_index(file_id, metadata),
            file_id,
            info.get('original_name', '未知'),
            p_info.get('title', '未提供'),
            p_info.get('year', '未提供'),
            p_info.get('journal', '未提供'),
            p_info.get('doi', '未提供'),
            p_info.get('keywords', '未提供'),
            info.get('mechanisms_count', info.get('insights_count', 0)),
            info.get('phenotype_count', 0),
            info.get('bioinfo_count', 0),
            total_knowledge_count(info)
        ]

    existing_rows.update(new_rows)
    with open(output_csv, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for file_id in sorted(existing_rows.keys()):
            writer.writerow(existing_rows[file_id])
    logging.info(f"✅ Excel 兼容表格已生成: {os.path.abspath(output_csv)}")

def aggregate_mechanisms(all_data, metadata, category="mechanism"):
    aggregated = {}
    for entry in all_data:
        if not isinstance(entry, dict):
            continue
        if entry.get("category", "mechanism") != category:
            continue
        if "canonical_source" not in entry and "source_name" in entry:
            entry = {
                "canonical_source": standardize_entity_name(entry.get("source_name", "")),
                "canonical_source_type": normalize_entity_type(entry.get("source_type", "未知分类")),
                "canonical_source_species": [],
                "canonical_target": standardize_entity_name(entry.get("target_name", "")),
                "canonical_target_type": normalize_entity_type(entry.get("target_type", "未知分类")),
                "canonical_target_species": [],
                "relation": entry.get("relation", "互作"),
                "stance": "support",
                "mechanism_summary": entry.get("mechanism_context", ""),
                "evidence": {
                    "context": entry.get("mechanism_context", ""),
                    "quote": entry.get("original_quote", ""),
                    "significance": entry.get("significance", "")
                },
                "methods": [],
                "ref": entry.get("ref")
            }

        src = entry.get("canonical_source")
        tgt = entry.get("canonical_target")
        src_type = normalize_entity_type(entry.get("canonical_source_type", "未知分类"))
        tgt_type = normalize_entity_type(entry.get("canonical_target_type", "未知分类"))
        src_species = normalize_species_list(entry.get("canonical_source_species", []))
        tgt_species = normalize_species_list(entry.get("canonical_target_species", []))
        rel = entry.get("relation", "互作")
        stance = entry.get("stance", "support")

        if not src or not tgt:
            continue
        if category == "mechanism":
            if not is_allowed_mechanism_type(src_type) or not is_allowed_mechanism_type(tgt_type):
                continue

        fp = mechanism_fingerprint(entry)
        if fp not in aggregated:
            aggregated[fp] = {
                "canonical_source": src,
                "canonical_source_type": src_type,
                "canonical_source_species": src_species,
                "canonical_target": tgt,
                "canonical_target_type": tgt_type,
                "canonical_target_species": tgt_species,
                "relation": rel,
                "stance": stance,
                "category": category,
                "mechanism_summary": entry.get("mechanism_summary", "") or entry.get("evidence", {}).get("context", ""),
                "evidence": entry.get("evidence", {}),
                "refs": set()
            }
        else:
            aggregated[fp]["canonical_source_species"] = merge_species_lists(
                aggregated[fp].get("canonical_source_species", []),
                src_species
            )
            aggregated[fp]["canonical_target_species"] = merge_species_lists(
                aggregated[fp].get("canonical_target_species", []),
                tgt_species
            )
        ref_id = entry.get("ref")
        if ref_id:
            aggregated[fp]["refs"].add(ref_id)
    return aggregated

def build_network(all_data, metadata, output_html, title_suffix="综合知识网络"):
    if not all_data:
        return
        
    logging.info(f"🕸️ 正在生成{title_suffix}...")
    net = Network(height="900px", width="100%", bgcolor="#1a1a1a", font_color="#f3f3f3", directed=True)
    net.force_atlas_2based(gravity=-80, central_gravity=0.01, spring_length=200, overlap=0.1)

    added_nodes = set()
    added_doc_nodes = set()
    aggregated = {}
    for cat in ["mechanism", "phenotype", "bioinfo"]:
        cat_map = aggregate_mechanisms(all_data, metadata, category=cat)
        for fp, entry in cat_map.items():
            aggregated[f"{cat}||{fp}"] = entry
    node_species = {}
    for entry in aggregated.values():
        src = entry.get("canonical_source")
        tgt = entry.get("canonical_target")
        src_species = entry.get("canonical_source_species", [])
        tgt_species = entry.get("canonical_target_species", [])
        if src:
            node_species[src] = merge_species_lists(node_species.get(src, []), src_species)
        if tgt:
            node_species[tgt] = merge_species_lists(node_species.get(tgt, []), tgt_species)

    # 先添加所有文献节点，保证网络中可见全部文献
    for ref_id in sorted(metadata.keys(), key=doc_id_number):
        doc_label = format_doc_index(ref_id, metadata)
        doc_node_id = f"DOC::{ref_id}"
        if doc_node_id in added_doc_nodes:
            continue
        status = metadata.get(ref_id, {}).get("status", "unknown")
        net.add_node(
            doc_node_id,
            label=doc_label,
            color="#95a5a6",
            title=f"文献节点: {doc_label}\n状态: {status}",
            shape="box",
            size=16
        )
        added_doc_nodes.add(doc_node_id)

    for _, entry in aggregated.items():
        src = entry.get("canonical_source")
        tgt = entry.get("canonical_target")
        src_type = entry.get("canonical_source_type", "未知分类")
        tgt_type = entry.get("canonical_target_type", "未知分类")
        rel = entry.get("relation", "互作")
        stance = entry.get("stance", "support")
        mech = entry.get("mechanism_summary", "无详细描述")
        quote = entry.get("evidence", {}).get("quote", "无原文摘录")

        if not src or not tgt:
            continue
        if is_excluded_name(src) or is_excluded_name(tgt):
            continue
        if not is_network_entity_type(src_type) or not is_network_entity_type(tgt_type):
            continue

        src_color = ENTITY_COLORS.get(src_type, ENTITY_COLORS["未知分类"])
        tgt_color = ENTITY_COLORS.get(tgt_type, ENTITY_COLORS["未知分类"])

        if src not in added_nodes:
            species_text = "；".join(node_species.get(src, [])) or "未提供"
            net.add_node(src, label=src, color=src_color, title=f"类型: {src_type}\n物种: {species_text}", shape="dot", size=25)
            added_nodes.add(src)
        if tgt not in added_nodes:
            species_text = "；".join(node_species.get(tgt, [])) or "未提供"
            net.add_node(tgt, label=tgt, color=tgt_color, title=f"类型: {tgt_type}\n物种: {species_text}", shape="dot", size=25)
            added_nodes.add(tgt)

        # 通过“文献节点”将同一篇文献的多类知识点串联起来
        for ref_id in sorted(entry.get("refs", [])):
            doc_label = format_doc_index(ref_id, metadata)
            doc_node_id = f"DOC::{ref_id}"
            if doc_node_id not in added_doc_nodes:
                net.add_node(
                    doc_node_id,
                    label=doc_label,
                    color="#95a5a6",
                    title=f"文献节点: {doc_label}",
                    shape="box",
                    size=16
                )
                added_doc_nodes.add(doc_node_id)
            # 使用细线将文献节点与实体关联，增强整体连通性
            net.add_edge(doc_node_id, src, label="文献关联", color="#7f8c8d", width=1, dashes=True, arrows="to")
            net.add_edge(doc_node_id, tgt, label="文献关联", color="#7f8c8d", width=1, dashes=True, arrows="to")

        ref_lines = []
        for ref_id in sorted(entry.get("refs", [])):
            ref_lines.append(f"- {format_doc_index(ref_id, metadata)}")
        ref_text = "\n".join(ref_lines) if ref_lines else "无文献记录"

        stance_cn = "相反" if stance == "contradict" else "支持"
        category = entry.get("category", "mechanism")
        if category == "mechanism":
            edge_color = "#2ecc71"
        elif category == "bioinfo":
            edge_color = "#3498db"
        else:
            edge_color = "#f39c12"
        src_species_text = "；".join(entry.get("canonical_source_species", [])) or "未提供"
        tgt_species_text = "；".join(entry.get("canonical_target_species", [])) or "未提供"
        hover_text = (
            f"⚡【关系】: {src} --[{rel}]--> {tgt} ({stance_cn})\n"
            f"🧬【机制摘要】: {mech}\n"
            f"🧫【上游物种】: {src_species_text}\n"
            f"🧫【下游物种】: {tgt_species_text}\n"
            f"----------------------------------------\n"
            f"📖【英文原文】: \"{quote}\"\n"
            f"📝【支持文献】:\n{ref_text}\n"
        )
        edge_label = f"{rel}·{stance_cn}" if category == "mechanism" else rel
        net.add_edge(src, tgt, label=edge_label, title=hover_text, color=edge_color, arrows="to")

    net.show_buttons(filter_=['physics'])
    net.save_graph(output_html)

def export_to_markdown(all_data, metadata, output_md):
    if not all_data: return
    logging.info("📝 正在生成 Markdown 知识库报告...")
    
    doc_grouped = {}
    for item in all_data:
        ref_id = item.get('ref')
        if ref_id not in doc_grouped: doc_grouped[ref_id] = []
        doc_grouped[ref_id].append(item)
    
    aggregated_mech = aggregate_mechanisms(all_data, metadata, category="mechanism")
    aggregated_pheno = aggregate_mechanisms(all_data, metadata, category="phenotype")
    aggregated_bio = aggregate_mechanisms(all_data, metadata, category="bioinfo")
        
    with open(output_md, "w", encoding="utf-8") as f:
        f.write("# 📚 植物病理学与分子机制 RAG 知识库\n\n")
        f.write("## 🔗 全局机制功能索引\n\n")

        for idx, entry in enumerate(aggregated_mech.values(), 1):
            src = entry.get("canonical_source", "")
            tgt = entry.get("canonical_target", "")
            rel = entry.get("relation", "")
            stance = entry.get("stance", "support")
            stance_cn = "相反" if stance == "contradict" else "支持"
            summary = entry.get("mechanism_summary", "无摘要")
            refs = entry.get("refs", [])
            ref_names = []
            for ref_id in sorted(refs):
                ref_names.append(format_doc_index(ref_id, metadata))
            ref_text = "；".join(ref_names) if ref_names else "无文献记录"

            f.write(f"**{idx}. 机制摘要**: {summary}\n")
            f.write(f"**分子关系**: `{src}` --[{rel}]--> `{tgt}`\n")
            f.write(f"**立场**: {stance_cn}\n")
            f.write(f"**支持文献**: {ref_text}\n\n")

        f.write("---\n\n")

        f.write("## 🧪 全局表型/宏观结果索引\n\n")
        for idx, entry in enumerate(aggregated_pheno.values(), 1):
            src = entry.get("canonical_source", "")
            tgt = entry.get("canonical_target", "")
            rel = entry.get("relation", "")
            summary = entry.get("evidence", {}).get("context", "无摘要")
            refs = entry.get("refs", [])
            ref_names = [format_doc_index(ref_id, metadata) for ref_id in sorted(refs)]
            ref_text = "；".join(ref_names) if ref_names else "无文献记录"
            f.write(f"**{idx}. 结果摘要**: {summary}\n")
            f.write(f"**关系**: `{src}` --[{rel}]--> `{tgt}`\n")
            f.write(f"**支持文献**: {ref_text}\n\n")

        f.write("---\n\n")

        f.write("## 💻 全局生物信息学索引\n\n")
        for idx, entry in enumerate(aggregated_bio.values(), 1):
            src = entry.get("canonical_source", "")
            tgt = entry.get("canonical_target", "")
            rel = entry.get("relation", "")
            summary = entry.get("evidence", {}).get("context", "无摘要")
            refs = entry.get("refs", [])
            ref_names = [format_doc_index(ref_id, metadata) for ref_id in sorted(refs)]
            ref_text = "；".join(ref_names) if ref_names else "无文献记录"
            f.write(f"**{idx}. 结论摘要**: {summary}\n")
            f.write(f"**关系**: `{src}` --[{rel}]--> `{tgt}`\n")
            f.write(f"**支持文献**: {ref_text}\n\n")

        f.write("---\n\n")
        
        for file_id in sorted(metadata.keys(), key=doc_id_number):
            info = metadata[file_id]
            if info.get('status') != 'processed': continue
            
            p_info = info.get('paper_info', {})
            title = p_info.get('title', '未知论文名')
            journal = p_info.get('journal', '未知期刊')
            year = p_info.get('year', '未知年份')
            doi = p_info.get('doi', '未知DOI')
            
            f.write(f"## 📄 {title}\n")
            f.write(f"**文献索引**: {format_doc_index(file_id, metadata)}\n\n")
            f.write(f"**原始文件**: `{info.get('original_name')}`\n\n")
            f.write(f"**期刊**: {journal} | **年份**: {year} | **DOI**: [{doi}](https://doi.org/{doi})\n\n")

            deep_analysis = info.get("deep_analysis", {})
            if isinstance(deep_analysis, dict) and deep_analysis:
                f.write("### 🧭 主要结果深度解析\n")
                f.write(f"**核心结果解析**: {deep_analysis.get('main_results', '未提供')}\n\n")
                f.write(f"**作者为什么这么做**: {deep_analysis.get('why', '未提供')}\n\n")
                f.write(f"**意义**: {deep_analysis.get('significance', '未提供')}\n\n")
                f.write("---\n\n")
            
            key_refs = info.get('key_references', [])
            if key_refs:
                f.write("### 📌 核心参考文献提取\n")
                for r in key_refs: f.write(f"- {r}\n")
                f.write("\n")
                
            mechanisms = [x for x in doc_grouped.get(file_id, []) if x.get("category", "mechanism") == "mechanism"]
            f.write("### 🔬 解析出的分子机制汇总\n")
            if mechanisms:
                for idx, item in enumerate(mechanisms, 1):
                    src = item.get("canonical_source")
                    tgt = item.get("canonical_target")
                    rel = item.get("relation")
                    stance = item.get("stance", "support")
                    stance_cn = "相反" if stance == "contradict" else "支持"
                    summary = item.get("mechanism_summary", "无摘要")
                    evidence = item.get("evidence", {})
                    methods = item.get("methods", [])
                    methods_text = "；".join([f"{m.get('method','')}：{m.get('result','')}" for m in methods if isinstance(m, dict) and (m.get("method") or m.get("result"))])
                    if not methods_text:
                        methods_text = "未提供"

                    f.write(f"**{idx}. 机制摘要**: {summary}\n")
                    f.write(f"**分子关系**: `{src}` --[{rel}]--> `{tgt}` | **立场**: {stance_cn}\n")
                    f.write(f"**机制脉络**: {evidence.get('context', '未提供')}\n")
                    f.write(f"**生物学意义**: {evidence.get('significance', '未提供')}\n")
                    f.write(f"**英文原文查证**: *\"{evidence.get('quote', '未提供')}\"*\n")
                    f.write(f"**方法与结果**: {methods_text}\n\n")
            else:
                f.write("未发现该类知识点。\n\n")
            f.write("---\n\n")

            phenotype_points = [x for x in doc_grouped.get(file_id, []) if x.get("category") == "phenotype"]
            f.write("### 🧪 表型与宏观结果汇总\n")
            if phenotype_points:
                for idx, item in enumerate(phenotype_points, 1):
                    src = item.get("canonical_source")
                    tgt = item.get("canonical_target")
                    rel = item.get("relation")
                    evidence = item.get("evidence", {})
                    methods = item.get("methods", [])
                    methods_text = "；".join([f"{m.get('method','')}：{m.get('result','')}" for m in methods if isinstance(m, dict) and (m.get("method") or m.get("result"))])
                    if not methods_text:
                        methods_text = "未提供"
                    f.write(f"**{idx}. 结果摘要**: {evidence.get('context', '未提供')}\n")
                    f.write(f"**关系**: `{src}` --[{rel}]--> `{tgt}`\n")
                    f.write(f"**方法与结果**: {methods_text}\n\n")
            else:
                f.write("未发现该类知识点。\n\n")
            f.write("---\n\n")

            bioinfo_points = [x for x in doc_grouped.get(file_id, []) if x.get("category") == "bioinfo"]
            f.write("### 💻 生物信息学结论汇总\n")
            if bioinfo_points:
                for idx, item in enumerate(bioinfo_points, 1):
                    src = item.get("canonical_source")
                    tgt = item.get("canonical_target")
                    rel = item.get("relation")
                    evidence = item.get("evidence", {})
                    methods = item.get("methods", [])
                    methods_text = "；".join([f"{m.get('method','')}：{m.get('result','')}" for m in methods if isinstance(m, dict) and (m.get("method") or m.get("result"))])
                    if not methods_text:
                        methods_text = "未提供"
                    f.write(f"**{idx}. 结论摘要**: {evidence.get('context', '未提供')}\n")
                    f.write(f"**关系**: `{src}` --[{rel}]--> `{tgt}`\n")
                    f.write(f"**方法与结果**: {methods_text}\n\n")
            else:
                f.write("未发现该类知识点。\n\n")
            f.write("---\n\n")

# ==========================================
# 7. 主程序引擎
# ==========================================

def main():
    parser = argparse.ArgumentParser(
        description="📚 云端 RAG：文献知识点抽取与网络构建工具",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "示例用法：\n"
            "  python3 ai_studio_code.py -i ./ --mode extract\n"
            "  python3 ai_studio_code.py -i ./ --mode both\n"
            "  python3 ai_studio_code.py -i ./ --mode extract --prompt-system-file ./prompt_system.txt --read-mode deep\n"
            "  python3 ai_studio_code.py -i ./ --mode extract --api-key sk-or-v1-xxxxxx\n"
            "  python3 ai_studio_code.py -i ./ --mode extract --platform siliconflow --model THUDM/glm-4-9b-chat\n\n"
            "说明：\n"
            "  - 必须提供 --prompt-system-file（支持 [DEEP]/[QUICK] 区块）。\n"
            "  - 你可以用 --api-key 直接传入 Key；不提供时会提示输入，留空将读取环境变量 MODEL_API_KEY。\n"
            "  - 需要限速时再设置 --rpm（默认 0=不限速）。\n"
            "  - 模型需支持 chat/completions 并能稳定输出 JSON，否则预检会失败。\n"
            "  - quick 模式每篇文献只调用 1 次模型；deep 模式固定 2 次（不含可选修复）。\n"
        )
    )
    parser.add_argument("-i", "--input", default="./pdf_papers", help="指定存放 PDF 论文的文件夹路径（可选）")
    parser.add_argument("--platform", choices=["openrouter", "siliconflow"], default="openrouter", help="模型平台：openrouter 或 siliconflow（可选）")
    parser.add_argument("--model", default="", help="平台内具体模型名称（可选，不填则使用默认）")
    parser.add_argument("--api-key", default="", help="模型平台 API Key（可选，不填将提示输入）")
    parser.add_argument("--rpm", type=int, default=0, help="每分钟请求上限（可选，0=不限制，建议 10-20）")
    parser.add_argument("--refresh", action="store_true", help="对已处理文献进行查漏补缺阅读（可选）")
    parser.add_argument("--no-refresh", action="store_true", help="只处理新增文献，不补全旧文献（可选）")
    parser.add_argument("--mode", choices=["extract", "network", "both"], default="extract",
                        help="运行模式：extract=只提取知识点并更新RAG/列表；network=仅重绘网络；both=提取后重绘网络（可选）")
    parser.add_argument("--read-mode", choices=["deep", "quick"], default="deep", help="阅读模式：deep=深度阅读；quick=快速阅读（可选）")
    parser.add_argument("--prompt-system-file", required=True, help="提示词系统文件路径（必填）")
    parser.add_argument("--allow-repair", action="store_true", help="允许 JSON 修复（会增加额外模型调用，可选）")
    args = parser.parse_args()
    
    work_dir = os.path.abspath(args.input)
    os.makedirs(work_dir, exist_ok=True)

    METADATA_FILE = os.path.join(work_dir, "paper_metadata.json") 
    OUTPUT_JSON = os.path.join(work_dir, "pathology_kb.json")
    OUTPUT_HTML = os.path.join(work_dir, "plant_pathology_network.html")
    OUTPUT_MD = os.path.join(work_dir, "pathology_report.md")
    OUTPUT_CSV = os.path.join(work_dir, "paper_summary_table.csv") # 新增 CSV 表格输出
    LOG_FILE = os.path.join(work_dir, "pathology_rag.log")
    LOG_FILE_RUN = os.path.join(work_dir, f"pathology_rag_{time.strftime('%Y%m%d_%H%M%S')}.log")
    global DEBUG_DIR
    DEBUG_DIR = os.path.join(work_dir, "debug_model_outputs")

    for handler in logging.root.handlers[:]: logging.root.removeHandler(handler)
        
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.FileHandler(LOG_FILE_RUN, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    logging.info("="*65)
    logging.info(f"🚀 启动云端 RAG 3.0 (七彩节点+重点文献+Excel信息表) | 目录: {work_dir}")
    logging.info("="*65)

    global PROMPT_SYSTEM_TEXT
    PROMPT_SYSTEM_TEXT = load_prompt_system(args.prompt_system_file, args.read_mode)
    if args.mode in {"extract", "both"}:
        if not PROMPT_SYSTEM_TEXT:
            logging.error("❌ 提示词系统为空，无法继续抽取。")
            return
        global ALLOW_JSON_REPAIR
        ALLOW_JSON_REPAIR = bool(args.allow_repair)
        if ALLOW_JSON_REPAIR:
            logging.warning("⚠️ 已开启 JSON 修复：每篇文献可能多一次模型调用。")
        global MODEL_NAME, MODEL_PLATFORM
        MODEL_PLATFORM, MODEL_NAME = resolve_platform_model(args)
        api_key = resolve_api_key(args, MODEL_PLATFORM)
        if not api_key:
            logging.error("❌ 未提供模型平台 API Key，已退出。")
            return
        init_client(api_key, MODEL_PLATFORM)
        global RATE_LIMIT_RPM
        RATE_LIMIT_RPM = max(0, int(args.rpm))
        if RATE_LIMIT_RPM > 0:
            logging.info(f"⏱️ 启用请求限速: {RATE_LIMIT_RPM} RPM")
        if not preflight_model_check():
            logging.error("❌ 模型预检未通过，已退出以避免批量失败。")
            return

    all_knowledge = load_json(OUTPUT_JSON, [])
    all_knowledge, migrated = migrate_old_knowledge(all_knowledge, OUTPUT_JSON)
    if migrated:
        save_json(all_knowledge, OUTPUT_JSON)
    counts_by_ref = compute_counts_by_ref(all_knowledge)

    metadata = load_json(METADATA_FILE, {})
    for info in metadata.values():
        if "mechanisms_count" not in info and "insights_count" in info:
            info["mechanisms_count"] = info.get("insights_count", 0)
        if "phenotype_count" not in info:
            info["phenotype_count"] = 0
        if "bioinfo_count" not in info:
            info["bioinfo_count"] = 0
        if "mechanism_status" not in info:
            info["mechanism_status"] = "unknown"
        if "phenotype_status" not in info:
            info["phenotype_status"] = "unknown"
        if "bioinfo_status" not in info:
            info["bioinfo_status"] = "unknown"
    for fid, counts in counts_by_ref.items():
        if fid in metadata:
            metadata[fid]["mechanisms_count"] = counts.get("mechanism", 0)
            metadata[fid]["phenotype_count"] = counts.get("phenotype", 0)
            metadata[fid]["bioinfo_count"] = counts.get("bioinfo", 0)
            metadata[fid].update(category_status_from_counts(metadata[fid], zero_status=metadata[fid].get("mechanism_status", "unknown")))

    if args.mode in {"extract", "both"}:
        metadata, renamed_count = manage_and_rename_files(work_dir, metadata)
        if renamed_count > 0:
            save_json(metadata, METADATA_FILE)

    has_prior_outputs = any(os.path.exists(p) for p in [
        OUTPUT_JSON, OUTPUT_MD, OUTPUT_CSV, OUTPUT_HTML
    ])

    do_refresh = False
    if args.mode in {"extract", "both"}:
        if args.refresh and not args.no_refresh:
            do_refresh = True
        elif args.no_refresh and not args.refresh:
            do_refresh = False
        else:
            if has_prior_outputs:
                choice = input("检测到已有知识库/网络/列表，是否查漏补缺阅读？(y/n，默认n): ").strip().lower()
                do_refresh = choice in {"y", "yes"}
            else:
                logging.info("🆕 未检测到已有知识库/网络/列表，进入首次深入阅读模式。")
                do_refresh = True

    files_to_process = []
    refresh_files = []
    refresh_focus = {}
    if args.mode in {"extract", "both"}:
        for fid in sorted(metadata.keys(), key=doc_id_number):
            info = metadata[fid]
            if info.get("status") == "pending":
                files_to_process.append(fid)
                continue
            if do_refresh and info.get("status") == "processed":
                missing = missing_categories(info)
                if missing:
                    refresh_files.append(fid)
                    refresh_focus[fid] = missing
                    logging.info(f"🧩 {fid} 缺失类别: {','.join(missing)}")
        files_to_process.extend(refresh_files)

    if args.mode == "network":
        build_network(all_knowledge, metadata, OUTPUT_HTML, title_suffix="综合知识网络")
        return

    if not files_to_process and args.mode in {"extract", "both"}:
        logging.info("☕ 当前无待处理或待补全的文献。更新RAG与列表...")
        export_to_markdown(all_knowledge, metadata, OUTPUT_MD)
        export_to_csv(metadata, OUTPUT_CSV)
        if args.mode == "both":
            build_network(all_knowledge, metadata, OUTPUT_HTML, title_suffix="综合知识网络")
        return

    logging.info(f"✨ 发现 {len(files_to_process)} 篇文献待处理/补全...")

    total_files = len(files_to_process)
    for idx, file_id in enumerate(files_to_process, 1):
        log_doc_progress(idx, total_files, file_id)
        refresh_mode = file_id in refresh_files
        focus_categories = refresh_focus.get(file_id, [])
        if refresh_mode:
            logging.info(f"🔁 {file_id} 进入补全模式，重点补充缺失类别: {','.join(focus_categories)}")

        target_path = os.path.join(work_dir, file_id)
        text = extract_text_hybrid(target_path, max_pages=6 if refresh_mode else 12)
        
        result = get_expert_insights(text, file_id, read_mode=args.read_mode, refresh_mode=refresh_mode, focus_categories=focus_categories)
        
        if result:
            mechanisms = result.get("mechanisms", [])
            phenotype_points = result.get("phenotype_points", [])
            bioinfo_points = result.get("bioinfo_points", [])
            deep_analysis = result.get("deep_analysis", {})
            all_knowledge.extend(mechanisms + phenotype_points + bioinfo_points)
            metadata[file_id]["status"] = "processed"
            counts = count_by_ref_for_doc(all_knowledge, file_id)
            metadata[file_id]["mechanisms_count"] = counts.get("mechanism", 0)
            metadata[file_id]["phenotype_count"] = counts.get("phenotype", 0)
            metadata[file_id]["bioinfo_count"] = counts.get("bioinfo", 0)
            zero_status = "none" if refresh_mode else "unknown"
            metadata[file_id].update(category_status_from_counts(metadata[file_id], zero_status=zero_status))
            metadata[file_id]["key_references"] = result.get("key_references", [])
            metadata[file_id]["paper_info"] = result.get("paper_info", {}) # 保存文献元数据
            if deep_analysis:
                metadata[file_id]["deep_analysis"] = deep_analysis
            
            if mechanisms or phenotype_points or bioinfo_points:
                logging.info(
                    f"💾 {file_id} 提取成功: 机制{len(mechanisms)}条, 表型{len(phenotype_points)}条, 生信{len(bioinfo_points)}条"
                )
            else:
                logging.warning(f"⚠️ {file_id} 未提取到知识点，但已更新文献信息。")
                if total_knowledge_count(metadata[file_id]) == 0:
                    no_data_dir = os.path.join(work_dir, "no_knowledge_pdfs")
                    try:
                        os.makedirs(no_data_dir, exist_ok=True)
                        shutil.copy2(target_path, os.path.join(no_data_dir, os.path.basename(target_path)))
                        logging.warning(f"📦 已将无知识点文献复制至: {no_data_dir}")
                    except Exception as e:
                        logging.warning(f"⚠️ 复制无知识点文献失败: {e}")

            logging.info(
                f"✅ {file_id} 机制功能知识点提取完毕: {metadata[file_id]['mechanisms_count']}条 (状态:{metadata[file_id].get('mechanism_status')})"
            )
            logging.info(
                f"✅ {file_id} 宏观表型实验知识点提取完毕: {metadata[file_id]['phenotype_count']}条 (状态:{metadata[file_id].get('phenotype_status')})"
            )
            logging.info(
                f"✅ {file_id} 生信知识点提取完毕: {metadata[file_id]['bioinfo_count']}条 (状态:{metadata[file_id].get('bioinfo_status')})"
            )
        else:
            metadata[file_id]["status"] = "failed_or_empty"
            logging.warning(f"⚠️ {file_id} 提取失败或无内容，已跳过。")
            
        save_json(all_knowledge, OUTPUT_JSON)
        save_json(metadata, METADATA_FILE)

    # 最终输出
    export_to_markdown(all_knowledge, metadata, OUTPUT_MD)
    export_to_csv(metadata, OUTPUT_CSV)
    if args.mode == "both":
        build_network(all_knowledge, metadata, OUTPUT_HTML, title_suffix="综合知识网络")
    logging.info("--- 🎉 本次批处理执行完毕 ---")

if __name__ == "__main__":
    main()
