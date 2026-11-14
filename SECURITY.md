# Security

findr reads a person's own files. This page says what it does with them, what it does not protect against, and how to report a problem. [한국어 요약](#한국어-요약)은 아래에 있습니다.

## What findr does
- **Everything runs on the PC.** File contents, questions and search history are not sent anywhere. The internet is used to download models and the engine during setup and updates, and for nothing else.
- **Files are only read.** findr never changes, moves or deletes the files it searches.
- **Secret files are not read.** Files such as `.env`, `.netrc`, SSH and PuTTY keys, certificates and key stores (`*.pem`, `*.pfx`, `*.p12`, `*.ppk`, `*.kdbx` and similar) are skipped by name. So are Korean joint certificates (`signCert.der`, `signPri.key`, `kmCert.der`, `kmPri.key`, anything in an `NPKI`, `GPKI` or `EPKI` folder or under a path naming 공동인증서 or 공인인증서), saved-password exports (`passwords*.csv`, `logins.csv`, the CSV files browsers export) and browsers' password stores, and password-manager exports (Bitwarden, 1Password, LastPass, KeePass, Dashlane and others). Their contents are never read; a file indexed before a rule was added loses its stored text on the next run. Folders such as `.ssh`, `.aws`, `.kube` and `.docker` and shell history are never indexed. The rules hold for the names systems give copies (`logins 2.csv`, `Copy of logins.csv`, `passwords.csv.txt`, `비밀번호 - 복사본.csv`, decomposed Korean, trailing spaces and dots such as `.env `), for `.envrc`, `.npmrc`, `.pypirc`, `kubeconfig` and files inside those folders, for any `.csv`, `.tsv` or `.txt` whose first line is a password export's header (English or Korean columns), for mail attachments, and for links: a link is judged by where it leads, and a link out of the chosen folders is listed by name only. A mail attached inside a mail is indexed by name only.
- **Where a file came from.** findr reads the download record the operating system keeps for each file (macOS download attributes, the Windows `Zone.Identifier` stream, Linux `user.xdg.*` attributes). Only the site's host name or the app's name goes into the index; the full address and a mail's sender and subject are not kept. Online-only cloud files are not opened. `FINDR_ORIGIN=0` turns this off.
- **Key-like strings are masked.** Text that looks like an API key, token or password setting (cloud and service tokens, `"password": "…"` in JSON, Bearer tokens and JWTs, PEM, PGP and PuTTY private keys, even cut short, and the password in `scheme://user:password@host`) is replaced before it is stored in the index, and again in everything findr returns: the search page and its API, `read_file`, excerpts.
- **The search page is local and needs a password.** It listens on `127.0.0.1` only. Repeated wrong passwords are refused for a while. Responses carry a content security policy, forbid framing and are not cached.
- **One data folder.** The index, models, settings, logs and what findr learned from use are in `%LOCALAPPDATA%\findr` (Windows) or `~/.findr` (macOS, Linux). Uninstalling removes it; see the README. findr does not search its own data folder. If a folder inside it is chosen for search, only that folder is searched; the index, models, logs and settings stay out.

## What findr does not do
- **The index is not encrypted.** It holds pieces of document text and is protected only by the file permissions of the user account. Against a lost or stolen PC, use the operating system's disk encryption (BitLocker, FileVault, LUKS).
- **Other programs running as the same user can read the index**, as they can read the files themselves.
- **Installers are not code-signed yet.** Download them only from this repository's releases page.

## AI tools (MCP)
When an AI assistant uses findr through MCP, the file names, locations and excerpts it asks for go to that assistant. If the assistant runs in the cloud, they leave the PC there. Set the assistant to ask before each use of a tool. `read_file` returns only text of files already in the index. No tool names, lists or reads anything in findr's data folder, even if it was indexed, and a path there gets the same answer as a path not in the index (links, letter case, `..`, macOS `/System/Volumes/Data/…` paths, hard-linked folders and Windows forms such as `\\?\`, `\\localhost\C$\`, short names or trailing dots do not get around it: folders are compared by identity, not only by text). The indexer leaves its own data folder out the same way. Notes and odds in `search_files` are worked out from the files it shows only, so they do not tell whether a left-out file holds a word. More folders can be kept from AI tools with `FINDR_MCP_DENY` (folders separated by `;`, or `:` outside Windows; `C:\x` stays one folder).

## Reporting a vulnerability
Please do not open a public issue. Use GitHub's **Security → Report a vulnerability** on this repository. We answer within 7 days.

## 한국어 요약
- 모든 처리는 PC 안에서 합니다. 인터넷은 설치·업데이트 때 모델과 엔진을 받는 데만 씁니다.
- 검색 대상 파일은 읽기만 합니다. 비밀 파일(`.env`, SSH 키, 인증서 등)과 공동인증서(NPKI·GPKI·EPKI 폴더, `signCert.der`·`signPri.key` 등), 비밀번호 CSV(`passwords*.csv`, `logins.csv`), 비밀번호 관리 앱의 내보내기 파일은 읽지 않습니다. 사본 이름(`logins 2.csv`, `Copy of logins.csv`, `비밀번호 - 복사본.csv`, `passwords.csv.txt`), 끝에 공백·점이 붙은 이름(`.env `), `.envrc`·`.npmrc`·`kubeconfig`, 첫 줄이 비밀번호 내보내기 머리글인 모든 `.csv`·`.tsv`·`.txt`, 메일 첨부, 비밀 파일을 가리키는 링크도 같습니다. 고른 폴더 밖을 가리키는 링크는 이름만 색인합니다.
- 키처럼 보이는 문자열(클라우드·서비스 토큰, JSON의 `"password": "…"`, Bearer·JWT, PEM·PGP·PuTTY 개인 키(끝이 잘려도), 주소 속 `user:비밀번호@`)은 저장할 때와 돌려줄 때(검색 화면·API·`read_file`·발췌) 모두 가립니다.
- 파일을 어디서 받았는지(운영체제가 남긴 다운로드 기록)를 읽어, 사이트 이름이나 앱 이름만 색인에 둡니다. 전체 주소나 메일의 보낸 사람·제목은 저장하지 않습니다. `FINDR_ORIGIN=0`으로 끌 수 있습니다.
- 검색 화면은 이 PC(127.0.0.1)에서만 열리고 비밀번호가 필요합니다.
- **색인은 암호화되어 있지 않습니다.** PC 분실에 대비하려면 운영체제의 디스크 암호화(BitLocker, FileVault)를 쓰세요.
- AI 도구(MCP)에 연결하면 찾은 파일의 이름과 내용 일부가 그 AI 도구로 전달됩니다. 클라우드 AI라면 PC 밖으로 나갑니다. findr 데이터 폴더 안의 파일은 AI 도구에 알려 주지 않으며, 더 뺄 폴더는 `FINDR_MCP_DENY`로 정할 수 있습니다. 링크·대소문자·macOS `/System/Volumes/Data/…`·Windows의 `\\?\`·`\\localhost\C$\`·짧은 이름으로 돌아가도 폴더를 그 실체로 비교해 막고, 색인기도 같은 방법으로 자기 데이터 폴더를 뺍니다. `search_files`의 안내문과 확률은 보여 주는 파일만으로 계산합니다.
- 취약점은 공개 이슈 대신 GitHub의 **Security → Report a vulnerability**로 알려 주세요. 7일 안에 답합니다.
