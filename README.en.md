# findr

A search tool that finds files on your PC from what you remember about them. Everything runs on your own computer.

[한국어](README.md)

This is the 0.2 preview release.

## Why

People rarely remember file names. They remember what happened around a file: the quote a supplier sent last month, the beach photos from last summer, the draft right before the final one. findr takes questions like these as they are and lists candidates in order of how likely each one is to be the file you mean. If nothing fits, it says so and still shows the closest candidates.

findr is built Korean-first (Korean phrasing, HWP documents, local office conventions) and also handles English names and text.

```
> 작년 여름 휴가 때 찍은 바다 사진      (beach photos from last summer's vacation)
  1  92%  IMG_5082.jpg      2025-08-12
  2  71%  IMG_5090.jpg      2025-08-13
  3  38%  KakaoTalk_...jpg  2025-08-20
```

## What it does

- Reads content, time, people and actions in a question together, including phrases such as "last August", "during winter break", "edited before the meeting" or "the one Mr. Kim sent".
- Finds photos by what is in them ("a cat lying on a laptop") and narrows them by the date they were taken. Requires a GPU.
- Shows how likely each candidate is and why. You make the final choice; findr remembers what you picked and what you marked as wrong, and uses it next time. You can review and erase what it has learned.
- Indexes the whole PC but skips system, program and cache folders. Desktop and Downloads come first, so search is usable within minutes.
- Switches to a lighter setup automatically on PCs without a GPU.

Readable formats: PDF, HWP, HWPX, DOC, DOCX, XLSX, PPTX, Keynote, TXT, Markdown, source code and photos (JPG, PNG, WEBP, iPhone HEIC and others). Other files are found by name, folder and date.

## Performance

Measured during development on questions that were never used for tuning. Results depend on the PC and its files.

| Condition | Result |
|---|---|
| 6 unseen synthetic PCs, 288 questions, no activity history | 76% top-1, 90% top-3 |
| A real personal PC, 45 unseen questions | 84% top-1 |
| 240 photos mixed with documents, 47 photo questions | 85% top-1 (document results unchanged) |
| Response time on a GPU PC | about 0.3 s median |
| First index, 2,100 documents, GPU PC | names searchable after ~1 min, contents after ~2.5 min |

## Installation

### Desktop app (recommended)

Download the file for your system from the [releases page](https://github.com/quantumaikr/findr/releases/latest).

| System | File |
|---|---|
| Windows 10, 11 | `findr_<version>_x64-setup.exe` |
| macOS (Apple silicon) | `findr_<version>_aarch64.dmg` |
| Ubuntu, Debian | `findr_<version>_amd64.deb` (`sudo apt install ./<file>.deb`) |
| Fedora and similar | `findr-<version>-1.x86_64.rpm` |
| Other Linux | `findr_<version>_amd64.AppImage` (make it executable, then run) |

On the first run the app downloads the components and models that suit the PC (5–20 minutes, 3–13 GB), then asks which folders to index.

This preview is not code-signed yet, so the first launch may show a warning:

- Windows: on "Windows protected your PC", choose "More info" → "Run anyway".
- macOS: for "unidentified developer", right-click the app and choose "Open". If macOS says the app "is damaged and can't be opened", move it to Applications and run `xattr -cr /Applications/findr.app` once in Terminal (it clears the downloaded-from-internet flag).
- Linux AppImage: `chmod +x findr_*.AppImage`, then run it. Requires Ubuntu 22.04 or later.

### Command line

You need git and about 10 GB of free disk space. The installer sets up its own Python, so there is no need to install one first. The internet is used only to download models on the first run.

Windows (PowerShell):

```powershell
git clone https://github.com/quantumaikr/findr
cd findr
powershell -ExecutionPolicy Bypass -File install.ps1
```

macOS, Linux:

```bash
git clone https://github.com/quantumaikr/findr && cd findr
bash install.sh
```

The script creates a dedicated Python environment, checks for a GPU and downloads the matching models. No sign-in is required.

## Usage

Run `.\findr` on Windows or `./findr.sh` on macOS and Linux, from the repository folder.

```
findr index                        index your own files across the PC (later runs pick up changes only)
findr index D:\docs E:\photos      index only these folders
findr serve                        open the search page (http://127.0.0.1:8096)
findr ask "report I edited last week"   search from the terminal
findr setup                        re-download models or change the setup (--lite, --photo)
findr doctor                       check the installation and the index
```

You can search while indexing is still running. The search page is password-protected; `findr serve` creates a password on its first run and prints it. Any user name works.

## Differences by hardware

| | NVIDIA GPU | Apple silicon Mac | No GPU |
|---|---|---|---|
| Content search | high accuracy | high accuracy | lightweight |
| Candidate comparison step | on | on | skipped |
| Photo content search | on (10 GB+ GPU memory) | name and date only | name and date only |
| Model download | about 5 GB, about 12 GB with photos | about 5 GB | about 2 GB |

## Data and privacy

File contents, questions and search history are never sent off the PC. Everything findr creates is kept in one folder, `%LOCALAPPDATA%\findr` on Windows and `~/.findr` on macOS and Linux; deleting it removes all of it. Searched files are only read, never modified. Passwords, certificates and key files are not indexed. On a shared PC only the signed-in user's folders are indexed.

## Known limitations

- History such as who sent a file or when it was printed can only be checked if the PC keeps such records. Without them findr searches by content and notes that the history could not be confirmed.
- With thousands of photos, a broad request such as "donut photo" can match many pictures. Adding a time ("last spring") makes results much more precise.
- On PCs without a GPU the precise re-comparison step is slower, so answers can take 1–2 seconds.

## License

Apache License 2.0. Models downloaded during setup follow their publishers' licenses. Report security issues as described in [SECURITY.md](SECURITY.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
