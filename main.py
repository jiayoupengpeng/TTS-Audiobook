import asyncio
import os
import re
import edge_tts
from edge_tts.communicate import split_text_by_byte_length
import random
import datetime
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn
from typing import List

# ===================== 【完全保留你原版配置，未做任何修改】=====================
DEFAULT_VOICE = "zh-CN-XiaoyiNeural"
DEFAULT_RATE = "+20%"
MAX_CHARS = 4000
# 批次控制（仅新增，不改动你原有合成逻辑）
BATCH_SIZE = 30
BATCH_SLEEP_MIN = 12
BATCH_SLEEP_MAX = 20

app = FastAPI(title="Edge-TTS 有声书合成")

# 全局状态
log_list: List[str] = []
task_status_msg = "等待任务"
task_run_state = "idle"  # idle / running / done / error
current_task_name = ""
current_root_dir = ""
current_total_seg = 0
final_mp3_path = ""

def add_log(msg: str):
    global log_list
    log_list.append(msg)
    if len(log_list) > 500:
        log_list = log_list[-300:]

# ===================== 【100% 沿用你原版语义分割函数，未改动】=====================
def split_semantic(text: str) -> list:
    sentences = re.split(r'(?<=[。！？])', text)
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

# ===================== 【100% 沿用你原版单段生成函数，未改动】=====================
async def gen_one(text: str, out_path: str, idx: int, total: int, voice: str, rate: str):
    global task_status_msg
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

# ===================== 断点检测（仅新增，用于续传）=====================
def get_finished_parts(out_dir: str, total: int) -> set:
    finished = set()
    for i in range(total):
        part_file = os.path.join(out_dir, f"part_{i:03d}.mp3")
        if os.path.exists(part_file):
            finished.add(i)
    return finished

# ===================== 【100% 沿用你原版ffmpeg合并逻辑，未改动】=====================
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

# ===================== 后台批量任务（分批+最后一批不休眠，核心逻辑不变）=====================
async def main_task(full_text: str, root_dir: str, task_name: str, voice: str, rate: str):
    global task_status_msg, task_run_state, current_task_name
    global current_root_dir, current_total_seg, final_mp3_path

    current_task_name = task_name
    current_root_dir = root_dir
    task_run_state = "running"
    log_list.clear()
    final_mp3_path = ""

    task_dir = os.path.join(root_dir, task_name)
    os.makedirs(task_dir, exist_ok=True)
    add_log(f"📂 任务目录：{task_dir}")

    # 原版分割逻辑
    chunks = split_semantic(full_text)
    total = len(chunks)
    current_total_seg = total
    add_log(f"✅ 文本切分完成，总共 {total} 段")

    # 断点续传
    finished_idx = get_finished_parts(task_dir, total)
    add_log(f"🔍 断点检测：已完成 {len(finished_idx)} 个分片，继续生成")

    try:
        # 分批执行
        for batch_start in range(0, total, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total)
            batch_tasks = []

            for idx in range(batch_start, batch_end):
                if idx in finished_idx:
                    continue
                chunk_text = chunks[idx]
                part_path = os.path.join(task_dir, f"part_{idx:03d}.mp3")
                batch_tasks.append(gen_one(chunk_text, part_path, idx, total, voice, rate))

            if batch_tasks:
                await asyncio.gather(*batch_tasks)

            # 最后一批不再休眠
            is_last_batch = (batch_end >= total)
            if not is_last_batch:
                sleep_t = random.randint(BATCH_SLEEP_MIN, BATCH_SLEEP_MAX)
                add_log(f"⌛ 本批次完成，等待 {sleep_t}s")
                await asyncio.sleep(sleep_t)
            else:
                add_log("✅ 所有分段音频全部生成完毕，开始合并")

        # 原版合并逻辑
        final_mp3 = os.path.join(task_dir, f"{task_name}_完整音频.mp3")
        merge_ok = merge_audio(task_dir, total, final_mp3)
        if not merge_ok:
            raise Exception("音频合并失败")

        final_mp3_path = final_mp3
        task_status_msg = f"🎉 任务【{task_name}】全部执行完成！"
        add_log(task_status_msg)
        task_run_state = "done"

    except Exception as e:
        err_all = f"💥 程序异常终止：{str(e)}"
        task_status_msg = err_all
        add_log(err_all)
        task_run_state = "error"

