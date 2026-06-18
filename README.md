# 📖 TTS 有声书工坊

> 将 TXT 电子书一键转换为有声书的 Docker Web 服务，基于 Edge-TTS，支持中文语音合成。

[![GitHub release](https://img.shields.io/github/v/release/jiayoupengpeng/TTS-Audiobook)](https://github.com/jiayoupengpeng/TTS-Audiobook/releases)
[![Build and Push Docker Image](https://github.com/jiayoupengpeng/TTS-Audiobook/actions/workflows/build.yml/badge.svg)](https://github.com/jiayoupengpeng/TTS-Audiobook/actions/workflows/build.yml)
[![Docker Pulls](https://img.shields.io/badge/docker-ghcr.io-blue)](https://github.com/jiayoupengpeng/TTS-Audiobook/pkgs/container/tts-audiobook)

---

## ✨ 功能特点

- 🎯 **上传 TXT → 收听 MP3** — 三步完成：上传文件 → 选择音色 → 开始合成
- 🎭 **多种中文音色** — 晓晓（温暖女声）、云扬（新闻男声）、云希（阳光男声）等 6 种高质量 AI 语音
- ⏱ **实时进度追踪** — 可视化进度条 + 分段计数 + 详细运行日志
- 🔄 **断点续传** — 意外中断后重新运行自动跳过已生成的分段
- 📑 **智能章节检测** — 自动识别 "第一章 / Chapter 1 / 1. 标题" 等章节结构
- 🔊 **音色在线试听** — 合成前先听效果，找到最合适的声线
- 🗂 **服务器文件浏览** — 直接选择服务器上已有的 TXT 文件进行转换
- 📋 **合成历史记录** — 自动保存历史任务，方便回溯
- ⏹ **任务取消** — 运行中的任务可随时取消
- 📱 **响应式界面** — 桌面 / 平板 / 手机均友好适配
- 🐳 **一键 Docker 部署** — 开箱即用，无需复杂配置

## 🚀 快速开始

### 方式一：Docker 部署（推荐）

```bash
# 拉取最新镜像并启动
docker pull ghcr.io/jiayoupengpeng/tts-audiobook:latest
docker run -d \
  --name tts-audiobook \
  -p 8080:8000 \
  -v $(pwd)/output:/app/output \
  --restart unless-stopped \
  ghcr.io/jiayoupengpeng/tts-audiobook:latest
```

或者使用 docker-compose：

```bash
# 克隆仓库
git clone https://github.com/jiayoupengpeng/TTS-Audiobook.git
cd TTS-Audiobook

# 启动服务
docker compose up -d
```

### 方式二：本地运行

```bash
# 1. 克隆仓库
git clone https://github.com/jiayoupengpeng/TTS-Audiobook.git
cd TTS-Audiobook

# 2. 安装依赖（需要 Python 3.10+）
pip install -r requirements.txt

# 3. 确保已安装 ffmpeg（用于合并音频）
# Windows: choco install ffmpeg 或 winget install ffmpeg
# macOS:   brew install ffmpeg
# Linux:   apt install ffmpeg

# 4. 启动服务
python main.py
```

### 访问

打开浏览器访问 **http://localhost:8080**（Docker）或 **http://localhost:8000**（本地运行）。

> ⚠️ Windows 用户如果本地端口被占用，可修改 `main.py` 中的端口号。

### 本地构建镜像

```bash
docker compose up -d --build
```

## 🎯 使用指南

### 基本流程

1. **上传 TXT 文件** — 拖拽或点击选择 .txt 文件（UTF-8 编码）
2. **或粘贴文本** — 直接复制粘贴书籍内容
3. **选择音色** — 从下拉列表选择喜欢的语音风格，点击 🔊 试听
4. **调整语速** — 默认 `+20%`，范围 `-50%` ~ `+50%`
5. **点击"开始合成"** — 等待进度条走完
6. **下载完整 MP3** — 完成后点击绿色下载按钮

### 章节检测

支持以下章节格式自动识别：
- `第一章` / `第1章` / `第100章`
- `Chapter 1` / `CHAPTER 1`
- `1. 标题`（数字编号）
- `第一节` / `第一篇` / `第一回` / `第一部` / `第一卷`

### 断点续传

如果合成过程中中断（如网络波动、服务重启），重新执行相同任务名时会自动跳过已生成的分段，只合成缺失的部分。

## 🎭 音色列表

| 音色 ID | 名称 | 风格 | 适合场景 |
|---------|------|------|---------|
| `zh-CN-XiaoxiaoNeural` | 晓晓 🎀 | 女声·温暖 | 小说、散文、日常阅读 |
| `zh-CN-XiaoyiNeural` | 晓艺 🌸 | 女声·活泼 | 儿童故事、轻松内容 |
| `zh-CN-YunyangNeural` | 云扬 📰 | 男声·新闻 | 历史、社科、正式内容 |
| `zh-CN-YunxiNeural` | 云希 ☀️ | 男声·阳光 | 文学、通用 |
| `zh-CN-YunjianNeural` | 云健 🔥 | 男声·激情 | 热血小说、动作 |
| `zh-CN-YunxiaNeural` | 云夏 🎀 | 女声·可爱 | 童话、温柔故事 |

## 📁 项目结构

```
TTS-Audiobook/
├── main.py                  # 主程序（FastAPI + 前端页面）
├── Dockerfile               # Docker 构建配置
├── docker-compose.yml       # Docker Compose 配置
├── requirements.txt         # Python 依赖
├── .dockerignore            # Docker 忽略文件
├── .gitignore               # Git 忽略文件
├── .github/workflows/
│   └── build.yml           # GitHub Actions：自动构建 Docker 镜像
└── output/                  # 输出目录（自动创建）
    └── {任务名}/
        ├── part_000.mp3     # 分段音频
        ├── part_001.mp3
        ├── ...
        └── {任务名}_完整音频.mp3  # 合并后的完整有声书
```

## ⚙️ 技术栈

- **后端框架**：FastAPI + Uvicorn
- **语音合成**：Microsoft Edge-TTS
- **音频处理**：ffmpeg
- **前端**：原生 HTML + CSS + JavaScript（暗色玻璃质感主题）
- **容器化**：Docker + GitHub Container Registry (GHCR)

## 🧪 本地开发

```bash
# 安装开发依赖
pip install -r requirements.txt

# 热重载启动（代码修改后自动重启）
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## 🤝 贡献

欢迎通过 Issue 提交建议或 Pull Request 贡献代码！

## 📄 License

MIT
