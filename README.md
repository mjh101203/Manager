# Manager

자연어로 일정을 관리하고 Notion·Obsidian에 자동 백업하는 CLI.
AI 두뇌는 **Claude Code CLI(`claude -p`)** — 별도 API 키 없이 Claude 구독으로 돌아감.

```
> 11월 16일 오후 9시까지 대수 주제탐구 보고서, 11월 18일 오후 4시 두루누리 회의
  1. + 추가: 대수 주제탐구 보고서 | 2026-11-16T21:00 (마감) <Assignment>
  2. + 추가: 두루누리 회의 | 2026-11-18T16:00 <Durunuri>
적용할까? [Y=적용 / n=취소 / 고칠 내용 입력 (예: 2번 빼줘, 시간 8시로)] 2번 빼줘
  1. + 추가: 대수 주제탐구 보고서 | 2026-11-16T21:00 (마감) <Assignment>
적용할까? [Y=적용 / n=취소 / 고칠 내용 입력 (예: 2번 빼줘, 시간 8시로)]
  ✔ 추가됨 #15 대수 주제탐구 보고서
Notion: +1 ~0 -0
Obsidian: 작성 2 / 삭제 0
```

## 구조

```
사용자 ──자연어──▶ cli.py ──프롬프트(현재시각·달력·기존일정·태그)──▶ claude -p
                     ▲                                               │
                     │                                   JSON 계획 {reply, actions}
                     │                                               ▼
      확인 [Y / n / 고칠 내용] ◀── 미리보기 ◀── ai.py (검증·적용)
                                                        │
                                               SQLite (원본, db.py)
                                              ┌─────────┴─────────┐
                                       sync_notion.py       sync_obsidian.py
                                       (DB 페이지 upsert)     (vault/Manager/*.md)
```

- **원본은 로컬 SQLite**(`manager.db`). Notion·Obsidian은 단방향 미러라 충돌이 없음.
- AI는 DB를 직접 못 건드리고 액션 JSON만 냄 → Python이 검증 후 적용.
  액션: `add` `update` `done` `delete` `add_tag` `rename_tag`
- 삭제는 soft delete → Notion 페이지는 휴지통으로, Obsidian 노트는 정리.

## 설치 (Windows)

```powershell
cd C:\Users\arang\GitHub\Manager
pip install -e .
```

- `manager` 명령이 안 잡히면(PATH 문제) 레포 폴더에서 `python -m manager ...`로 실행하면 됨.
- Python 3.10 이하면 `pip install tomli` 필요.
- `claude --version`이 터미널에서 실행되는지 먼저 확인.

## 설정

레포 안 `config.toml`(git에 안 올라감) 또는 `~/.manager/config.toml`. 템플릿은 `config.example.toml`, `manager init`.

### Obsidian
```toml
[obsidian]
enabled = true
vault_path = 'C:\Users\arang\obsidian\obsidian'
folder = "Manager"
```
`<vault>/Manager/Index.md`(진행 중·다가오는 30일·지난 미완료·태그 통계)와
`<vault>/Manager/Daily/YYYY-MM-DD.md`(날짜별 체크리스트)가 생성됨.
`generator: manager` 표시가 있는 파일만 덮어쓰고, 직접 만든 파일은 건드리지 않음.

### Notion
DB **Schedule Management**는 이미 만들어져 있음 (`database_id = "50d614dcf56242dda69fe1dfffdaf045"`).

1. https://www.notion.so/profile/integrations → 새 API 통합(Internal) → **내부 통합 시크릿** 복사 (`ntn_...`)
2. Schedule Management DB → `···` → **연결(Connections)** → 그 integration 추가
3. config에 토큰 넣고 켜기:
   ```toml
   [notion]
   enabled = true
   token = "ntn_..."          # 또는 환경변수 NOTION_TOKEN
   database_id = "50d614dcf56242dda69fe1dfffdaf045"
   ```
4. `manager sync --notion`으로 연결 확인

DB 속성:

| 속성 | 종류 | Manager가 쓰는지 |
|---|---|---|
| Name | 제목 | ✔ |
| Category | 선택 (태그) | ✔ |
| Start Date / End Date | 날짜 | ✔ (마감 일정은 End Date만) |
| Done | 체크박스 | ✔ (완료 = 체크) |
| Teacher | 다중선택 | ✔ (Assignment는 과목별 선생님 자동 입력) |
| Submission Link | 링크(URL) | ✔ 제출 링크. 링크가 없으면 제출 방법을 텍스트로 (예: 드롭박스, 교무실 제출함) |
| Team | 다중선택 | ✔ 팀원 |

새 DB가 필요하면 `manager notion-setup <부모페이지ID>`.

## 명령어

설치 안 했으면 `manager` 대신 `python -m manager`.

### AI (자연어)
| 명령 | 설명 |
|---|---|
| `manager` | 대화 모드 |
| `manager ask "내용"` | 한 번만 요청. 여러 일정을 한 문장/여러 줄로 넣어도 됨. `-y` = 확인 없이 적용 |

### 일정
| 명령 | 설명 |
|---|---|
| `manager today` | 오늘 + 진행 중 + 지난 미완료 |
| `manager list` | 앞으로 14일. `--days 30` `--from 2026-11-01` `--todo`(미완료만) `--all`(전체) |
| `manager add 제목 "2026-11-16 21:00"` | AI 없이 추가. `--end` `--all-day` `--deadline` `-c 태그` `-t 선생님`(여러 번) `-m 팀원`(여러 번) `-s 과목` `-l 장소` `-n 메모` |
| `manager edit 3` | 대화형 수정 — 항목별로 물어봄, 엔터 = 유지 |
| `manager edit 3 "30분 늦춰줘"` | AI로 그 일정만 수정 |
| `manager edit 3 --tag Research --end 2026-11-20` | 바로 수정. `--title` `--start` `--end -`(비우기) `-t "A 선생님, B 선생님"` `-m "팀원1, 팀원2"` `-s 과목` `--deadline`/`--no-deadline` `--status` `--submit <링크 또는 방법>` |
| `manager submit 15` | 제출 링크·방법 보기, 링크면 브라우저로 열기 (`--no-open`) |
| `manager done 3 4` | 완료 처리 (Notion Done 체크). 과제면 실제 걸린 시간을 물어봄 / `--took 3h` |
| `manager rm 5` | 삭제 (Notion은 휴지통으로) |

### 태그
| 명령 | 설명 |
|---|---|
| `manager tag` | 태그 목록 + 태그별 일정 수 |
| `manager tag add Club` | 태그 추가 |
| `manager tag rename Club Durunuri` | 이름 변경 — 그 태그 일정 전부 바뀜 |
| `manager tag rm Club [--to Research]` | 태그 삭제 — 그 일정들은 `--to` 태그로 옮기거나 태그 없음으로 (일정 자체는 안 지워짐) |

### 과제 수행 계획
| 명령 | 설명 |
|---|---|
| `manager plan 15` | 과제 #15를 마감까지 작업 세션으로 나눠 빈 시간에 배치 |
| `manager plan 15 "주말 위주로, 11/7은 안 돼"` | 요청을 반영해서 계획 |
| `manager plan 15 --topic "해밍 코드" --file 안내문.pdf` | 주제·안내문 저장하고 계획 (`--scope`도 가능) |
| `manager profile` | 가용 시간·세션 설정·유형별 소요 시간(내 기록 반영) 보기 |
| `manager school` / `manager school sync` | 학사일정·귀가주 확인 / 학교 홈페이지에서 지금 새로 받기 |

자연어도 됨: "대수 보고서 일정 짜줘", "화학전지 논문 언제 하면 좋을지 짜줘".

### 과목별 선생님
| 명령 | 설명 |
|---|---|
| `manager teacher` | 등록된 과목 → 선생님 목록 |
| `manager teacher set 화학 "서보배 선생님"` | 등록/변경. 선생님이 비어 있는 기존 화학 Assignment에도 채움 |
| `manager teacher rm 화학` | 과목 삭제 (이미 채워진 일정은 그대로) |
| `manager teacher learn` | 선생님이 이미 적힌 Assignment 일정의 제목에서 과목을 찾아 과목별 선생님 추정 → 미리보기 → Y면 등록 (`2번 빼`, `3번 생명과학`, `1번 김OO 선생님`으로 고치기) |