# ===================== 接口 =====================
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
        "has_file": has_file
    }

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

    # 任务名规则
    task_name = ""
    if task_name_input.strip():
        task_name = task_name_input.strip()
    elif file_name:
        task_name = file_name
    else:
        task_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # 过滤非法文件名
    illegal = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    for c in illegal:
        task_name = task_name.replace(c, "_")

    bg_task.add_task(main_task, full_text, save_dir, task_name, voice, rate)
    return {"code": 0, "msg": f"任务【{task_name}】已启动"}

# ===================== 前端页面（补齐音色下拉、语速输入框，布局完整）=====================
@app.get("/", response_class=HTMLResponse)
async def index():
    html = '''
    <html>
    <head>
        <meta charset="utf-8">
        <title>Edge-TTS 有声书合成</title>
        <style>
            body{padding:15px;font-family:Arial;}
            .box{display:flex;gap:20px;}
            .left{flex:2;}
            .right{flex:1;min-width:280px;}
            #log{height:380px;border:1px solid #ccc;padding:10px;overflow:auto;margin-top:10px;}
            .down{margin-top:15px;padding:10px;border:1px solid green;display:none;}
            .row{margin:8px 0;}
        </style>
    </head>
    <body>
        <h3>Edge-TTS 有声书合成</h3>
        <div class="box">
            <div class="left">
                <form id="f" enctype="multipart/form-data">
                    <div class="row">
                        选择TXT文件：<input type="file" name="txtFile" accept=".txt">
                        <span style="color:#666">自动使用文件名作为任务名</span>
                    </div>
                    <div class="row">
                        粘贴文本：<textarea name="text" rows="8" cols="60" placeholder="二选一即可"></textarea>
                    </div>
                    <div class="row">
                        输出目录：<input name="save_dir" value="./output" style="width:180px;">
                        自定义任务名：<input name="task_name_input" placeholder="选填，优先级最高" style="width:180px;">
                    </div>
                    <div class="row">
                        选择音色：
                        <select id="voiceSel" style="width:200px;">
                            <option value="zh-CN-XiaoyiNeural" selected>晓艺 女声</option>
                            <option value="zh-CN-YunxiNeural">云希 男声</option>
                            <option value="zh-CN-YunjianNeural">云健 男声</option>
                            <option value="zh-CN-YunxiaNeural">云夏 女声</option>
                            <option value="zh-CN-YunyangNeural">云扬 男声</option>
                        </select>
                        <input type="hidden" name="voice" id="voiceInput" value="zh-CN-XiaoyiNeural">
                        语速：<input name="rate" value="+20%" style="width:100px;" placeholder="例：+20% / -10%">
                    </div>
                    <div class="row">
                        <button type="button" onclick="run()">开始合成</button>
                    </div>
                </form>
                <h4>运行日志</h4>
                <div id="log"></div>
            </div>
            <div class="right">
                <h4>任务状态：<span id="stat"></span></h4>
                <div class="down" id="downBox">
                    <h4>✅ 完整音频已生成</h4>
                    <a href="/download_full" download>点击下载 完整MP3</a>
                </div>
            </div>
        </div>
    <script>
        // 音色下拉同步到隐藏输入框
        document.getElementById("voiceSel").addEventListener("change",function(){
            document.getElementById("voiceInput").value = this.value;
        });

        let t = null;
        function poll(){
            if(t) clearInterval(t);
            t = setInterval(async ()=>{
                let res = await fetch("/api/get_runtime");
                let d = await res.json();
                document.getElementById("stat").innerText = d.status;
                document.getElementById("log").innerHTML = d.log.join("<br>");
                document.getElementById("log").scrollTop = document.getElementById("log").scrollHeight;

                if(d.state === "done" || d.state === "error"){
                    clearInterval(t);
                    t = null;
                    if(d.state === "done" && d.has_file){
                        document.getElementById("downBox").style.display = "block";
                    }
                }
            },800)
        }
        async function run(){
            let fd = new FormData(document.getElementById("f"));
            let res = await fetch("/api/start",{method:"POST",body:fd});
            let ret = await res.json();
            if(ret.code!==0){alert(ret.msg);return;}
            document.getElementById("downBox").style.display = "none";
            poll();
        }
    </script>
    </body>
    </html>
    '''
    return html

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)