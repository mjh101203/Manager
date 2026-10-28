# Manager

자연어로 일정을 관리하고 Notion·Obsidian에 자동 백업하는 CLI.
AI 두뇌는 **Claude Code CLI(`claude -p`)** — 별도 API 키 없이 Claude 구독으로 돌아감.

```
> 다음주 화요일 3시부터 5시까지 물포럼 회의
물포럼 회의 11/3(화) 15:00–17:00으로 추가할게.
  + 추가: 물포럼 회의 | 2026-11-03T15:00 ~ 2026-11-03T17:00 <물포럼>
적용할까? [Y/n]
  ✔ 추가됨 #1 물포럼 회의
Notion: +1 ~0 -0
Obsidian: 작성 2 / 삭제 0
```

## 구조

```
사용자 ──자연어──▶ cli.py ──프롬프트(현재시각·달력·기존일정)──▶ claude -p
                     ▲                                          │
                     │                              JSON 계획 {reply, actions}
                     │                                          ▼
                  확인 [Y/n] ◀──── 미리보기 ◀──── ai.py (검증·적용)
                                                   │
                                          SQLite (원본, db.py)
                                         ┌─────────┴─────────┐
                                  sync_notion.py       sync_obsidian.py
                                  (DB 페이지 upsert)     (vault/Manager/*.md)
```

- **원본은 로컬 SQLite**. Notion·Obsidian은 단방향 미러라 충돌이 없음.
- AI는 DB를 직접 못 건드리고, `add / update / done / delete` 액션 JSON만 냄 → Python이 검증 후 적용.
- 삭제는 soft delete → Notion 페이지는 휴지통으로, Obsidian 노트는 정리.

## 설치 (Windows)

```powershell
cd C:\Users\arang\GitHub\Manager
python -m venv .venv
.venv\Scripts\activate
pip install -e .
manager init          # ~/.manager/config.toml 생성
```

`claude` 명령이 터미널에서 실행되는지 먼저 확인 (`claude --version`).

## 설정

`~/.manager/config.toml` (또는 레포 안 `config.toml`) — `config.example.toml` 참고.

### Obsidian
```toml
[obsidian]
enabled = true
vault_path = 'C:\Users\arang\Documents\내볼트'
folder = "Manager"
```
`<vault>/Manager/Index.md` (다가오는 30일·지난 미완료·카테고리 통계)와
`<vault>/Manager/Daily/YYYY-MM-DD.md` (날짜별 체크리스트)가 생성됨.
자동 생성 파일에는 `generator: manager` 표시가 있고, 이 표시가 없는 파일은 절대 덮어쓰지 않음.

### Notion
1. https://www.notion.so/profile/integrations → 새 internal integration 생성 → 토큰 복사
2. 일정 DB를 둘 Notion 페이지에서 `···` → **연결(Connections)** → 방금 만든 integration 추가
3. 토큰을 config에 넣고 (`token = "ntn_..."` 또는 환경변수 `NOTION_TOKEN`)
4. 부모 페이지 ID(URL 끝 32자리)로 DB 생성:
   ```
   manager notion-setup <부모페이지ID>
   ```
5. 출력된 `database_id`를 config에 넣고 `enabled = true`

DB 속성: Name / Date / Status(예정·완료·취소) / Category / Location / Notes / Manager ID

## 사용법

| 명령 | 설명 |
|---|---|
| `manager` | 대화 모드 (`/list` `/today` `/tags` `/edit <번호>` `/sync` `/quit`) |
| `manager ask "내일 9시 동아리 회의"` | 한 번만 요청 (`-y`로 확인 생략). 여러 일정을 한 문장/여러 줄로 넣어도 됨 |
| `manager today` | 오늘 + 진행 중 + 지난 미완료 |
| `manager list [--days 30] [--todo] [--all]` | 목록 |
| `manager add 제목 "2026-10-28 15:00" [--end ..] [-c 태그] [--deadline]` | AI 없이 직접 추가 |
| `manager edit 3` | 대화형 수정 — 필드별로 물어봄, 엔터 = 유지 |
| `manager edit 3 "30분 늦춰줘"` | AI로 그 일정만 수정 |
| `manager edit 3 --end 2026-11-20 --tag Research [--deadline/--no-deadline]` | 바로 수정 (`--end -` = 비우기) |
| `manager done 3 4` / `manager rm 5` | 완료 / 삭제 |
| `manager tag` | 태그 목록과 일정 수 |
| `manager tag add Club` / `tag rename Club Durunuri` / `tag rm Club [--to Research]` | 태그 추가·이름 변경·삭제 |
| `manager sync [--notion] [--obsidian]` | 수동 백업 (`auto_sync=true`면 변경 시 자동) |

### 적용 확인 단계
AI가 제안한 변경은 번호 매겨서 보여주고 확인을 받음:
- `Y`/엔터 → 적용
- `n` → "어떻게 고칠까?" → 수정 요청을 받아 제안을 다시 만듦 (엔터 = 취소)
- 확인 창에 바로 `2번 빼줘`, `시간 8시로` 같은 걸 쳐도 수정 요청으로 처리

### 태그
- 태그 = Notion의 `Category` 선택지. 동기화 때 Notion에 없는 태그는 선택지로 자동 추가됨.
- AI는 기존 태그만 씀. 새 태그가 필요하면 "Club 태그 만들어서 넣어줘"처럼 말하면 됨 (미리보기에 `(새 태그)` 표시).
- `tag rename` / `tag rm`은 그 태그가 붙은 일정을 전부 바꿈. 다음 동기화 때 Notion DB에서 예전 태그가 붙은 **모든 페이지**(Manager가 만들지 않은 페이지 포함)를 새 태그로 바꾸고, 예전 선택지는 지움.

### 마감 일정
수행평가·제출처럼 "그날까지" 일정은 마감 일정으로 저장되고, Notion에는 `End Date`에만 기록됨. AI가 알아서 판단하고, 직접 넣을 땐 `--deadline`.

## 테스트

```
pip install -e .[dev]
pytest
```

## 다음 확장 아이디어
- Obsidian 체크박스 → 완료 상태 역동기화
- 반복 일정 (RRULE)
- 디스코드/텔레그램 봇, 웹 UI (현재 `ai.interpret` / `apply_actions`를 그대로 재사용 가능)
- Google Calendar 연동, 아침 브리핑 알림