### 백업·설정
| 명령 | 설명 |
|---|---|
| `manager sync` | Notion에서 가져오기 → Notion + Obsidian 백업. `--notion` / `--obsidian`으로 한쪽만, `--no-pull`이면 가져오기 생략. `auto_sync = true`면 변경 시 자동 |
| `manager pull` | Notion에서 고친 내용을 로컬에 반영. `--dry-run`이면 미리보기 → 적용할지 물어봄 (아래 참고) |
| `manager init` | 설정 파일 템플릿 생성 |
| `manager notion-setup <페이지ID>` | Notion에 새 일정 DB 생성 |

### 대화 모드 안에서
`/list` `/today` `/tags` `/teachers` `/plan 15 [요청]` `/edit 3 [내용]` `/pull` `/sync` `/quit`

## 동작 메모

### 적용 확인 단계
AI가 제안한 변경은 번호를 매겨 보여주고 확인을 받음.
- `Y`/엔터 → 적용
- `n` → 취소
- 그 외 입력(`2번 빼줘`, `시간 8시로` 등) → 수정 요청으로 받아 제안을 다시 만듦. 적용/취소할 때까지 반복

### 태그
- 태그 = Notion `Category` 선택지. 동기화 때 Notion에 없는 태그는 선택지로 자동 추가.
- AI는 기존 태그만 씀. 새 태그가 필요하면 "Club 태그 만들어서 넣어줘"처럼 말하면 됨 (미리보기에 `(새 태그)` 표시).
- `tag rename` / `tag rm` → 다음 동기화 때 Notion DB에서 예전 태그가 붙은 **모든 페이지**(Manager가 만들지 않은 페이지 포함)를 바꾸고 예전 선택지는 지움. Notion이 꺼져 있거나 실패하면 쌓아뒀다가 다음 동기화 때 순서대로 처리.
- `rename X Y`와 `rm X --to Y`는 결과가 같음. 기존 태그로 합칠 땐 `rm --to`가 의미상 자연스러움.

### 과제 수행 계획 (스킬)
같은 "보고서"라도 **주제마다 해야 할 일이 다르므로**, 유형이 아니라 주제 기준으로 계획함.

1. **과제 정보** — 주제(`--topic`), 범위·요구사항(`--scope`), 안내문 파일(`--file`, PDF·이미지·txt — Claude가 직접 읽음).
   주제·범위가 없으면 AI가 먼저 1~3개 질문하고, 답은 과제의 '범위·요구사항'에 저장.
2. **AI** (`skills/assignment-planner/SKILL.md`) — 주제를 작업 요소(딥러닝, 수학 이론, 글쓰기…)로 나누고
   요소별 필요 수준(Lv 1~10)과 **내 역량**을 비교: 모자라면 칸당 +25%(공부·시행착오), 넘치면 칸당 −5%.
   역량이 모자란 요소 앞엔 학습 단계를 넣고, 단계는 주제에 맞게 구체적으로.
3. **Python** — 요일별 가용 시간 − 기존 일정 = 빈 시간에 배치.
   세션 길이·하루 상한·세션 사이 휴식·마감 전 여유, 그리고 **매일 예비 시간(기본 120분)은 비워둠**.
   그날 남은 만큼 잘라 채우고, 같은 날 이어지는 조각은 합치며, 순서는 절대 안 뒤집힘. 모자라면 경고.
4. 미리보기 → `Y` 적용 / `n` 취소 / 고칠 내용 입력("더 짧게", "주말에 몰아서", "11/10 빼줘").
5. **보정** — 완료 때 실제 시간을 기록하면 유형별 + **작업 요소별** `실제/예상` 비율이 쌓여 다음 추정에 반영.

`planner.toml`:
- `[availability]` 요일별 작업 가능 시간
- `[session]` 세션 길이, 하루 상한, `daily_reserve_minutes`(매일 예비 시간), `buffer_hours`(마감 전 여유)
- `latest_end`(기본 22:00) 이후로는 안 잡고, **귀가주 금·토**만 `home_weekend_latest_end`(기본 24:00)까지.
  귀가주 = 학교 홈페이지 학사일정에서 그 주 금·토에 '미귀가'가 없는 주. 계획할 때 7일마다 자동으로 받아
  `school_calendar.json`에 저장 (못 받으면 저장된 걸 씀)
