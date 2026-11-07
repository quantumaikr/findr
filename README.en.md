<p align="center">
  <img src="assets/logo.svg" width="88" alt="findr">
</p>

<h1 align="center">findr</h1>

<p align="center">
  <b>Ask your PC the way you remember.</b><br>
  Natural-language file and photo search that runs entirely on your own computer
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.2.0-2563eb">
  <img alt="status" src="https://img.shields.io/badge/status-private%20preview-f59e0b">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-475569">
  <img alt="privacy" src="https://img.shields.io/badge/data-stays%20on%20your%20PC-16a34a">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-64748b">
</p>

<p align="center">
  <a href="README.md">한국어</a> · <b>English</b>
</p>

---

## Overview

When we look for a file, we rarely remember its name. We remember **what happened**: "the quote the supplier sent last month",
"the beach photos from last summer", "not the final version, the one right before it".
findr understands requests like these, finds the files on your PC, and lists them **ordered by how likely each one is to be the one you want**.
When nothing really matches, it **says so** instead of handing you the wrong file.

findr is designed Korean-first (Korean phrasing, HWP documents, Korean office conventions) and also handles English file names and text.

```text
(example)
> 작년 여름 휴가 때 찍은 바다 사진        ("beach photos from last summer's vacation")
  1. 92%  IMG_5082.jpg     2025-08-12   photo content matches
  2. 71%  IMG_5090.jpg     2025-08-13   photo content matches
  3. 38%  KakaoTalk_...jpg 2025-08-20   photo content partly matches
```

## Features

| | |
|---|---|
| 🗣️ **Ask as you remember** | Understands content, time, people and actions together: "last August", "during winter break", "edited before the meeting", "the one Mr. Kim sent" |
| 🖼️ **Photos by what is in them** | "a cat lying on a laptop", "two people holding a cake", narrowed by the date the photo was taken |
| 🎯 **Odds and reasons** | Every candidate shows how likely it is to be right and why; candidates stay available across pages, and you make the final call |
| 🧠 **Learns you** | Remembers what you picked and what you marked "✘ not this", so results fit you better over time. You can review and erase what it learned |
| 🙅 **Says "not here"** | If no file fits what you remember, it tells you, and still shows the closest candidates |
| 🗂️ **Your files, whole PC** | Skips system, program and cache folders and indexes only what people create or receive, starting with Desktop and Downloads, so you can search within minutes |
| 🔒 **Everything stays local** | File contents and questions never leave your PC. The search page is password-protected |
| 💻 **No GPU required** | Picks the right setup for your hardware automatically |

Supported formats: PDF · HWP · HWPX · DOC · DOCX · XLSX · PPTX · TXT · Markdown · source code · photos (JPG · PNG · WEBP, …). Other files are found by name, folder and date.

## Performance

| Measurement | Result |
|---|---|
| 6 unseen synthetic PCs, 288 questions (ordinary PCs with no activity history) | **76% top-1**, 90% top-3 |
| A real personal PC, 45 unseen questions | **84% top-1** |
| Photo search (240 unseen photos mixed with documents) | **85% top-1**; document results unchanged with photo search on |
| Response time (GPU PC) | about 0.3 s median |
| First index (PC with 2,100 documents, GPU) | names searchable in ~1 min, contents in ~2.5 min |

> Development measurements; results vary with hardware and files. All numbers come from questions that were never used for tuning.

## Installation

**Requirements:** git, an internet connection (first install only), about 10 GB of free disk space. The installer sets up Python by itself.

<details open>
<summary><b>Windows</b></summary>

```powershell
git clone https://github.com/quantumaikr/findr
cd findr
powershell -ExecutionPolicy Bypass -File install.ps1
```
</details>

<details>
<summary><b>macOS · Linux</b></summary>

```bash
git clone https://github.com/quantumaikr/findr && cd findr
bash install.sh
```
</details>

The installer creates a dedicated Python environment, detects your GPU, and downloads the matching setup and models. No sign-in is needed.

## Usage

```bash
findr index          # index your own files across the PC (first run; later runs pick up changes only)
findr serve          # search page → http://127.0.0.1:8096
```

| Command | What it does |
|---|---|
| `findr index` | Index your files across the PC. You can search while it runs |
| `findr index D:\docs E:\photos` | Index only the folders you choose |
| `findr serve` | Open the search page. The first run creates a password and shows it |
| `findr ask "report I edited last week"` | Search straight from the terminal |
| `findr setup` | Re-download models or change the setup (`--lite`, `--photo`) |
| `findr doctor` | Check the installation and index |

> Run it as `.\findr` (Windows) or `./findr.sh` (macOS · Linux) from the repository folder.

## Setup by hardware

| | NVIDIA GPU | Apple silicon Mac | No GPU |
|---|:---:|:---:|:---:|
| Content (meaning) search | ● high accuracy | ● high accuracy | ● lightweight |
| Side-by-side candidate check | ● | ● | – |
| Photo content search | ● (10 GB+ GPU memory) | name & date | name & date |
| Model download | ~5 GB (+7 GB for photos) | ~5 GB | ~2 GB |

## Privacy and data

- File contents, questions and search history **never leave your PC**. The internet is used only to download models during setup.
- Everything findr creates lives in one folder: `%LOCALAPPDATA%\findr` on Windows, `~/.findr` on macOS and Linux. Delete it and findr leaves nothing behind.
- Files are **only read, never changed**. Sensitive files such as passwords, keys and certificates are not indexed.
- On a shared PC, only the **current user's folders** are indexed.

## Known limitations

- History such as "who sent it" or "when it was printed" can be checked only when the PC keeps such records. Without them, findr searches by content and tells you so.
- With thousands of photos, a broad request like "donut photo" can match many pictures. Adding a time ("last spring") makes it much more precise.
- On PCs without a GPU, the precise comparison step is slower and answers may take 1–2 seconds.

## License

Apache License 2.0. Downloaded models follow the licenses of their publishers.
Please report security issues privately as described in [SECURITY.md](SECURITY.md). Changes are listed in the [CHANGELOG](CHANGELOG.md).
