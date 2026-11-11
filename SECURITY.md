# Security

findr reads a person's own files. This page says what it does with them, what it does not protect against, and how to report a problem. [한국어 요약](#한국어-요약)은 아래에 있습니다.

## What findr does
- **Everything runs on the PC.** File contents, questions and search history are not sent anywhere. The internet is used to download models and the engine during setup and updates, and for nothing else.
- **Files are only read.** findr never changes, moves or deletes the files it searches.
- **Secret files are not read.** Files such as `.env`, `.netrc`, SSH keys, certificates and key stores (`*.pem`, `*.pfx`, `*.p12`, `*.kdbx` and similar) are skipped by name. Folders such as `.ssh`, `.aws`, `.kube` and `.docker` and shell history are never indexed.
- **Key-like strings are masked.** Text that looks like an API key or token is replaced before it is stored in the index.
- **The search page is local and needs a password.** It listens on `127.0.0.1` only. Repeated wrong passwords are refused for a while. Responses carry a content security policy, forbid framing and are not cached.
- **One data folder.** The index, models, settings, logs and what findr learned from use are in `%LOCALAPPDATA%\findr` (Windows) or `~/.findr` (macOS, Linux). Uninstalling removes it; see the README.

## What findr does not do
- **The index is not encrypted.** It holds pieces of document text and is protected only by the file permissions of the user account. Against a lost or stolen PC, use the operating system's disk encryption (BitLocker, FileVault, LUKS).
- **Other programs running as the same user can read the index**, as they can read the files themselves.
- **Installers are not code-signed yet.** Download them only from this repository's releases page.

## AI tools (MCP)
When an AI assistant uses findr through MCP, the file names, locations and excerpts it asks for go to that assistant. If the assistant runs in the cloud, they leave the PC there. Set the assistant to ask before each use of a tool. `read_file` returns only text of files already in the index.

## Reporting a vulnerability
Please do not open a public issue. Use GitHub's **Security → Report a vulnerability** on this repository. We answer within 7 days.

## 한국어 요약
- 모든 처리는 PC 안에서 합니다. 인터넷은 설치·업데이트 때 모델과 엔진을 받는 데만 씁니다.
- 검색 대상 파일은 읽기만 합니다. 비밀 파일(`.env`, SSH 키, 인증서 등)은 읽지 않고, 키처럼 보이는 문자열은 가려서 저장합니다.
- 검색 화면은 이 PC(127.0.0.1)에서만 열리고 비밀번호가 필요합니다.
- **색인은 암호화되어 있지 않습니다.** PC 분실에 대비하려면 운영체제의 디스크 암호화(BitLocker, FileVault)를 쓰세요.
- AI 도구(MCP)에 연결하면 찾은 파일의 이름과 내용 일부가 그 AI 도구로 전달됩니다. 클라우드 AI라면 PC 밖으로 나갑니다.
- 취약점은 공개 이슈 대신 GitHub의 **Security → Report a vulnerability**로 알려 주세요. 7일 안에 답합니다.
