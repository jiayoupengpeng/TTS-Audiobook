import asyncio
import os
import re
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
    ("zh-CN-XiaoxiaoNeural", "晓晓 女声·温暖"),
    ("zh-CN-XiaoyiNeural", "晓艺 女声·活泼"),
    ("zh-CN-YunyangNeural", "云扬 男声·新闻"),
    ("zh-CN-YunxiNeural", "云希 男声·阳光"),
    ("zh-CN-YunjianNeural", "云健 男声·激情"),
    ("zh-CN-YunxiaNeural", "云夏 女声·可爱"),
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


# ===================== 前端页面 =====================
@app.get("/", response_class=HTMLResponse)
async def index():
    html = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📖 TTS 有声书工坊</title>
<style>
/* ============ CSS 变量 & 全局 ============ */
:root {
  --bg: #0b0e17;
  --surface: rgba(255,255,255,0.04);
  --surface-hover: rgba(255,255,255,0.08);
  --card: rgba(255,255,255,0.06);
  --card-border: rgba(255,255,255,0.08);
  --text: #e8edf5;
  --text-dim: #8892a8;
  --accent: #5b8def;
  --accent-glow: rgba(91,141,239,0.3);
  --green: #34d399;
  --green-glow: rgba(52,211,153,0.25);
  --orange: #f59e0b;
  --red: #ef4444;
  --radius: 14px;
  --gap: 20px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;
  background-image:radial-gradient(ellipse at 20% 50%, rgba(91,141,239,0.06) 0%, transparent 50%),
                    radial-gradient(ellipse at 80% 20%, rgba(52,211,153,0.04) 0%, transparent 50%);
}
/* ============ 布局 ============ */
.app{max-width:1300px;margin:0 auto;padding:24px}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;flex-wrap:wrap;gap:12px}
.header h1{font-size:22px;font-weight:600;background:linear-gradient(135deg,#5b8def,#34d399);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-0.3px}
.header-badge{font-size:12px;color:var(--text-dim);background:var(--surface);padding:6px 14px;border-radius:20px;border:1px solid var(--card-border)}
.grid{display:grid;grid-template-columns:1fr 340px;gap:var(--gap)}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
/* ============ 卡片 ============ */
.card{
  background:var(--card);border:1px solid var(--card-border);border-radius:var(--radius);
  padding:22px;backdrop-filter:blur(12px);transition:border-color 0.2s;
}
.card:hover{border-color:rgba(255,255,255,0.12)}
.card-title{font-size:14px;font-weight:500;color:var(--text-dim);margin-bottom:16px;display:flex;align-items:center;gap:8px}
/* ============ 上传区 ============ */
.drop-zone{
  border:2px dashed rgba(255,255,255,0.15);border-radius:12px;padding:36px 20px;
  text-align:center;cursor:pointer;transition:all 0.3s;position:relative;
  background:rgba(91,141,239,0.02);
}
.drop-zone:hover,.drop-zone.drag-over{
  border-color:var(--accent);background:rgba(91,141,239,0.06);
  box-shadow:0 0 30px var(--accent-glow);
}
.drop-zone-icon{font-size:36px;margin-bottom:8px}
.drop-zone-text{font-size:14px;color:var(--text-dim)}
.drop-zone-text span{color:var(--accent);text-decoration:underline;cursor:pointer}
.drop-zone input{position:absolute;inset:0;opacity:0;cursor:pointer}
/* ============ 表单 ============ */
.form-row{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
.form-row label{font-size:13px;color:var(--text-dim);min-width:60px}
.form-row input,.form-row select,.form-row textarea{
  background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;
  color:var(--text);padding:8px 12px;font-size:13px;outline:none;transition:all 0.2s;flex:1;min-width:120px;
}
.form-row input:focus,.form-row select:focus,.form-row textarea:focus{
  border-color:var(--accent);box-shadow:0 0 12px var(--accent-glow);
}
.form-row textarea{resize:vertical;min-height:80px;font-family:inherit}
.form-row select{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%238892a8'%3E%3Cpath d='M2 4l4 4 4-4'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:32px}
.form-row select option{background:#1a1f2e;color:#e8edf5}
.form-divider{height:1px;background:var(--card-border);margin:16px 0}
/* ============ 按钮 ============ */
.btn{
  display:inline-flex;align-items:center;gap:6px;padding:10px 20px;border-radius:10px;
  font-size:14px;font-weight:500;border:none;cursor:pointer;transition:all 0.25s;
}
.btn-primary{background:linear-gradient(135deg,#5b8def,#4a7ad9);color:#fff;box-shadow:0 4px 14px var(--accent-glow)}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 20px var(--accent-glow)}
.btn-primary:disabled{opacity:0.4;cursor:not-allowed;transform:none}
.btn-danger{background:rgba(239,68,68,0.15);color:var(--red);border:1px solid rgba(239,68,68,0.2)}
.btn-danger:hover{background:rgba(239,68,68,0.25)}
.btn-ghost{background:var(--surface);color:var(--text-dim);border:1px solid var(--card-border)}
.btn-ghost:hover{background:var(--surface-hover);color:var(--text)}
.btn-sm{padding:6px 12px;font-size:12px}
.btn-group{display:flex;gap:10px;flex-wrap:wrap}
/* ============ 进度 ============ */
.progress-wrap{background:rgba(255,255,255,0.06);border-radius:10px;height:8px;overflow:hidden;margin:12px 0;position:relative}
.progress-bar{height:100%;border-radius:10px;background:linear-gradient(90deg,var(--accent),var(--green));transition:width 0.6s cubic-bezier(.4,0,.2,1);width:0%;position:relative}
.progress-bar::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.2),transparent);animation:shimmer 2s infinite}
@keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
.progress-info{display:flex;justify-content:space-between;font-size:12px;color:var(--text-dim)}
/* ============ 日志 ============ */
.log-box{
  background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.06);border-radius:10px;
  padding:12px;height:280px;overflow-y:auto;font-size:12px;line-height:1.7;font-family:'Cascadia Code','Fira Code','Consolas',monospace;
  margin-top:12px;
}
.log-box::-webkit-scrollbar{width:4px}
.log-box::-webkit-scrollbar-thumb{background:var(--accent);border-radius:2px}
/* ============ 状态徽章 ============ */
.badge{
  display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;
  font-size:12px;font-weight:500;
}
.badge-idle{background:rgba(136,146,168,0.15);color:var(--text-dim)}
.badge-running{background:rgba(91,141,239,0.15);color:var(--accent)}
.badge-done{background:rgba(52,211,153,0.15);color:var(--green)}
.badge-error{background:rgba(239,68,68,0.15);color:var(--red)}
.badge-cancelled{background:rgba(245,158,11,0.15);color:var(--orange)}
/* ============ 下载区 ============ */
.download-box{display:none;margin-top:16px;padding:16px;border:1px solid rgba(52,211,153,0.2);border-radius:12px;background:rgba(52,211,153,0.04)}
.download-box.visible{display:block}
.download-btn{display:inline-flex;align-items:center;gap:8px;padding:12px 24px;background:linear-gradient(135deg,#34d399,#2bb07f);color:#fff;border-radius:10px;text-decoration:none;font-weight:500;font-size:15px;transition:all 0.25s;box-shadow:0 4px 14px var(--green-glow)}
.download-btn:hover{transform:translateY(-2px);box-shadow:0 6px 24px var(--green-glow)}
/* ============ 历史记录 ============ */
.history-list{max-height:320px;overflow-y:auto;margin-top:8px}
.history-list::-webkit-scrollbar{width:4px}
.history-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:2px}
.history-item{
  display:flex;align-items:center;justify-content:space-between;padding:10px 12px;
  border-radius:8px;margin-bottom:4px;cursor:pointer;transition:background 0.2s;gap:8px;
}
.history-item:hover{background:var(--surface)}
.history-item .h-name{font-size:13px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.history-item .h-time{font-size:11px;color:var(--text-dim);min-width:60px;text-align:right}
.history-item .h-state{font-size:11px}
/* ============ 章节预览 ============ */
.chapter-list{margin-top:12px;max-height:200px;overflow-y:auto}
.chapter-item{
  padding:6px 10px;font-size:12px;border-left:2px solid var(--accent);margin-bottom:4px;
  background:rgba(91,141,239,0.04);border-radius:0 6px 6px 0;color:var(--text-dim);
  cursor:pointer;transition:all 0.2s;
}
.chapter-item:hover{background:rgba(91,141,239,0.1);color:var(--text)}
/* ============ 响应式 ============ */
@media(max-width:900px){
  .app{padding:16px;margin-bottom:80px}
  .form-row{flex-direction:column;align-items:stretch}
  .form-row label{min-width:auto}
}
/* ============ Toast ============ */
.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:12px 24px;
  border-radius:12px;font-size:14px;z-index:999;opacity:0;transition:all 0.4s;
  pointer-events:none;backdrop-filter:blur(12px);
}
.toast.show{opacity:1;pointer-events:auto}
.toast-info{background:rgba(91,141,239,0.2);border:1px solid rgba(91,141,239,0.3);color:var(--accent)}
.toast-success{background:rgba(52,211,153,0.2);border:1px solid rgba(52,211,153,0.3);color:var(--green)}
.toast-error{background:rgba(239,68,68,0.2);border:1px solid rgba(239,68,68,0.3);color:var(--red)}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>📖 TTS 有声书工坊</h1>
    <span class="header-badge">Edge-TTS · 中文有声书</span>
  </div>

  <div class="grid">
    <!-- ===== 左侧主面板 ===== -->
    <div class="main-panel">
      <!-- 上传区 -->
      <div class="card">
        <div class="card-title">📂 选择电子书</div>
        <div class="drop-zone" id="dropZone">
          <div class="drop-zone-icon">📄</div>
          <div class="drop-zone-text">拖拽 TXT 文件到此处，或<span>点击选择文件</span></div>
          <input type="file" id="fileInput" accept=".txt">
        </div>
        <div id="fileInfo" style="display:none;margin-top:10px;padding:10px 14px;background:rgba(91,141,239,0.06);border-radius:8px;font-size:13px">
          ✅ 已选择：<span id="fileName"></span> <span id="fileSize" style="color:var(--text-dim)"></span>
          <button class="btn btn-ghost btn-sm" onclick="clearFile()" style="margin-left:8px">移除</button>
        </div>
      </div>

      <!-- 或粘贴文本 -->
      <div class="card">
        <div class="card-title">✍️ 或直接粘贴文本</div>
        <textarea id="pasteText" rows="6" style="width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);padding:12px;font-size:13px;font-family:inherit;outline:none;resize:vertical" placeholder="粘贴小说/文章内容..."></textarea>
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="detectChaptersFromText()">📑 检测章节</button>
          <span id="charCount" style="font-size:12px;color:var(--text-dim);align-self:center">0 字</span>
        </div>
        <div id="chapterPreview" class="chapter-list" style="display:none"></div>
      </div>

      <!-- 设置 -->
      <div class="card">
        <div class="card-title">⚙️ 合成设置</div>
        <div class="form-row">
          <label>音色</label>
          <select id="voiceSel" style="flex:1">'''
    for v_id, v_name in VOICE_LIST:
        sel = ' selected' if v_id == DEFAULT_VOICE else ''
        html += f'<option value="{v_id}"{sel}>{v_name}</option>\n'
    html += r'''</select>
          <button class="btn btn-ghost btn-sm" onclick="previewVoice()">🔊 试听</button>
        </div>
        <div class="form-row">
          <label>语速</label>
          <input id="rateInput" value="+20%" style="flex:1" placeholder="例：+20% / -10% / +0%">
          <span style="font-size:11px;color:var(--text-dim)">-50% ~ +50%</span>
        </div>
        <div class="form-row">
          <label>任务名</label>
          <input id="taskName" placeholder="选填，默认用文件名或时间戳" style="flex:1">
        </div>
        <div class="form-divider"></div>
        <div class="btn-group">
          <button class="btn btn-primary" id="startBtn" onclick="startTask()">🚀 开始合成</button>
          <button class="btn btn-danger" id="cancelBtn" style="display:none" onclick="cancelTask()">⏹ 取消任务</button>
        </div>
      </div>

      <!-- 进度 -->
      <div class="card" id="progressCard" style="display:none">
        <div class="card-title">📊 合成进度 <span id="stateBadge" class="badge badge-idle">等待中</span></div>
        <div class="progress-wrap"><div class="progress-bar" id="progressBar"></div></div>
        <div class="progress-info">
          <span id="progressText">0%</span>
          <span id="progressSeg" style="color:var(--text-dim)">0/0 段</span>
        </div>
        <div class="log-box" id="logBox"></div>
      </div>

      <!-- 下载 -->
      <div class="card">
        <div class="download-box" id="downloadBox">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
            <span style="font-size:20px">🎉</span>
            <div>
              <div style="font-weight:500">有声书已生成！</div>
              <div style="font-size:12px;color:var(--text-dim)" id="downloadFileName"></div>
            </div>
          </div>
          <a class="download-btn" href="/download_full" download id="downloadLink">⬇ 下载完整MP3</a>
        </div>
      </div>
    </div>

    <!-- ===== 右侧面板 ===== -->
    <div class="side-panel">
      <!-- 状态 -->
      <div class="card">
        <div class="card-title">🔵 当前状态</div>
        <div style="font-size:13px;color:var(--text-dim)" id="statusText">等待任务...</div>
      </div>

      <!-- 历史记录 -->
      <div class="card">
        <div class="card-title" style="cursor:pointer" onclick="loadHistory()">🕐 历史记录 <span style="font-size:11px;color:var(--text-dim);margin-left:auto">点击刷新</span></div>
        <div class="history-list" id="historyList"></div>
      </div>

      <!-- 服务器文件 -->
      <div class="card">
        <div class="card-title">📁 服务器文件</div>
        <div style="margin-bottom:8px">
          <input id="scanDir" value="./output" placeholder="目录路径" style="width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);padding:8px 12px;font-size:12px;outline:none">
          <button class="btn btn-ghost btn-sm" onclick="scanServerFiles()" style="margin-top:6px">🔍 扫描</button>
        </div>
        <div id="serverFiles" style="max-height:200px;overflow-y:auto;font-size:12px"></div>
      </div>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
// ============ 状态变量 ============
let pollTimer = null;
let currentFile = null;

// ============ Toast ============
function showToast(msg, type='info'){
  const t = document.getElementById('toast');
  t.className = 'toast toast-'+type+' show';
  t.textContent = msg;
  clearTimeout(t._hide);
  t._hide = setTimeout(()=>t.classList.remove('show'), 3000);
}

// ============ 拖拽上传 ============
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');

dropZone.addEventListener('dragover', e=>{e.preventDefault(); dropZone.classList.add('drag-over')});
dropZone.addEventListener('dragleave', ()=>dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e=>{
  e.preventDefault(); dropZone.classList.remove('drag-over');
  if(e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e=>{if(e.target.files.length) handleFile(e.target.files[0])});

function handleFile(file){
  if(!file.name.endsWith('.txt')){showToast('请选择TXT格式文件','error');return}
  currentFile = file;
  document.getElementById('fileInfo').style.display = 'block';
  document.getElementById('fileName').textContent = file.name;
  document.getElementById('fileSize').textContent = '('+(file.size/1024).toFixed(1)+' KB)';
  document.getElementById('pasteText').value = '';
  document.getElementById('charCount').textContent = '0 字';
  showToast('已选择文件：'+file.name,'success');
}
function clearFile(){
  currentFile = null;
  document.getElementById('fileInfo').style.display = 'none';
  fileInput.value = '';
}

// ============ 字数统计 ============
document.getElementById('pasteText').addEventListener('input', function(){
  document.getElementById('charCount').textContent = this.value.length + ' 字';
});

// ============ 试听音色 ============
async function previewVoice(){
  const voice = document.getElementById('voiceSel').value;
  const rate = document.getElementById('rateInput').value || '+20%';
  try{
    const r = await fetch('/api/preview_voice?voice='+encodeURIComponent(voice)+'&rate='+encodeURIComponent(rate)+'&text='+encodeURIComponent('你好，欢迎使用有声书工坊。这是一段语音预览，请选择您喜欢的音色和语速。'));
    if(!r.ok) throw Error('预览失败');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.play();
    showToast('🔊 正在播放语音预览...','info');
  }catch(e){
    showToast('试听失败：'+e.message,'error');
  }
}

// ============ 检测章节 ============
async function detectChaptersFromText(){
  const text = document.getElementById('pasteText').value;
  if(!text.trim()){showToast('请先粘贴文本内容','error');return}
  try{
    const r = await fetch('/api/detect_chapters',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:text.slice(0,50000)})
    });
    // fallback: use GET
    const r2 = await fetch('/api/detect_chapters?text='+encodeURIComponent(text.slice(0,50000)));
    const d = await r2.json();
    const ch = d.chapters || [];
    const container = document.getElementById('chapterPreview');
    if(ch.length>1){
      container.style.display = 'block';
      container.innerHTML = '<div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">检测到 '+ch.length+' 个章节：</div>'+
        ch.map(c=>'<div class="chapter-item">'+escHtml(c.title)+'</div>').join('');
      showToast('📑 检测到 '+ch.length+' 个章节','success');
    }else{
      container.style.display = 'none';
      showToast('未检测到章节结构，将按全文处理','info');
    }
  }catch(e){showToast('章节检测失败','error')}
}

function escHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

// ============ 扫描服务器文件 ============
async function scanServerFiles(){
  const dir = document.getElementById('scanDir').value || './output';
  try{
    const r = await fetch('/api/scan_txt?base_dir='+encodeURIComponent(dir));
    const d = await r.json();
    const files = d.files || [];
    const container = document.getElementById('serverFiles');
    if(!files.length){
      container.innerHTML = '<div style="color:var(--text-dim);font-size:12px">📭 未找到TXT文件</div>';
      return;
    }
    container.innerHTML = files.map(f=>
      '<div class="history-item" onclick="loadServerFile(\''+escHtml(f.path)+'\')">'+
        '<span class="h-name">📄 '+escHtml(f.path)+'</span>'+
        '<span class="h-time">'+(f.size/1024).toFixed(0)+'KB</span>'+
      '</div>'
    ).join('');
    showToast('找到 '+files.length+' 个TXT文件','success');
  }catch(e){showToast('扫描失败','error')}
}

async function loadServerFile(path){
  try{
    const r = await fetch('/api/read_txt?file_path='+encodeURIComponent(path));
    const d = await r.json();
    if(d.code!==0){showToast(d.msg,'error');return}
    document.getElementById('pasteText').value = d.content;
    document.getElementById('charCount').textContent = d.content.length+' 字';
    document.getElementById('taskName').value = d.name.replace('.txt','');
    clearFile();
    showToast('已加载：'+d.name,'success');
  }catch(e){showToast('加载失败','error')}
}

// ============ 开始合成 ============
async function startTask(){
  const txtFile = currentFile;
  const text = document.getElementById('pasteText').value;
  const voice = document.getElementById('voiceSel').value;
  const rate = document.getElementById('rateInput').value || '+20%';
  const taskName = document.getElementById('taskName').value;
  const startBtn = document.getElementById('startBtn');
  const cancelBtn = document.getElementById('cancelBtn');

  if(!txtFile && !text.trim()){showToast('请上传文件或粘贴文本','error');return}

  const fd = new FormData();
  if(txtFile) fd.append('txtFile', txtFile);
  if(text.trim()) fd.append('text', text);
  fd.append('voice', voice);
  fd.append('rate', rate);
  if(taskName.trim()) fd.append('task_name_input', taskName.trim());

  try{
    const r = await fetch('/api/start',{method:'POST',body:fd});
    const d = await r.json();
    if(d.code!==0){showToast(d.msg,'error');return}
    showToast(d.msg,'success');
    startBtn.disabled = true;
    cancelBtn.style.display = 'inline-flex';
    document.getElementById('progressCard').style.display = 'block';
    document.getElementById('downloadBox').classList.remove('visible');
    startPolling();
  }catch(e){showToast('启动失败：'+e.message,'error')}
}

// ============ 取消任务 ============
async function cancelTask(){
  try{
    const r = await fetch('/api/cancel',{method:'POST'});
    const d = await r.json();
    showToast(d.msg,d.code===0?'info':'error');
  }catch(e){showToast('取消失败','error')}
}

// ============ 轮询 ============
function startPolling(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async ()=>{
    try{
      const r = await fetch('/api/get_runtime');
      const d = await r.json();
      updateUI(d);
      if(d.state === 'done' || d.state === 'error' || d.state === 'cancelled'){
        clearInterval(pollTimer);
        pollTimer = null;
        document.getElementById('startBtn').disabled = false;
        document.getElementById('cancelBtn').style.display = 'none';
        if(d.state === 'done' && d.has_file){
          document.getElementById('downloadBox').classList.add('visible');
          document.getElementById('downloadFileName').textContent = d.task_name || '有声书';
        }
      }
    }catch(e){}
  }, 800);
}

function updateUI(d){
  // badges
  const badge = document.getElementById('stateBadge');
  const states = {idle:'badge-idle',running:'badge-running',done:'badge-done',error:'badge-error',cancelled:'badge-cancelled'};
  badge.className = 'badge '+(states[d.state]||'badge-idle');
  const labels = {idle:'等待中',running:'运行中',done:'已完成',error:'出错',cancelled:'已取消'};
  badge.textContent = labels[d.state]||d.state;

  // status
  document.getElementById('statusText').textContent = d.status || '—';

  // progress
  document.getElementById('progressBar').style.width = d.progress+'%';
  document.getElementById('progressText').textContent = d.progress+'%';
  document.getElementById('progressSeg').textContent = (d.total_seg||0)+' 段';

  // log
  const logBox = document.getElementById('logBox');
  if(d.log && d.log.length){
    logBox.innerHTML = d.log.join('<br>');
    logBox.scrollTop = logBox.scrollHeight;
  }
}

// ============ 历史记录 ============
async function loadHistory(){
  try{
    const r = await fetch('/api/history');
    const d = await r.json();
    const list = d.history || [];
    const container = document.getElementById('historyList');
    if(!list.length){
      container.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px">暂无记录</div>';
      return;
    }
    container.innerHTML = list.map(h=>{
      const stateColor = {完成:'var(--green)',失败:'var(--red)',已取消:'var(--orange)'};
      return '<div class="history-item">'+
        '<span class="h-name">📖 '+escHtml(h.name)+'</span>'+
        '<span class="h-state" style="color:'+(stateColor[h.state]||'var(--text-dim)')+'">'+escHtml(h.state)+'</span>'+
        '<span class="h-time">'+escHtml(h.time)+'</span>'+
      '</div>';
    }).join('');
  }catch(e){}
}

// ============ 初始化 ============
loadHistory();
scanServerFiles();
</script>
</body>
</html>'''
    return html


if __name__ == "__main__":
    load_history()
    uvicorn.run(app, host="0.0.0.0", port=8000)
