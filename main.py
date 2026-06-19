import asyncio
import os
import re
import uuid
import threading
import sqlite3
import subprocess
import tempfile
import edge_tts
from edge_tts.communicate import split_text_by_byte_length
import random
import datetime
import json
import time
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn
from typing import List, Optional

# ===================== 核心配置 =====================
DEFAULT_VOICE = "zh-CN-XiaoyiNeural"
DEFAULT_RATE = "+20%"
MAX_CHARS = 4000
BATCH_SIZE = 30
BATCH_SLEEP_MIN = 12
BATCH_SLEEP_MAX = 20

VOICE_LIST = [
    ("zh-CN-XiaoxiaoNeural", "晓晓 女声·温暖", "🎀"),
    ("zh-CN-XiaoyiNeural", "晓艺 女声·活泼", "🌸"),
    ("zh-CN-YunyangNeural", "云扬 男声·新闻", "📰"),
    ("zh-CN-YunxiNeural", "云希 男声·阳光", "☀️"),
    ("zh-CN-YunjianNeural", "云健 男声·激情", "🔥"),
    ("zh-CN-YunxiaNeural", "云夏 女声·可爱", "🎀"),
]

app = FastAPI(title="📖 TTS 有声书工坊")

# ===================== 全局状态 =====================
log_list: List[str] = []
task_status_msg = "等待任务"
task_run_state = "idle"  # idle / running / done / error / cancelled
current_task_name = ""
current_root_dir = ""
current_total_seg = 0
current_progress = 0  # 0-100
final_mp3_path = ""
cancel_flag = False
task_history = []  # 存储历史记录 {name, time, state, file_path, voice, rate}
history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_history.json")

HISTORY_MAX = 50


def load_history():
    global task_history
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                task_history = json.load(f)
        except:
            task_history = []


def save_history():
    global task_history
    task_history = task_history[:HISTORY_MAX]
    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(task_history, f, ensure_ascii=False, indent=2)
    except:
        pass


def add_to_history(name, state, file_path, voice, rate):
    load_history()
    entry = {
        "name": name,
        "time": datetime.datetime.now().strftime("%m-%d %H:%M"),
        "state": state,
        "file_path": file_path,
        "voice": voice,
        "rate": rate,
    }
    task_history.insert(0, entry)
    save_history()


def add_log(msg: str):
    global log_list
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log_list.append(f"[{ts}] {msg}")
    if len(log_list) > 500:
        log_list = log_list[-300:]


# ===================== 章节检测 =====================
# 支持常见章节格式
CHAPTER_PATTERNS = [
    r'^第[一二三四五六七八九十百千万\d]+[章节部篇回卷集]',
    r'^Chapter\s+\d+',
    r'^CHAPTER\s+\d+',
    r'^第\d+[章节]',
    r'^\d+\.\s+',  # "1. 标题"
]

def detect_chapters(text: str) -> list:
    """返回 [(chapter_title, start_pos, end_pos), ...]"""
    lines = text.split('\n')
    chapters = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in CHAPTER_PATTERNS:
            if re.match(pattern, stripped):
                chapters.append((stripped, i))
                break
    
    if not chapters:
        # 如果没有检测到章节，按段落分
        return [("全文", 0, len(lines))]
    
    # 计算每个章节的行范围
    chapter_ranges = []
    for idx, (title, line_num) in enumerate(chapters):
        start_line = line_num
        if idx + 1 < len(chapters):
            end_line = chapters[idx + 1][1]
        else:
            end_line = len(lines)
        chapter_ranges.append((title, start_line, end_line))
    
    return chapter_ranges


def get_chapter_text(text: str, chapter_info: tuple) -> str:
    """根据章节信息提取文本"""
    title, start, end = chapter_info
    lines = text.split('\n')
    return '\n'.join(lines[start:end])


# ===================== 语义分割 =====================
def split_semantic(text: str) -> list:
    sentences = re.split(r'(?<=[。！？\n])', text)
    chunks = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(current) + len(sent) <= MAX_CHARS:
            current += sent
        else:
            if current:
                chunks.append(current)
            if len(sent) > MAX_CHARS:
                sub_chunks = list(split_text_by_byte_length(sent.encode("utf-8"), MAX_CHARS))
                chunks.extend([c.decode("utf-8") for c in sub_chunks])
            else:
                current = sent
    if current:
        chunks.append(current)
    return chunks


# ===================== 单段生成 =====================
async def gen_one(text: str, out_path: str, idx: int, total: int, voice: str, rate: str):
    global task_status_msg, cancel_flag
    if cancel_flag:
        return
    info = f"[生成中] 第 {idx + 1}/{total} 段 → {os.path.basename(out_path)}"
    task_status_msg = info
    add_log(info)
    try:
        tts = edge_tts.Communicate(text, voice, rate=rate)
        await tts.save(out_path)
        ok_msg = f"[完成] 第 {idx + 1}/{total} 段"
        task_status_msg = ok_msg
        add_log(ok_msg)
    except Exception as e:
        err_msg = f"[错误] 第 {idx + 1}/{total} 段生成失败：{str(e)}"
        task_status_msg = err_msg
        add_log(err_msg)
        raise e


# ===================== 断点检测 =====================
def get_finished_parts(out_dir: str, total: int) -> set:
    finished = set()
    for i in range(total):
        part_file = os.path.join(out_dir, f"part_{i:03d}.mp3")
        if os.path.exists(part_file):
            finished.add(i)
    return finished