- `[skills]` 영역별 내 역량 **1 = 평균적인 고등학생, 10 = 대학원생**
- `[estimates]` 유형별 기본 시간(형식 참고용), `[me] note` 나에 대한 메모
(TOML이라 한글 키는 `"파이썬" = 5`처럼 따옴표 필요)

### 선생님 자동 입력
- `Assignment` 태그 일정에 선생님이 비어 있으면 과목표로 자동 입력 (다른 태그는 안 함).
- 과목 판별: AI가 등록된 과목 중에서 고름 → 안 되면 제목에 과목명이 들어 있는지로 판단 (예: "**대수** 주제탐구 보고서").
- 선생님을 직접 말하거나 `-t`로 넣으면 그게 우선. 미리보기에 `👤이름 (자동)`으로 표시.
- 이름은 Notion Teacher 선택지와 같은 표기(예: `최한욱 선생님`)로 등록해야 같은 선택지로 묶임.

### 제목
"제출", "마감", "~까지", "11/16까지" 같은 말은 제목에서 자동으로 빠짐 (마감은 마감 일정 표시, 제출은 Submission Link 칸에).
예: `캔위성 계획서 제출` → `캔위성 계획서`. `제출물 정리`처럼 다른 단어의 일부면 그대로.

### 마감 일정
수행평가·제출처럼 "그날까지" 일정은 마감 일정으로 저장 → 목록에 `마감`으로 표시, Notion `End Date` = 마감일. AI가 알아서 판단하고, 직접 넣을 땐 `--deadline`.
- 시작일을 안 주면 Notion `Start Date`는 **입력한 날짜**: `manager add 보고서 2026-11-12 --deadline`
- 시작일을 주면 그 날짜: `manager add 보고서 2026-11-01 --end 2026-11-12 --deadline` (말로 "11/1부터 11/12까지"도 같음)

### Notion에서 고친 내용 가져오기 (pull)
`chat` `ask` `add` `list` `today` `done` `plan` `submit` `rm` `edit`, `sync`를 실행하면 먼저 Notion을 확인해서 로컬에 반영함.
끄려면 config.toml `[notion]`에 `pull = false`.

| Notion에서 한 일 | 로컬 |
|---|---|
| 제목·날짜·Done·Category·Teacher·Team·Submission Link 수정 | 같은 값으로 수정 |
| DB에 새 페이지 추가 | 새 일정으로 추가 (Start/End Date 중 하나는 있어야 함) |
| 페이지 삭제·DB 밖으로 이동 | 로컬에서도 삭제 (Notion 페이지는 그대로 둠) |

- 로컬과 Notion에서 같은 일정을 둘 다 고쳤으면 **충돌**로 알려주고 로컬 값을 유지함 (다음 동기화 때 Notion이 로컬 값으로 덮임).
- `manager pull --dry-run` (대화 모드 `/pull`): 번호 붙은 미리보기를 보여주고 `[Y=적용 / n=취소 / 고칠 내용]`을 물어봄.
  고칠 내용을 입력하면 반영해서 미리보기를 다시 보여주고, Y나 n이 나올 때까지 반복.
  - `2번 빼`, `2, 3 제외`, `-4` → 그 항목 제외 (AI 없이 바로)
  - `KSEF는 12월 5일 마감으로 넣어줘` → 날짜 없어서 건너뛴 항목을 추가로
  - `3번은 로컬 값 유지하고 노션 덮어` → Notion 값 무시, 다음 동기화 때 로컬 값으로 Notion 수정
  - `충돌난 건 노션 걸로` → 충돌 항목도 Notion 값 적용
  - `1번 제목은 OO로` → 가져올 값 자체를 수정
- 가져온 제목에 "제출", "마감" 같은 말이 있으면 로컬에서 지우고, Notion 제목도 그렇게 고침.

## 테스트

```
pip install -e .[dev]
pytest
```

## 다음 확장 아이디어
- Obsidian 체크박스 → 완료 상태 역동기화 (Notion은 `manager pull`로 지원)
- 반복 일정 (RRULE)
- 파일로 일괄 입력 (`manager ask -f 일정.txt`)
- 학교 시간표·시험 기간을 가용 시간에 자동 반영
- 디스코드/텔레그램 봇, 웹 UI (`ai.interpret` / `apply_actions` 재사용)
- Google Calendar 연동, 아침 브리핑 알림
