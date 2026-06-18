# 📖 TTS 有声书工坊

> 将 TXT 电子书一键转换为有声书的 Docker Web 服务，基于 Edge-TTS，支持中文语音合成。

[![Build](https://github.com/jiayoupengpeng/TTS-Audiobook/actions/workflows/build.yml/badge.svg)](https://github.com/jiayoupengpeng/TTS-Audiobook/actions/workflows/build.yml)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://github.com/jiayoupengpeng/TTS-Audiobook/pkgs/container/tts-audiobook)

---

## ✨ 功能特点

- 🎯 **三步完成** — 上传 TXT → 选音色 → 生成有声书
- 🎭 **6 种中文 AI 音色** — 卡片式选择，支持在线试听
- 🔄 **实时环形进度** — 动画进度环 + 百分比 + 分段计数
- 📑 **智能章节检测** — 自动识别"第一章/Chapter 1/1. 标题"等结构
- 🗂 **服务器文件浏览** — 直接选择 NAS 或服务器上的 TXT 文件
- 📋 **合成历史** — 自动保存历史记录，重启不丢失
- ⏹ **任务取消** — 运行中的任务可随时取消
- 📱 **响应式设计** — 桌面 / 平板 / 手机均完美适配
- 🐳 **Docker 一键部署** — 开箱即用

## 🚀 快速开始

### Docker 部署（推荐）

```bash
docker run -d \
  --name=tts-audiobook \
  -v $(pwd)/output:/app/output \
  -p 8080:8000 \
  --restart=unless-stopped \
  ghcr.io/jiayoupengpeng/tts-audiobook:latest
```

或使用 docker-compose：

```bash
git clone https://github.com/jiayoupengpeng/TTS-Audiobook.git
cd TTS-Audiobook
docker compose up -d
```

访问 **http://localhost:8080**

### 本地运行

```bash
pip install -r requirements.txt
# 需要 ffmpeg
python main.py
```

访问 **http://localhost:8000**

## 🎯 使用指南

1. **上传 TXT** — 拖拽或点击选择 `.txt` 文件（UTF-8 编码）
2. **或粘贴文本** — 直接粘贴书籍内容
3. **选择音色** — 点击卡片选择，点击 🔊 在线试听
4. **调整语速** — 默认 `+20%`，范围 `-50%` ~ `+50%`
5. **点击"开始合成"** — 实时进度追踪
6. **下载 MP3** — 完成后点击金色下载按钮

### 章节检测

支持自动识别：`第一章` / `第1章` / `Chapter 1` / `1. 标题` / `第一节` / `第一回`

## 🎭 音色一览

| 图标 | 名称 | 风格 | 场景 |
|:---:|------|------|------|
| 🎀 | 晓晓 | 女声·温暖 | 小说、散文 |
| 🌸 | 晓艺 | 女声·活泼 | 儿童故事 |
| 📰 | 云扬 | 男声·新闻 | 历史、社科 |
| ☀️ | 云希 | 男声·阳光 | 文学、通用 |
| 🔥 | 云健 | 男声·激情 | 热血小说 |
| 🎀 | 云夏 | 女声·可爱 | 童话、温柔 |

## 📁 项目结构

```
├── main.py                 # 主程序
├── Dockerfile              # 构建配置
├── docker-compose.yml      # Compose 配置
├── .github/workflows/
│   └── build.yml          # CI/CD 自动构建
└── output/                 # 输出目录
    └── {书名}/
        ├── part_000.mp3    # 分段
        └── {书名}_完整音频.mp3
```

## ⚙️ 技术栈

**FastAPI** + **Edge-TTS** + **ffmpeg** + **Docker**

## 📄 License

MIT