# ===================== ffmpeg合并 =====================
def merge_audio(out_dir: str, total_parts: int, final_path: str):
    add_log("🔧 开始合并所有音频片段...")
    list_path = os.path.join(out_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for i in range(total_parts):
            f.write(f"file 'part_{i:03d}.mp3'\n")

    cmd = f'ffmpeg -f concat -safe 0 -i "{list_path}" -c copy "{final_path}" -y'
    ret = os.system(cmd)
    if ret == 0:
        add_log(f"✅ 合并成功！最终文件：{os.path.basename(final_path)}")
        return True
    else:
        add_log("❌ 音频合并失败，请检查 ffmpeg 是否正常安装")
        return False


# ===================== 后台任务 =====================
async def main_task(full_text: str, root_dir: str, task_name: str, voice: str, rate: str):
    global task_status_msg, task_run_state, current_task_name
    global current_root_dir, current_total_seg, final_mp3_path, current_progress, cancel_flag

    current_task_name = task_name
    current_root_dir = root_dir
    task_run_state = "running"
    cancel_flag = False
    current_progress = 0
    log_list.clear()
    final_mp3_path = ""

    task_dir = os.path.join(root_dir, task_name)
    os.makedirs(task_dir, exist_ok=True)
    add_log(f"📂 任务目录：{task_dir}")

    chunks = split_semantic(full_text)
    total = len(chunks)
    current_total_seg = total
    add_log(f"✅ 文本切分完成，总共 {total} 段")

    finished_idx = get_finished_parts(task_dir, total)
    add_log(f"🔍 断点检测：已完成 {len(finished_idx)} 个分片")

    try:
        for batch_start in range(0, total, BATCH_SIZE):
            if cancel_flag:
                add_log("⏹ 任务已被用户取消")
                task_status_msg = "⏹ 任务已取消"
                task_run_state = "cancelled"
                return

            batch_end = min(batch_start + BATCH_SIZE, total)
            batch_tasks = []

            for idx in range(batch_start, batch_end):
                if cancel_flag:
                    break
                if idx in finished_idx:
                    continue
                chunk_text = chunks[idx]
                part_path = os.path.join(task_dir, f"part_{idx:03d}.mp3")
                batch_tasks.append(gen_one(chunk_text, part_path, idx, total, voice, rate))

            if batch_tasks:
                await asyncio.gather(*batch_tasks)

            # 更新进度
            current_progress = min(int((batch_end / total) * 100), 99)

            is_last_batch = (batch_end >= total)
            if not is_last_batch and not cancel_flag:
                sleep_t = random.randint(BATCH_SLEEP_MIN, BATCH_SLEEP_MAX)
                add_log(f"⌛ 本批次完成，等待 {sleep_t}s")
                await asyncio.sleep(sleep_t)
            else:
                add_log("✅ 所有分段音频全部生成完毕，开始合并")

        if cancel_flag:
            add_log("⏹ 任务已被用户取消")
            task_status_msg = "⏹ 任务已取消"
            task_run_state = "cancelled"
            return

        final_mp3 = os.path.join(task_dir, f"{task_name}_完整音频.mp3")
        merge_ok = merge_audio(task_dir, total, final_mp3)
        if not merge_ok:
            raise Exception("音频合并失败")

        final_mp3_path = final_mp3
        current_progress = 100
        task_status_msg = f"🎉 任务【{task_name}】全部执行完成！"
        add_log(task_status_msg)
        task_run_state = "done"
        add_to_history(task_name, "完成", final_mp3, voice, rate)

    except Exception as e:
        err_all = f"💥 程序异常终止：{str(e)}"
        task_status_msg = err_all
        add_log(err_all)
        task_run_state = "error"
        add_to_history(task_name, "失败", "", voice, rate)


# ===================== 获取服务器TXT文件列表 =====================
def scan_txt_files(base_dir: str) -> list:
    """扫描输出目录及子目录下的TXT文件"""
    txt_files = []
    if not os.path.exists(base_dir):
        return txt_files
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.lower().endswith('.txt'):
                rel_path = os.path.relpath(os.path.join(root, f), base_dir)
                txt_files.append({
                    "path": rel_path,
                    "name": f,
                    "size": os.path.getsize(os.path.join(root, f))
                })
    return txt_files


# ===================== 在线预览 =====================
@app.get("/api/preview_voice")
async def preview_voice(voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+20%", text: str = "你好，欢迎使用有声书工坊。"):
    """生成语音预览片段"""
    try:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp_path = tmp.name
        tmp.close()
        tts = edge_tts.Communicate(text[:200], voice, rate=rate)
        await tts.save(tmp_path)
        return FileResponse(tmp_path, media_type="audio/mpeg", filename="preview.mp3",
                            headers={"Content-Disposition": "inline"})
    except Exception as e:
        return JSONResponse({"code": 1, "msg": str(e)})


# ===================== API =====================
@app.get("/download_full")
async def download_full():
    if task_run_state != "done" or not final_mp3_path or not os.path.exists(final_mp3_path):
        return {"code": 1, "msg": "暂无可下载文件"}
    return FileResponse(
        path=final_mp3_path,
        filename=os.path.basename(final_mp3_path),
        media_type="audio/mpeg"
    )


@app.get("/api/get_runtime")
async def get_runtime():
    has_file = (task_run_state == "done" and final_mp3_path and os.path.exists(final_mp3_path))
    return {
        "state": task_run_state,
        "status": task_status_msg,
        "log": log_list,
        "has_file": has_file,
        "progress": current_progress,
        "total_seg": current_total_seg,
        "task_name": current_task_name,
    }


@app.get("/api/history")
async def get_history():
    load_history()
    return {"history": task_history}


@app.get("/api/scan_txt")
async def api_scan_txt(base_dir: str = "./output"):
    files = scan_txt_files(base_dir)
    return {"files": files}


@app.get("/api/read_txt")
async def api_read_txt(file_path: str):
    """读取服务器上的TXT文件内容"""
    full_path = os.path.join("./output", file_path) if not os.path.isabs(file_path) else file_path
    if not os.path.exists(full_path):
        return {"code": 1, "msg": "文件不存在"}
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read(50000)  # 最多读50KB预览
        return {"code": 0, "content": content, "name": os.path.basename(file_path)}
    except Exception as e:
        return {"code": 1, "msg": str(e)}


@app.get("/api/detect_chapters")
async def api_detect_chapters(file_path: str = "", text: str = ""):
    """检测章节结构"""
    content = text
    if file_path and not content:
        full_path = os.path.join("./output", file_path) if not os.path.isabs(file_path) else file_path
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read(50000)

    chapters = detect_chapters(content) if content else []
    return {
        "chapters": [{"title": c[0], "start": c[1], "end": c[2]} for c in chapters]
    }


@app.post("/api/cancel")
async def cancel_task():
    global cancel_flag, task_run_state, task_status_msg
    if task_run_state == "running":
        cancel_flag = True
        task_status_msg = "⏹ 正在取消任务..."
        add_log("⏹ 用户请求取消任务")
        return {"code": 0, "msg": "取消请求已发送"}
    return {"code": 1, "msg": "当前没有运行中的任务"}


@app.post("/api/start")
async def start(
    bg_task: BackgroundTasks,
    txtFile: UploadFile = File(None),
    text: str = Form(""),
    save_dir: str = Form("./output"),
    task_name_input: str = Form(""),
    voice: str = Form(DEFAULT_VOICE),
    rate: str = Form(DEFAULT_RATE)
):
    if task_run_state == "running":
        return {"code": 1, "msg": "当前有任务正在运行，请稍后"}

    full_text = ""
    file_name = ""
    if txtFile and txtFile.filename.endswith(".txt"):
        file_name = os.path.splitext(txtFile.filename)[0]
        try:
            content = await txtFile.read()
            full_text = content.decode("utf-8", errors="ignore")
        except:
            return {"code": 1, "msg": "读取TXT失败，请使用UTF-8编码"}
    elif text.strip():
        full_text = text.strip()
    else:
        return {"code": 1, "msg": "请上传TXT文件或粘贴文本内容"}

    if not full_text.strip():
        return {"code": 1, "msg": "文本内容不能为空"}

    task_name = ""
    if task_name_input.strip():
        task_name = task_name_input.strip()
    elif file_name:
        task_name = file_name
    else:
        task_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    illegal = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    for c in illegal:
        task_name = task_name.replace(c, "_")

    bg_task.add_task(main_task, full_text, save_dir, task_name, voice, rate)
    return {"code": 0, "msg": f"任务【{task_name}】已启动"}


# ===================== Calibre 书库集成 =====================
CALIBRE_DB_PATH = os.environ.get("CALIBRE_DB", "./metadata.db")
CALIBRE_LIBRARY = os.environ.get("CALIBRE_LIBRARY", "")

EXTRACTABLE_FORMATS = {"EPUB", "MOBI", "AZW3", "TXT", "AZW", "DOCX", "RTF", "HTML", "HTM", "XHTML"}

def get_calibre_conn():
    """只读连接 Calibre 数据库"""
    if not os.path.exists(CALIBRE_DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{CALIBRE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn

def get_book_file_path(book_path: str, filename: str, fmt: str) -> str:
    """定位书籍文件的完整路径"""
    # 优先从 CALIBRE_LIBRARY 找
    if CALIBRE_LIBRARY:
        candidates = [
            os.path.join(CALIBRE_LIBRARY, book_path, f"{filename}.{fmt.lower()}"),
            os.path.join(CALIBRE_LIBRARY, book_path, f"{filename}.{fmt.upper()}"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
    # 从工作目录找 metadata.db 同目录
    db_dir = os.path.dirname(os.path.abspath(CALIBRE_DB_PATH))
    candidates = [
        os.path.join(db_dir, book_path, f"{filename}.{fmt.lower()}"),
        os.path.join(db_dir, book_path, f"{filename}.{fmt.upper()}"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


def extract_text_from_file(file_path: str, fmt: str) -> dict:
    """从书籍文件提取文本，返回 {success, text, error, format_type}"""
    if not os.path.exists(file_path):
        return {"success": False, "text": "", "error": "文件不存在", "format_type": "missing"}
    
    fmt = fmt.upper()
    text = ""
    
    # TXT - 直接读取
    if fmt == "TXT":
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            if len(text.strip()) < 100:
                return {"success": False, "text": text, "error": "文本内容过少，可能为扫描版", "format_type": fmt}
            return {"success": True, "text": text, "error": "", "format_type": fmt}
        except Exception as e:
            return {"success": False, "text": "", "error": str(e), "format_type": fmt}
    
    # EPUB - ebooklib
    if fmt == "EPUB":
        try:
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup
            book = epub.read_epub(file_path)
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), "html.parser")
                    text += soup.get_text(separator="\n") + "\n"
            if len(text.strip()) < 100:
                return {"success": False, "text": text, "error": "EPUB 提取文本过少", "format_type": fmt}
            return {"success": True, "text": text, "error": "", "format_type": fmt}
        except ImportError:
            pass  # 降级到 ebook-convert
        except Exception as e:
            return {"success": False, "text": "", "error": str(e), "format_type": fmt}
    
    # PDF - pypdf
    if fmt == "PDF":
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            for page in reader.pages:
                text += page.extract_text() + "\n"
            if len(text.strip()) < 200:
                return {"success": False, "text": text, "error": "PDF 未能提取出有效文本，可能是扫描版", "format_type": "PDF_SCAN"}
            return {"success": True, "text": text, "error": "", "format_type": "PDF_TEXT"}
        except ImportError:
            pass
        except Exception as e:
            return {"success": False, "text": "", "error": str(e), "format_type": fmt}
    
    # MOBI/AZW3/DOCX/其他 - 尝试 ebook-convert
    return try_ebook_convert(file_path, fmt)


def try_ebook_convert(file_path: str, fmt: str) -> dict:
    """使用 calibre 的 ebook-convert 提取文本"""
    # 检查是否有 ebook-convert
    try:
        subprocess.run(["ebook-convert", "--version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"success": False, "text": "", "error": f"需要 Calibre 的 ebook-convert 工具来转换 {fmt} 格式", "format_type": fmt}
    
    tmp_txt = tempfile.mktemp(suffix=".txt")
    try:
        result = subprocess.run(
            ["ebook-convert", file_path, tmp_txt],
            capture_output=True, timeout=120, text=True
        )
        if result.returncode != 0:
            return {"success": False, "text": "", "error": f"ebook-convert 失败: {result.stderr[:200]}", "format_type": fmt}
        with open(tmp_txt, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        os.unlink(tmp_txt)
        if len(text.strip()) < 100:
            return {"success": False, "text": text, "error": f"{fmt} 转换后文本过少，可能无法提取", "format_type": fmt}
        return {"success": True, "text": text, "error": "", "format_type": fmt}
    except subprocess.TimeoutExpired:
        try: os.unlink(tmp_txt)
        except: pass
        return {"success": False, "text": "", "error": "转换超时（>120秒）", "format_type": fmt}
    except Exception as e:
        try: os.unlink(tmp_txt)
        except: pass
        return {"success": False, "text": "", "error": str(e), "format_type": fmt}


def check_is_extractable(fmt: str) -> str:
    """返回格式的可提取状态: extractable / limited / unsupported"""
    fmt = fmt.upper()
    if fmt in {"EPUB", "MOBI", "AZW3", "TXT", "AZW", "DOCX", "RTF", "HTML", "XHTML"}:
        return "extractable"
    if fmt == "PDF":
        return "limited"
    return "unsupported"


@app.get("/api/calibre/stats")
async def calibre_stats():
    """书库统计"""
    conn = get_calibre_conn()
    if not conn:
        return {"available": False, "msg": "未找到 Calibre 数据库，请设置 CALIBRE_DB 环境变量"}
    
    cur = conn.execute("""
        SELECT d.format, COUNT(*) as cnt
        FROM data d GROUP BY d.format ORDER BY cnt DESC
    """)
    format_stats = {r["format"]: r["cnt"] for r in cur.fetchall()}
    
    cur = conn.execute("SELECT COUNT(*) as total FROM books")
    total = cur.fetchone()["total"]
    
    cur = conn.execute("SELECT COUNT(*) as cnt FROM books WHERE has_cover=1")
    has_cover = cur.fetchone()["cnt"]
    
    extractable = sum(v for k, v in format_stats.items() if check_is_extractable(k) == "extractable")
    limited = sum(v for k, v in format_stats.items() if check_is_extractable(k) == "limited")
    
    conn.close()
    return {
        "available": True,
        "total_books": total,
        "has_cover": has_cover,
        "format_stats": format_stats,
        "extractable": extractable,
        "limited": limited,
    }


@app.get("/api/calibre/books")
async def calibre_books(page: int = 1, per_page: int = 50, search: str = ""):
    """列出书籍"""
    conn = get_calibre_conn()
    if not conn:
        return {"books": [], "total": 0, "page": page}
    
    where = ""
    params = []
    if search.strip():
        where = "WHERE b.title LIKE ? OR a.name LIKE ?"
        params = [f"%{search.strip()}%", f"%{search.strip()}%"]
    
    # 总数
    cur = conn.execute(f"""
        SELECT COUNT(DISTINCT b.id) as total
        FROM books b
        JOIN books_authors_link bal ON bal.book = b.id
        JOIN authors a ON a.id = bal.author
        {where}
    """, params)
    total = cur.fetchone()["total"]
    
    # 分页
    offset = (page - 1) * per_page
    cur = conn.execute(f"""
        SELECT b.id, b.title, b.path, b.has_cover,
               a.name as author,
               GROUP_CONCAT(d.format || ':' || d.name || ':' || printf('%.1f', d.uncompressed_size/1024.0/1024.0), '|') as formats
        FROM books b
        JOIN books_authors_link bal ON bal.book = b.id
        JOIN authors a ON a.id = bal.author
        LEFT JOIN data d ON d.book = b.id
        {where}
        GROUP BY b.id
        ORDER BY b.id DESC
        LIMIT ? OFFSET ?
    """, params + [per_page, offset])
    
    books = []
    for r in cur.fetchall():
        fmt_list = []
        if r["formats"]:
            for entry in r["formats"].split("|"):
                parts = entry.split(":")
                if len(parts) >= 3:
                    fmt_list.append({
                        "format": parts[0],
                        "name": parts[1],
                        "size_mb": float(parts[2]),
                        "extractable": check_is_extractable(parts[0]),
                    })
        books.append({
            "id": r["id"],
            "title": r["title"],
            "author": r["author"],
            "path": r["path"],
            "has_cover": bool(r["has_cover"]),
            "formats": fmt_list,
            "best_format": fmt_list[0]["format"] if fmt_list else "",
            "can_extract": any(f["extractable"] != "unsupported" for f in fmt_list),
        })
    
    conn.close()
    return {"books": books, "total": total, "page": page, "per_page": per_page}


@app.get("/api/calibre/extract/{book_id}")
async def calibre_extract(book_id: int):
    """启动后台提取任务，立即返回 task_id"""
    conn = get_calibre_conn()
    if not conn:
        return {"success": False, "error": "未找到 Calibre 数据库"}
    
    cur = conn.execute("""
        SELECT b.id, b.title, b.path, a.name as author,
               d.format, d.name
        FROM books b
        JOIN books_authors_link bal ON bal.book = b.id
        JOIN authors a ON a.id = bal.author
        JOIN data d ON d.book = b.id
        WHERE b.id = ?
        ORDER BY 
            CASE d.format
                WHEN 'TXT' THEN 0
                WHEN 'EPUB' THEN 1
                WHEN 'MOBI' THEN 2
                WHEN 'AZW3' THEN 3
                WHEN 'DOCX' THEN 4
                WHEN 'RTF' THEN 5
                WHEN 'HTML' THEN 6
                WHEN 'PDF' THEN 7
                ELSE 8
            END
        LIMIT 1
    """, [book_id])
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "未找到书籍"}
    
    fmt = row["format"]
    file_path = get_book_file_path(row["path"], row["name"], fmt)
    conn.close()
    
    if not file_path:
        return {"success": False, "error": f"找不到文件，请挂载书库目录"}
    
    task_id = str(uuid.uuid4())
    extraction_tasks[task_id] = {"status": "running", "result": None, "error": None, "book_title": row["title"]}
    
    def _do_extract(tid, fp, ffmt, title, author):
        try:
            result = extract_text_from_file(fp, ffmt)
            result["book_title"] = title
            result["book_author"] = author
            extraction_tasks[tid] = {"status": "done", "result": result, "error": None, "book_title": title}
        except Exception as e:
            extraction_tasks[tid] = {"status": "error", "result": None, "error": str(e), "book_title": title}
    
    t = threading.Thread(target=_do_extract, args=(task_id, file_path, fmt, row["title"], row["author"]))
    t.daemon = True
    t.start()
    
    return {"task_id": task_id, "status": "started", "book_title": row["title"]}


extraction_tasks = {}


@app.get("/api/calibre/extract_status/{task_id}")
async def calibre_extract_status(task_id: str):
    """轮询提取任务状态"""
    task = extraction_tasks.get(task_id)
    if not task:
        return {"status": "not_found"}
    
    if task["status"] == "running":
        return {"status": "running", "book_title": task["book_title"]}
    
    if task["status"] == "done":
        return {"status": "done", "result": task["result"]}
    
    return {"status": "error", "error": task["error"], "book_title": task["book_title"]}


# ===================== 前端页面 =====================
@app.get("/", response_class=HTMLResponse)
async def index():
    html = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📖 有声书工坊</title>
<style>
/* ========== Reset & Base ========== */
*, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
:root {
  --bg: #0a0e1a;
  --surface: #111827;
  --surface-hover: #1a2235;
  --card: #0f1525;
  --card-border: #1e2a45;
  --card-border-hover: #2d3f66;
  --text: #eef2f8;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --gold: #f5a623;
  --gold-light: #fbbf4a;
  --gold-glow: rgba(245,166,35,0.2);
  --gold-glow-strong: rgba(245,166,35,0.35);
  --green: #34d399;
  --red: #ef4444;
  --blue: #60a5fa;
  --radius: 16px;
  --radius-sm: 10px;
  --shadow: 0 4px 24px rgba(0,0,0,0.3);
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Noto Sans SC", "PingFang SC", "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  background-image:
    radial-gradient(ellipse at 10% 30%, rgba(245,166,35,0.04) 0%, transparent 50%),
    radial-gradient(ellipse at 90% 70%, rgba(96,165,250,0.03) 0%, transparent 50%),
    radial-gradient(ellipse at 50% 0%, rgba(245,166,35,0.02) 0%, transparent 40%);
  line-height: 1.6;
  overflow-x: hidden;
}
/* ========== Layout ========== */
.app { max-width: 1100px; margin: 0 auto; padding: 32px 24px; }
.app-header {
  display: flex; align-items: center; gap: 16px; margin-bottom: 40px;
  position: relative;
}
.app-logo {
  width: 44px; height: 44px; border-radius: 14px;
  background: linear-gradient(135deg, var(--gold), #d97706);
  display: flex; align-items: center; justify-content: center;
  font-size: 22px; box-shadow: 0 4px 16px var(--gold-glow);
  flex-shrink: 0;
}
.app-title {
  font-size: 20px; font-weight: 700;
  letter-spacing: -0.3px;
}
.app-title span { color: var(--gold); }
.app-subtitle {
  font-size: 13px; color: var(--text-muted);
  margin-top: 2px;
}
.app-version {
  margin-left: auto; font-size: 11px; color: var(--text-muted);
  padding: 6px 14px; border: 1px solid var(--card-border); border-radius: 20px;
  background: var(--surface);
}
/* ========== Steps ========== */
.steps {
  display: flex; align-items: center; gap: 0;
  margin-bottom: 36px; padding: 4px;
  background: var(--surface); border-radius: 100px;
  border: 1px solid var(--card-border);
}
.step {
  flex: 1; display: flex; align-items: center; justify-content: center; gap: 8px;
  padding: 10px 12px; border-radius: 100px;
  font-size: 13px; font-weight: 500; color: var(--text-muted);
  transition: all 0.4s ease; white-space: nowrap; cursor: default;
}
.step.active { background: linear-gradient(135deg, var(--gold), #d97706); color: #fff; box-shadow: 0 2px 12px var(--gold-glow); }
.step.done { color: var(--gold); }
.step-num {
  width: 22px; height: 22px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700;
  background: rgba(255,255,255,0.08);
}
.step.active .step-num { background: rgba(255,255,255,0.25); color: #fff; }
.step.done .step-num { background: var(--gold); color: #0a0e1a; }
.step-icon { font-size: 14px; }
@media(max-width:640px) { .step-label { display:none; } }

/* ========== Panels ========== */
.panels { display: grid; grid-template-columns: 1fr 340px; gap: 24px; }
@media(max-width:900px) { .panels { grid-template-columns: 1fr; } }

.panel {
  background: var(--card); border: 1px solid var(--card-border); border-radius: var(--radius);
  padding: 24px; transition: border-color 0.3s;
}
.panel:hover { border-color: var(--card-border-hover); }
.panel-title {
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.5px;
  margin-bottom: 16px; display: flex; align-items: center; gap: 8px;
}
.panel-divider { height: 1px; background: var(--card-border); margin: 20px 0; }

/* ========== Drop Zone ========== */
.drop-zone {
  border: 2px dashed var(--card-border);
  border-radius: var(--radius-sm); padding: 44px 20px;
  text-align: center; cursor: pointer; position: relative;
  transition: all 0.4s cubic-bezier(.4,0,.2,1);
  background: linear-gradient(180deg, rgba(245,166,35,0.02) 0%, transparent 100%);
  overflow: hidden;
}
.drop-zone::before {
  content: ''; position: absolute; inset: -2px;
  border-radius: inherit;
  background: linear-gradient(135deg, var(--gold), var(--gold-light), var(--gold));
  opacity: 0; transition: opacity 0.4s;
  z-index: -1;
}
.drop-zone:hover::before, .drop-zone.drag-over::before { opacity: 0.15; }
.drop-zone:hover, .drop-zone.drag-over {
  border-color: var(--gold);
  box-shadow: 0 0 40px var(--gold-glow);
  transform: translateY(-2px);
}
.drop-zone-icon {
  font-size: 48px; display: block; margin-bottom: 12px;
  animation: float 3s ease-in-out infinite;
}
@keyframes float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
.drop-zone-text { font-size: 14px; color: var(--text-muted); }
.drop-zone-text span { color: var(--gold); text-decoration: underline; text-underline-offset: 3px; cursor: pointer; font-weight: 500; }
.drop-zone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.file-info {
  display: none; margin-top: 12px; padding: 12px 16px;
  background: rgba(245,166,35,0.06); border: 1px solid rgba(245,166,35,0.15);
  border-radius: var(--radius-sm); font-size: 13px;
  animation: slideDown 0.3s ease;
}
.file-info.visible { display: flex; align-items: center; gap: 8px; }
.file-info-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-info-size { color: var(--text-muted); font-size: 12px; }
@keyframes slideDown { from { opacity:0; transform:translateY(-8px); } to { opacity:1; transform:translateY(0); } }

/* ========== Textarea ========== */
.text-input {
  width: 100%; min-height: 100px; resize: vertical;
  background: rgba(0,0,0,0.3); border: 1px solid var(--card-border);
  border-radius: var(--radius-sm); color: var(--text);
  padding: 14px 16px; font-size: 14px; font-family: inherit;
  outline: none; transition: all 0.3s; line-height: 1.7;
}
.text-input:focus { border-color: var(--gold); box-shadow: 0 0 20px var(--gold-glow); }
.text-input::placeholder { color: var(--text-muted); }
.text-actions {
  display: flex; gap: 8px; margin-top: 10px; align-items: center; flex-wrap: wrap;
}
.char-count { font-size: 12px; color: var(--text-muted); margin-left: auto; }

/* ========== Form Controls ========== */
.form-group {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 14px; flex-wrap: wrap;
}
.form-group:last-child { margin-bottom: 0; }
.form-label { font-size: 13px; color: var(--text-secondary); min-width: 56px; font-weight: 500; }

/* Voice Selector (Card-style) */
.voice-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px; flex: 1;
}
.voice-card {
  padding: 10px 12px; border-radius: var(--radius-sm);
  border: 1px solid var(--card-border); cursor: pointer;
  transition: all 0.25s; background: rgba(0,0,0,0.2);
  font-size: 13px; position: relative;
}
.voice-card:hover { border-color: rgba(245,166,35,0.3); background: rgba(245,166,35,0.04); }
.voice-card.active {
  border-color: var(--gold); background: rgba(245,166,35,0.08);
  box-shadow: 0 0 16px var(--gold-glow);
}
.voice-card .v-name { font-weight: 600; color: var(--text); }
.voice-card .v-desc { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.voice-card .v-check {
  position: absolute; top: 8px; right: 8px;
  width: 16px; height: 16px; border-radius: 50%;
  border: 2px solid var(--card-border); transition: all 0.25s;
  display: flex; align-items: center; justify-content: center;
}
.voice-card.active .v-check {
  background: var(--gold); border-color: var(--gold);
}

.fancy-input {
  flex: 1; min-width: 100px;
  background: rgba(0,0,0,0.3); border: 1px solid var(--card-border);
  border-radius: var(--radius-sm); color: var(--text);
  padding: 10px 14px; font-size: 13px; outline: none; transition: all 0.3s;
}
.fancy-input:focus { border-color: var(--gold); box-shadow: 0 0 16px var(--gold-glow); }
.fancy-input::placeholder { color: var(--text-muted); }

/* ========== Buttons ========== */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 12px 24px; border-radius: var(--radius-sm);
  font-size: 14px; font-weight: 600; border: none; cursor: pointer;
  transition: all 0.3s cubic-bezier(.4,0,.2,1);
  position: relative; overflow: hidden;
  text-decoration: none;
}
.btn::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(255,255,255,0.08), transparent);
  opacity: 0; transition: opacity 0.3s;
}
.btn:hover::after { opacity: 1; }
.btn:active { transform: scale(0.97); }

.btn-primary {
  background: linear-gradient(135deg, var(--gold), #d97706);
  color: #0a0e1a; box-shadow: 0 4px 16px var(--gold-glow);
  width: 100%;
}
.btn-primary:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 24px var(--gold-glow-strong);
}
.btn-primary:disabled { opacity: 0.35; cursor: not-allowed; transform: none; box-shadow: none; }

.btn-secondary {
  background: var(--surface); color: var(--text-secondary);
  border: 1px solid var(--card-border);
}
.btn-secondary:hover { background: var(--surface-hover); color: var(--text); }

.btn-danger {
  background: rgba(239,68,68,0.1); color: var(--red);
  border: 1px solid rgba(239,68,68,0.2);
}
.btn-danger:hover { background: rgba(239,68,68,0.2); }

.btn-sm { padding: 8px 16px; font-size: 12px; }
.btn-icon { width: 36px; height: 36px; padding: 0; border-radius: 8px; }

.btn-group { display: flex; gap: 10px; flex-wrap: wrap; }

/* ========== Progress ========== */
.progress-section { display: none; margin-top: 0; }
.progress-section.visible { display: block; }

.progress-ring-wrap {
  display: flex; align-items: center; gap: 28px;
  margin-bottom: 20px;
}
.progress-ring {
  position: relative; width: 80px; height: 80px; flex-shrink: 0;
}
.progress-ring svg { transform: rotate(-90deg); }
.progress-ring-bg { fill: none; stroke: rgba(255,255,255,0.06); stroke-width: 5; }
.progress-ring-fg {
  fill: none; stroke: url(#goldGrad); stroke-width: 5; stroke-linecap: round;
  transition: stroke-dashoffset 0.6s cubic-bezier(.4,0,.2,1);
}
.progress-ring-text {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 700;
}
.progress-ring-text .pct { font-size: 12px; color: var(--text-muted); margin-left: 1px; }

.progress-meta { flex: 1; min-width: 0; }
.progress-state {
  display: flex; align-items: center; gap: 8px;
  font-size: 14px; font-weight: 500; margin-bottom: 6px;
}
.progress-detail { font-size: 12px; color: var(--text-muted); }

.progress-bar-wrap {
  width: 100%; height: 4px; border-radius: 4px;
  background: rgba(255,255,255,0.06); overflow: hidden; position: relative;
}
.progress-bar-fill {
  height: 100%; border-radius: 4px;
  background: linear-gradient(90deg, var(--gold), var(--gold-light));
  transition: width 0.5s cubic-bezier(.4,0,.2,1);
  position: relative;
}
.progress-bar-fill::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
  animation: barShimmer 1.5s infinite;
}
@keyframes barShimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }

/* ========== Log ========== */
.log-box {
  background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.04);
  border-radius: var(--radius-sm); padding: 14px 16px;
  height: 220px; overflow-y: auto;
  font-size: 12px; line-height: 1.8;
  font-family: "SF Mono", "Cascadia Code", "Fira Code", Consolas, monospace;
  margin-top: 16px;
}
.log-box::-webkit-scrollbar { width: 4px; }
.log-box::-webkit-scrollbar-track { background: transparent; }
.log-box::-webkit-scrollbar-thumb { background: var(--gold); border-radius: 2px; }

/* ========== Status Badge ========== */
.state-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 20px;
  font-size: 12px; font-weight: 500;
}
.state-idle { background: rgba(100,116,139,0.12); color: var(--text-muted); }
.state-running { background: rgba(245,166,35,0.12); color: var(--gold); }
.state-running::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--gold); animation: pulse 1s ease-in-out infinite; }
.state-done { background: rgba(52,211,153,0.12); color: var(--green); }
.state-error { background: rgba(239,68,68,0.12); color: var(--red); }
.state-cancelled { background: rgba(100,116,139,0.12); color: var(--text-muted); }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }

/* ========== Download ========== */
.download-box {
  display: none; margin-top: 0;
  animation: fadeUp 0.5s ease;
}
.download-box.visible { display: block; }
@keyframes fadeUp { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:translateY(0); } }

.download-card {
  background: linear-gradient(135deg, rgba(245,166,35,0.08), rgba(52,211,153,0.04));
  border: 1px solid rgba(245,166,35,0.2);
  border-radius: var(--radius); padding: 24px;
  text-align: center;
}
.download-icon { font-size: 48px; margin-bottom: 12px; }
.download-title { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
.download-sub { font-size: 13px; color: var(--text-muted); margin-bottom: 20px; }
.download-btn {
  display: inline-flex; align-items: center; gap: 10px;
  padding: 16px 40px;
  background: linear-gradient(135deg, var(--gold), #d97706);
  color: #0a0e1a; border-radius: var(--radius-sm);
  text-decoration: none; font-weight: 700; font-size: 16px;
  transition: all 0.3s; box-shadow: 0 4px 20px var(--gold-glow);
}
.download-btn:hover { transform: translateY(-3px); box-shadow: 0 8px 30px var(--gold-glow-strong); }

/* ========== Side Panel ========== */
.side-section {
  background: var(--card); border: 1px solid var(--card-border);
  border-radius: var(--radius); padding: 20px; margin-bottom: 20px;
  transition: border-color 0.3s;
}
.side-section:hover { border-color: var(--card-border-hover); }
.side-section:last-child { margin-bottom: 0; }
.side-title {
  font-size: 12px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.8px;
  margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between;
}
.side-status-content { font-size: 14px; color: var(--text-secondary); line-height: 1.7; }

/* History */
.history-scroll {
  max-height: 260px; overflow-y: auto;
}
.history-scroll::-webkit-scrollbar { width: 3px; }
.history-scroll::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 2px; }
.history-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: 8px;
  margin-bottom: 4px; cursor: pointer;
  transition: all 0.2s; font-size: 13px;
}
.history-item:hover { background: rgba(255,255,255,0.03); }
.history-item .h-icon { font-size: 16px; }
.history-item .h-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-item .h-time { font-size: 11px; color: var(--text-muted); min-width: 55px; text-align: right; }
.history-empty { color: var(--text-muted); font-size: 13px; text-align: center; padding: 20px; }

/* Server files */
.server-files-scroll {
  max-height: 180px; overflow-y: auto;
}
.server-files-scroll::-webkit-scrollbar { width: 3px; }
.server-files-scroll::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 2px; }
.server-file-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-radius: 6px; margin-bottom: 2px;
  cursor: pointer; transition: all 0.2s; font-size: 12px;
}
.server-file-item:hover { background: rgba(245,166,35,0.06); }
.server-file-item .sf-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.server-file-item .sf-size { color: var(--text-muted); font-size: 11px; }
.server-file-input {
  width: 100%;
  background: rgba(0,0,0,0.3); border: 1px solid var(--card-border);
  border-radius: 6px; color: var(--text); padding: 8px 12px;
  font-size: 12px; outline: none; transition: all 0.2s; margin-bottom: 8px;
}
.server-file-input:focus { border-color: var(--gold); }

/* ========== Chapter Preview ========== */
.chapter-preview { margin-top: 12px; display: none; }
.chapter-preview.visible { display: block; }
.chapter-list {
  max-height: 160px; overflow-y: auto;
  display: flex; flex-direction: column; gap: 4px;
}
.chapter-list::-webkit-scrollbar { width: 3px; }
.chapter-list::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 2px; }
.chapter-tag {
  padding: 6px 12px; font-size: 12px;
  border-left: 3px solid var(--gold);
  background: rgba(245,166,35,0.04);
  border-radius: 0 6px 6px 0;
  color: var(--text-secondary);
}
.chapter-count { font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }

/* ========== Toast ========== */
.toast-container {
  position: fixed; bottom: 32px; left: 50%; transform: translateX(-50%);
  z-index: 9999; display: flex; flex-direction: column; gap: 8px;
  pointer-events: none;
}
.toast {
  padding: 14px 24px; border-radius: var(--radius-sm);
  font-size: 14px; backdrop-filter: blur(16px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  animation: toastIn 0.4s ease, toastOut 0.4s ease 2.6s forwards;
  pointer-events: auto;
  display: flex; align-items: center; gap: 10px;
}
.toast-success { background: rgba(52,211,153,0.15); border: 1px solid rgba(52,211,153,0.25); color: var(--green); }
.toast-error { background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.25); color: var(--red); }
.toast-info { background: rgba(245,166,35,0.15); border: 1px solid rgba(245,166,35,0.25); color: var(--gold); }
@keyframes toastIn { from { opacity:0; transform:translateY(16px) scale(0.95); } to { opacity:1; transform:translateY(0) scale(1); } }
@keyframes toastOut { from { opacity:1; } to { opacity:0; transform:translateY(-8px); } }

/* ========== Responsive ========== */
@media(max-width:900px) {
  .app { padding: 20px 16px; }
  .app-header { margin-bottom: 28px; }
  .voice-grid { grid-template-columns: 1fr; }
  .progress-ring-wrap { flex-direction: column; align-items: flex-start; gap: 16px; }
}
@media(max-width:640px) {
  .app { padding: 16px 12px; }
  .app-title { font-size: 17px; }
  .steps { border-radius: var(--radius-sm); padding: 3px; }
  .step { padding: 8px 10px; font-size: 12px; }
}

/* ========== Calibre ========== */
.calibre-search { margin-bottom: 10px; }
.calibre-stats {
  display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px;
  font-size: 11px;
}
.calibre-stat-tag {
  padding: 2px 8px; border-radius: 4px; background: rgba(255,255,255,0.04);
  color: var(--text-muted);
}
.calibre-stat-tag strong { color: var(--text-secondary); }
.calibre-book-list {
  max-height: 400px; overflow-y: auto;
}
.calibre-book-list::-webkit-scrollbar { width: 3px; }
.calibre-book-list::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 2px; }
.calibre-book {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-radius: 6px; margin-bottom: 3px;
  cursor: pointer; transition: all 0.2s; position: relative;
}
.calibre-book:hover { background: rgba(245,166,35,0.06); }
.calibre-book.loading { opacity: 0.5; pointer-events: none; }
.calibre-book .cb-info { flex: 1; min-width: 0; }
.calibre-book .cb-title {
  font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--text);
}
.calibre-book .cb-author {
  font-size: 11px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.calibre-book .cb-badges { display: flex; gap: 3px; flex-shrink: 0; }
.calibre-book .cb-badge {
  font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 600;
}
.badge-EPUB, .badge-MOBI, .badge-AZW3, .badge-TXT, .badge-DOCX, .badge-RTF { background: rgba(52,211,153,0.12); color: var(--green); }
.badge-PDF { background: rgba(245,166,35,0.12); color: var(--gold); }
.badge-unsupported { background: rgba(239,68,68,0.1); color: var(--red); }
.calibre-book .cb-status {
  font-size: 10px; color: var(--text-muted); white-space: nowrap;
}
.calibre-loading-dots::after { content: '...'; animation: dots 1.2s infinite; }
@keyframes dots { 0% { content: '.'; } 33% { content: '..'; } 66% { content: '...'; } }
.calibre-error {
  padding: 16px; text-align: center; color: var(--text-muted);
  font-size: 12px;
}
.calibre-error-icon { font-size: 28px; display: block; margin-bottom: 6px; }
</style>
</head>
<body>
<div class="app">
  <!-- Header -->
  <header class="app-header">
    <div class="app-logo">📖</div>
    <div>
      <div class="app-title"><span>有声书</span>工坊</div>
      <div class="app-subtitle">TXT 电子书 → 有声书 · Edge-TTS</div>
    </div>
    <span class="app-version">v2.0</span>
  </header>

  <!-- Step Indicator -->
  <div class="steps" id="steps">
    <div class="step active" data-step="1"><span class="step-num">1</span><span class="step-label">上传文本</span></div>
    <div class="step" data-step="2"><span class="step-num">2</span><span class="step-label">选择配置</span></div>
    <div class="step" data-step="3"><span class="step-num">3</span><span class="step-label">生成有声书</span></div>
  </div>

  <div class="panels">
    <!-- ========== LEFT ========== -->
    <div class="main-panel">

      <!-- Upload -->
      <div class="panel">
        <div class="panel-title">📂 上传电子书</div>
        <div class="drop-zone" id="dropZone">
          <span class="drop-zone-icon">📄</span>
          <div class="drop-zone-text">将 TXT 文件拖到这里，或 <span>浏览文件</span></div>
          <input type="file" id="fileInput" accept=".txt">
        </div>
        <div class="file-info" id="fileInfo">
          <span>✅</span>
          <span class="file-info-name" id="fileName"></span>
          <span class="file-info-size" id="fileSize"></span>
          <button class="btn btn-secondary btn-sm" onclick="clearFile()" style="margin-left:auto">移除</button>
        </div>

        <div class="panel-divider"></div>
        <div class="panel-title">✍️ 或粘贴文本</div>
        <textarea class="text-input" id="pasteText" rows="5" placeholder="直接粘贴小说或文章内容……"></textarea>
        <div class="text-actions">
          <button class="btn btn-secondary btn-sm" onclick="detectChapters()">📑 检测章节</button>
          <span class="char-count" id="charCount">0 字</span>
        </div>
        <div class="chapter-preview" id="chapterPreview">
          <div class="chapter-count" id="chapterCount"></div>
          <div class="chapter-list" id="chapterList"></div>
        </div>
      </div>

      <!-- Settings -->
      <div class="panel" id="settingsPanel">
        <div class="panel-title">⚙️ 合成配置</div>

        <div class="form-group">
          <span class="form-label">音色</span>
          <div class="voice-grid" id="voiceGrid">'''
    for v_id, v_name, v_icon in VOICE_LIST:
        active = ' active' if v_id == DEFAULT_VOICE else ''
        html += f'''<div class="voice-card{active}" data-voice="{v_id}" onclick="selectVoice(this)">
          <div class="v-name">{v_icon} {v_name.split('·')[0].strip()}</div>
          <div class="v-desc">{v_name.split('·')[1].strip() if '·' in v_name else ''}</div>
          <div class="v-check">✓</div>
        </div>\n'''
    html += r'''</div>
        </div>

        <div class="form-group">
          <span class="form-label">语速</span>
          <div style="display:flex;gap:8px;flex:1;align-items:center">
            <input class="fancy-input" id="rateInput" value="+20%" placeholder="例：+20% / -10%" style="flex:1">
            <span style="font-size:11px;color:var(--text-muted);white-space:nowrap">-50% ~ +50%</span>
            <button class="btn btn-secondary btn-sm" onclick="previewVoice()" title="试听当前音色">🔊</button>
          </div>
        </div>

        <div class="form-group">
          <span class="form-label">任务名</span>
          <input class="fancy-input" id="taskName" placeholder="选填，默认用文件名">
        </div>

        <div class="panel-divider"></div>

        <div class="btn-group" style="flex-direction:column">
          <button class="btn btn-primary" id="startBtn" onclick="startTask()">
            <span style="font-size:16px">🎙️</span> 开始合成有声书
          </button>
          <button class="btn btn-danger" id="cancelBtn" style="display:none" onclick="cancelTask()">⏹ 取消任务</button>
        </div>
      </div>

      <!-- Progress -->
      <div class="panel progress-section" id="progressSection">
        <svg width="0" height="0"><defs><linearGradient id="goldGrad" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" stop-color="var(--gold)"/><stop offset="100%" stop-color="var(--gold-light)"/></linearGradient></defs></svg>

        <div class="progress-ring-wrap">
          <div class="progress-ring">
            <svg width="80" height="80" viewBox="0 0 80 80">
              <circle class="progress-ring-bg" cx="40" cy="40" r="34"/>
              <circle class="progress-ring-fg" id="progressCircle" cx="40" cy="40" r="34" stroke-dasharray="213.6" stroke-dashoffset="213.6"/>
            </svg>
            <div class="progress-ring-text"><span id="progressNum">0</span><span class="pct">%</span></div>
          </div>
          <div class="progress-meta">
            <div class="progress-state">
              <span class="state-badge state-idle" id="stateBadge">等待中</span>
              <span id="statusText" style="color:var(--text-muted);font-size:13px;font-weight:400">准备就绪</span>
            </div>
            <div class="progress-detail" id="progressDetail">0 段 · 等待开始</div>
            <div class="progress-bar-wrap" style="margin-top:10px">
              <div class="progress-bar-fill" id="progressBarFill" style="width:0%"></div>
            </div>
          </div>
        </div>
        <div class="log-box" id="logBox"><span style="color:var(--text-muted)">📝 运行日志将显示在这里……</span></div>
      </div>

      <!-- Download -->
      <div class="download-box" id="downloadBox">
        <div class="download-card">
          <div class="download-icon">🎉</div>
          <div class="download-title">有声书制作完成！</div>
          <div class="download-sub" id="downloadFileName">点击下方按钮下载</div>
          <a class="download-btn" href="/download_full" download id="downloadLink">
            ⬇ 下载完整 MP3
          </a>
        </div>
      </div>
    </div>

    <!-- ========== RIGHT SIDEBAR ========== -->
    <div class="side-panel">
      <div class="side-section">
        <div class="side-title">📌 当前状态</div>
        <div class="side-status-content" id="sideStatus">等待任务...</div>
      </div>

      <div class="side-section">
        <div class="side-title">🕐 合成历史 <button class="btn btn-secondary btn-sm" onclick="loadHistory()" style="padding:2px 8px;font-size:10px">刷新</button></div>
        <div class="history-scroll" id="historyList">
          <div class="history-empty">暂无记录</div>
        </div>
      </div>

      <div class="side-section" id="calibreSection">
        <div class="side-title">📚 Calibre 书库 <button class="btn btn-secondary btn-sm" onclick="loadCalibreStats()" style="padding:2px 8px;font-size:10px">刷新</button></div>
        <div id="calibrePanel">
          <div class="calibre-search">
            <input class="server-file-input" id="calibreSearch" placeholder="搜索书名或作者..." oninput="debounceSearch()" style="margin-bottom:0">
          </div>
          <div class="calibre-stats" id="calibreStats"></div>
          <div class="calibre-book-list" id="calibreBookList">
            <div class="history-empty">正在加载书库...</div>
          </div>
        </div>
      </div>

      <div class="side-section">
        <div class="side-title">📁 服务器文件</div>
        <input class="server-file-input" id="scanDir" value="./output" placeholder="目录路径">
        <button class="btn btn-secondary btn-sm" onclick="scanServerFiles()" style="width:100%;margin-bottom:8px">🔍 扫描</button>
        <div class="server-files-scroll" id="serverFiles">
          <div class="history-empty" style="font-size:12px">点击扫描查看文件</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast-container" id="toastContainer"></div>

<script>
// ========== Voice Selector ==========
function selectVoice(el) {
  document.querySelectorAll('.voice-card').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
}
function getSelectedVoice() {
  const active = document.querySelector('.voice-card.active');
  return active ? active.dataset.voice : 'zh-CN-XiaoyiNeural';
}

// ========== Toast ==========
function showToast(msg, type='info') {
  const c = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast toast-'+type;
  const icons = {success:'✅',error:'❌',info:'ℹ️'};
  t.innerHTML = (icons[type]||'ℹ️')+' '+msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

// ========== Drag & Drop ==========
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
let currentFile = null;

dropZone.addEventListener('dragover', e=>{e.preventDefault();dropZone.classList.add('drag-over')});
dropZone.addEventListener('dragleave', ()=>dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e=>{e.preventDefault();dropZone.classList.remove('drag-over');if(e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0])});
fileInput.addEventListener('change', e=>{if(e.target.files.length) handleFile(e.target.files[0])});

function handleFile(f){if(!f.name.endsWith('.txt')){showToast('请选择 TXT 格式文件','error');return}
  currentFile=f;
  const fi=document.getElementById('fileInfo');fi.classList.add('visible');
  document.getElementById('fileName').textContent=f.name;
  document.getElementById('fileSize').textContent='('+(f.size/1024).toFixed(1)+' KB)';
  document.getElementById('pasteText').value='';document.getElementById('charCount').textContent='0 字';
  updateStep(1);showToast('已选择：'+f.name,'success')}

function clearFile(){currentFile=null;document.getElementById('fileInfo').classList.remove('visible');fileInput.value=''}

// ========== Text ==========
document.getElementById('pasteText').addEventListener('input', function(){
  document.getElementById('charCount').textContent=this.value.length+' 字';
  if(this.value.trim()) updateStep(1);
});

// ========== Step Indicator ==========
function updateStep(n) {
  document.querySelectorAll('.step').forEach((s,i)=>{s.classList.toggle('active',i+1===n);s.classList.toggle('done',i+1<n)});
}

// ========== Preview Voice ==========
async function previewVoice() {
  const voice=getSelectedVoice(),rate=document.getElementById('rateInput').value||'+20%';
  try{
    const r=await fetch('/api/preview_voice?voice='+encodeURIComponent(voice)+'&rate='+encodeURIComponent(rate)+'&text='+encodeURIComponent('你好，欢迎使用有声书工坊。这是一段语音预览，请选择您喜欢的音色和语速。'));
    if(!r.ok) throw Error('预览失败');
    const blob=await r.blob();const url=URL.createObjectURL(blob);new Audio(url).play();
    showToast('🔊 正在播放语音预览','info');
  }catch(e){showToast('试听失败','error')}
}

// ========== Detect Chapters ==========
async function detectChapters() {
  const text=document.getElementById('pasteText').value;
  if(!text.trim()){showToast('请先粘贴文本内容','error');return}
  try{
    const r=await fetch('/api/detect_chapters?text='+encodeURIComponent(text.slice(0,50000)));
    const d=await r.json();const ch=d.chapters||[];
    const pre=document.getElementById('chapterPreview');const list=document.getElementById('chapterList');
    const cnt=document.getElementById('chapterCount');
    if(ch.length>1){
      pre.classList.add('visible');
      cnt.textContent='📑 检测到 '+ch.length+' 个章节';
      list.innerHTML=ch.map(c=>'<div class="chapter-tag">'+escHtml(c.title)+'</div>').join('');
      showToast('检测到 '+ch.length+' 个章节','success');
    }else{
      pre.classList.remove('visible');showToast('未检测到章节结构，将按全文处理','info');
    }
  }catch(e){showToast('章节检测失败','error')}
}
function escHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

// ========== Server Files ==========
async function scanServerFiles(){
  const dir=document.getElementById('scanDir').value||'./output';
  try{
    const r=await fetch('/api/scan_txt?base_dir='+encodeURIComponent(dir));
    const d=await r.json();const files=d.files||[];
    const container=document.getElementById('serverFiles');
    if(!files.length){container.innerHTML='<div class="history-empty" style="font-size:12px">📭 未找到 TXT 文件</div>';return}
    container.innerHTML=files.map(f=>
      '<div class="server-file-item" onclick="loadServerFile(\''+escHtml(f.path)+'\')">'+
        '<span>📄</span><span class="sf-name">'+escHtml(f.path)+'</span><span class="sf-size">'+(f.size/1024).toFixed(0)+'KB</span></div>'
    ).join('');
    showToast('找到 '+files.length+' 个 TXT 文件','success');
  }catch(e){showToast('扫描失败','error')}
}
async function loadServerFile(path){
  try{
    const r=await fetch('/api/read_txt?file_path='+encodeURIComponent(path));
    const d=await r.json();if(d.code!==0){showToast(d.msg,'error');return}
    document.getElementById('pasteText').value=d.content;
    document.getElementById('charCount').textContent=d.content.length+' 字';
    document.getElementById('taskName').value=d.name.replace('.txt','');
    clearFile();showToast('已加载：'+d.name,'success');
  }catch(e){showToast('加载失败','error')}
}

// ========== Start Task ==========
async function startTask(){
  const txtFile=currentFile,text=document.getElementById('pasteText').value;
  const voice=getSelectedVoice(),rate=document.getElementById('rateInput').value||'+20%';
  const taskName=document.getElementById('taskName').value;
  if(!txtFile&&!text.trim()){showToast('请上传文件或粘贴文本','error');return}
  const fd=new FormData();
  if(txtFile) fd.append('txtFile',txtFile);
  if(text.trim()) fd.append('text',text);
  fd.append('voice',voice);fd.append('rate',rate);
  if(taskName.trim()) fd.append('task_name_input',taskName.trim());
  try{
    const r=await fetch('/api/start',{method:'POST',body:fd});
    const d=await r.json();if(d.code!==0){showToast(d.msg,'error');return}
    showToast(d.msg,'success');
    document.getElementById('startBtn').disabled=true;
    document.getElementById('cancelBtn').style.display='inline-flex';
    document.getElementById('progressSection').classList.add('visible');
    document.getElementById('downloadBox').classList.remove('visible');
    updateStep(3);startPolling();
  }catch(e){showToast('启动失败','error')}
}

// ========== Cancel Task ==========
async function cancelTask(){
  try{const r=await fetch('/api/cancel',{method:'POST'});const d=await r.json();showToast(d.msg,d.code===0?'info':'error')}
  catch(e){showToast('取消失败','error')}
}

// ========== Poll ==========
let pollTimer=null;
function startPolling(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer=setInterval(async()=>{
    try{
      const r=await fetch('/api/get_runtime');const d=await r.json();updateUI(d);
      if(d.state==='done'||d.state==='error'||d.state==='cancelled'){
        clearInterval(pollTimer);pollTimer=null;
        document.getElementById('startBtn').disabled=false;
        document.getElementById('cancelBtn').style.display='none';
        if(d.state==='done'&&d.has_file){
          document.getElementById('downloadBox').classList.add('visible');
          document.getElementById('downloadFileName').textContent=(d.task_name||'有声书')+' 已生成';
        }
      }
    }catch(e){}
  },800);
}

function updateUI(d){
  const badge=document.getElementById('stateBadge');
  const states={idle:'state-idle',running:'state-running',done:'state-done',error:'state-error',cancelled:'state-cancelled'};
  const labels={idle:'等待中',running:'生成中',done:'已完成',error:'出错',cancelled:'已取消'};
  badge.className='state-badge '+(states[d.state]||'state-idle');
  badge.textContent=labels[d.state]||d.state;

  document.getElementById('statusText').textContent=d.status||'—';
  document.getElementById('sideStatus').textContent=d.status||'等待任务...';

  const p=d.progress||0;
  document.getElementById('progressNum').textContent=p;
  document.getElementById('progressBarFill').style.width=p+'%';
  const circumference=213.6;
  document.getElementById('progressCircle').style.strokeDashoffset=circumference-(p/100)*circumference;
  document.getElementById('progressDetail').textContent=(d.total_seg||0)+' 段 · '+(d.status||'');

  const logBox=document.getElementById('logBox');
  if(d.log&&d.log.length){logBox.innerHTML=d.log.map(l=>'<span>'+escHtml(l)+'</span>').join('<br>');logBox.scrollTop=logBox.scrollHeight;}
}

// ========== History ==========
async function loadHistory(){
  try{
    const r=await fetch('/api/history');const d=await r.json();const list=d.history||[];
    const container=document.getElementById('historyList');
    if(!list.length){container.innerHTML='<div class="history-empty">暂无记录</div>';return}
    const stateColor={完成:'var(--green)',失败:'var(--red)',已取消:'var(--text-muted)'};
    container.innerHTML=list.map(h=>'<div class="history-item"><span class="h-icon">📖</span><span class="h-name">'+escHtml(h.name)+
      '</span><span style="color:'+(stateColor[h.state]||'var(--text-muted)')+';font-size:11px">'+escHtml(h.state)+
      '</span><span class="h-time">'+escHtml(h.time)+'</span></div>').join('');
  }catch(e){}
}

// ========== Calibre ==========
let calibreSearchTimer = null;
function debounceSearch() {
  clearTimeout(calibreSearchTimer);
  calibreSearchTimer = setTimeout(() => loadCalibreBooks(), 400);
}

async function loadCalibreStats() {
  const panel = document.getElementById('calibrePanel');
  try {
    const r = await fetch('/api/calibre/stats');
    const d = await r.json();
    if (!d.available) {
      document.getElementById('calibreStats').innerHTML = '';
      document.getElementById('calibreBookList').innerHTML =
        '<div class="calibre-error"><span class="calibre-error-icon">📭</span>未检测到 Calibre 书库<br><span style="font-size:11px">请将 metadata.db 放置在程序目录，<br>或设置 CALIBRE_DB 环境变量</span></div>';
      return;
    }
    const tags = [];
    tags.push('<span class="calibre-stat-tag">📚 <strong>' + d.total_books + '</strong> 本</span>');
    tags.push('<span class="calibre-stat-tag">✅ <strong>' + d.extractable + '</strong> 可转</span>');
    if (d.limited > 0) tags.push('<span class="calibre-stat-tag">⚠️ <strong>' + d.limited + '</strong> PDF</span>');
    document.getElementById('calibreStats').innerHTML = tags.join(' ');
    loadCalibreBooks();
  } catch (e) {
    document.getElementById('calibreBookList').innerHTML =
      '<div class="calibre-error"><span class="calibre-error-icon">❌</span>连接失败</div>';
  }
}

async function loadCalibreBooks() {
  const search = document.getElementById('calibreSearch').value.trim();
  const list = document.getElementById('calibreBookList');
  try {
    const r = await fetch('/api/calibre/books?page=1&per_page=100&search=' + encodeURIComponent(search));
    const d = await r.json();
    if (!d.books || !d.books.length) {
      list.innerHTML = '<div class="history-empty">' + (search ? '未找到匹配的书籍' : '书库为空') + '</div>';
      return;
    }
    list.innerHTML = d.books.map(function(b) {
      var badges = '';
      if (b.formats) {
        b.formats.forEach(function(f) {
          var cls = f.extractable === 'extractable' ? 'badge-' + f.format : (f.extractable === 'limited' ? 'badge-PDF' : 'badge-unsupported');
          badges += '<span class="cb-badge ' + cls + '">' + f.format + '</span>';
        });
      }
      var status = b.can_extract ? '' : '<span class="cb-status" style="color:var(--red)">❌</span>';
      return '<div class="calibre-book" onclick="extractCalibreBook(' + b.id + ', this)" title="' + escHtml(b.title) + ' — ' + escHtml(b.author) + '">' +
        '<div class="cb-info">' +
        '<div class="cb-title">' + escHtml(b.title) + '</div>' +
        '<div class="cb-author">' + escHtml(b.author) + '</div>' +
        '</div>' +
        '<div class="cb-badges">' + badges + '</div>' +
        status +
        '</div>';
    }).join('');

    if (d.total > d.books.length) {
      list.innerHTML += '<div class="history-empty" style="font-size:11px">显示 ' + d.books.length + ' / ' + d.total + ' 本，请输入更精确的搜索</div>';
    }
  } catch (e) {
    list.innerHTML = '<div class="calibre-error"><span class="calibre-error-icon">❌</span>加载失败</div>';
  }
}

async function extractCalibreBook(bookId, el) {
  el.classList.add('loading');
  el.querySelector('.cb-title').innerHTML = '⏳ 正在提取...';
  var bookTitle = '';
  try {
    const r = await fetch('/api/calibre/extract/' + bookId);
    const d = await r.json();
    if (d.status !== 'started' || !d.task_id) {
      el.classList.remove('loading');
      showToast('❌ ' + (d.error || '启动失败'), 'error');
      el.querySelector('.cb-title').textContent = d.book_title || '提取失败';
      return;
    }
    bookTitle = d.book_title || '';
    // 显示带计时器的状态
    var dots = 0;
    var statusMsg = el.querySelector('.cb-status');
    if (!statusMsg) {
      statusMsg = document.createElement('span');
      statusMsg.className = 'cb-status';
      el.appendChild(statusMsg);
    }
    statusMsg.textContent = '⏳ 提取中';

    // 轮询提取状态
    var pollTimer = setInterval(async function() {
      try {
        var r2 = await fetch('/api/calibre/extract_status/' + d.task_id);
        var s = await r2.json();
        if (s.status === 'running') {
          dots = (dots + 1) % 4;
          statusMsg.textContent = '⏳ 提取中' + '.'.repeat(dots);
          return;
        }
        clearInterval(pollTimer);
        el.classList.remove('loading');
        if (s.status === 'done' && s.result && s.result.success) {
          document.getElementById('pasteText').value = s.result.text;
          document.getElementById('charCount').textContent = s.result.text.length + ' 字';
          document.getElementById('taskName').value = s.result.book_title || bookTitle;
          clearFile();
          var wc = (s.result.text.length / 10000).toFixed(1);
          showToast('✅ 已加载：《' + (s.result.book_title || bookTitle) + '》' + wc + '万字', 'success');
          document.getElementById('pasteText').scrollIntoView({behavior: 'smooth'});
          statusMsg.textContent = '✅ ' + wc + '万字';
          el.querySelector('.cb-title').textContent = s.result.book_title || bookTitle;
        } else {
          var errMsg = (s.result && s.result.error) || s.error || '提取失败';
          showToast('❌ ' + errMsg, 'error');
          statusMsg.textContent = '❌ 失败';
          el.querySelector('.cb-title').textContent = s.book_title || bookTitle || '提取失败';
        }
      } catch(e) {
        clearInterval(pollTimer);
        el.classList.remove('loading');
        showToast('❌ 轮询失败', 'error');
        el.querySelector('.cb-title').textContent = bookTitle || '请求失败';
      }
    }, 1000);
  } catch (e) {
    el.classList.remove('loading');
    showToast('❌ 请求失败', 'error');
    el.querySelector('.cb-title').textContent = bookTitle || '请求失败';
  }
}

// ========== Init ==========
loadHistory();scanServerFiles();loadCalibreStats();
</script>

</body>
</html>'''
    return html


if __name__ == "__main__":
    load_history()
    uvicorn.run(app, host="0.0.0.0", port=8000)
