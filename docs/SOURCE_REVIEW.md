# ALDO 해커톤 자료 검토 기록

확인일: 2026-10-15. 시간 해석 기준: America/Toronto. 로컬 경로 기준: `/Users/minsu/Desktop/aldo-return-agent`.

## 자료 목록

| ID | 자료와 위치 | 확인 내용 |
| --- | --- | --- |
| S1 | `docs/sources/Hackathon Final Instructions.pdf` | Outlook 10월 9일 Finalist REVEAL 메일에서 이번 작업 중 다운로드. 4쪽. 2쪽 일정, 3쪽 필수 결과물, 4쪽 최종 심사표. 4쪽은 이미지라 렌더링 후 직접 읽음 |
| S2 | `docs/sources/USE CASE RETAIL GEN AI HACKATHON 923.pdf` | 기존 Downloads 파일 복사 및 12쪽 전체 텍스트 추출. 4–7쪽 문제·에이전트·프로토타입 요구, 9쪽 최종 심사, 10–12쪽 예시 데이터 목록 |
| S3 | `docs/sources/BSRM X ALDO HACKATHON OVERVIEW 2026.pdf` | 기존 Downloads 파일 복사 및 5쪽 전체 텍스트 추출. 2쪽 이전 데이터 안내, 4쪽 행사 일정 |
| S4 | `docs/sources/PHASE 1 HACKATHON  INSTRUCTIONS 923.pdf` | 기존 Downloads 파일 복사 및 7쪽 전체 텍스트 추출. Phase 1 기준이며 현재 최종 심사표와 구분 |
| S5 | `docs/sources/BSRM × ALDO RETAIL GEN AI HACKATHON.pptx` | 열린 Google Slides에서 이번 작업 중 PPTX 다운로드. 4장 전체 OOXML 텍스트 추출. 팀명, 역할, Agentic Returns Gateway 제안 및 AWS 구성 |
| S6 | `dataset/Notes.docx` | 문단 전체 추출. final sale, 수수료, 상품, 교환, 출고 주문 조인 규칙 |
| S7 | `dataset/*.csv`, `dataset/Book.xlsx` | CSV 5개 전행 프로파일. XLSX 내부 workbook/worksheet 확인: Sheet1 A1, 셀 데이터 없음 |
| S8 | `docs/sources/AWS × McGill Hackathon — Intro AWS.pdf` | 기존 Downloads 파일 복사 및 14쪽 전체 텍스트 추출. 7–11쪽 AgentCore/Strands, AWS 활용, 계정 삭제 안내 |
| S9 | `docs/sources/Participant Handout.pdf` | 기존 Downloads 파일 복사 및 2쪽 전체 텍스트 추출. AWS 도구와 행사 계정 안내 |
| S10 | Outlook 관련 메일, 아래 요약 | 크롬 앱 접근성 트리로 본문 확인. 메일 원문 전체 및 접근 코드는 파일로 저장하지 않음 |
| S11 | AWS Workshop Studio, 아래 링크 | 열린 로그인 세션에서 dashboard, workshop, CLI/SDK, Building agents, Strands 페이지 읽음 |
| S12 | GitHub 연결 도구 `get_repo` | `MinsuKin/aldo-return-agent`, public, default branch main 확인. 로컬 origin 동일. 로컬 main에는 커밋 없음 |
| S13 | Sites 연결 도구 `list_sites` | 조회된 소유 사이트 목록에 ALDO 프로젝트 없음. 이 저장소에 `.openai/hosting.json` 없음. ALDO 사이트 생성·배포 안 함 |

## 메일에서 확인한 운영 사실

- 2026-10-09 12:35, BSRM Competitions, **BSRM X ALDO Retail Gen AI Hackathon Finalist REVEAL !**: 10개 파이널 팀 선정, 10월 15일 10시 Bronfman 2층 킥오프, 전원 참석 안내. S1 첨부.
- 2026-10-13 09:59, BSRM Competitions, **Reminder LIVE Final Thursday Oct 15th 10:00 am BSRM x ALDO Retail Gen Ai Hackathon**: CMD Commerce는 Phase 1 점수 93/100, 공동 3위. 목요일 새 데이터셋 제공 안내. 10:04 Vassili의 참석 확인 답장도 존재.
- 2026-10-13 14:20, Vincent Rioux, **Your AWS account for the Aldo-McGill BSRM hackathon - Team 10**: 팀 10, 10월 15일 10시 활성화, **10월 16일 21시 종료 및 내부 모든 자원 자동 삭제, 복구 불가**. 팀 전용 초대 URL은 다른 팀에 공유하지 말라는 안내. 초대 URL/코드는 이 문서에 보관하지 않음. 메일의 시각에 별도 시간대 표기가 없어 현지 행사 시간으로 해석.
- 2026-10-14 19:39, Jonathan Gagne, **Jonathan Gagne a partagé le dossier « Dataset McGill » avec vous**: 직접 수신자만 접근 가능한 OneDrive 데이터 폴더 제공. 현재 열린 폴더의 7개 파일명과 로컬 폴더가 일치.

이 요약은 메일을 읽고 작성한 작업 기록이다. 메일을 전송하거나 회신하지 않았다.

## 브라우저에서 확인한 링크

- [팀 Google Slides](https://docs.google.com/presentation/d/1INTEDlRj31Klj9ANFfi4mR5-oogM0F3_uKRpin9WoS0/edit)
- [Workshop dashboard](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US)
- [Workshop 안내](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US/workshop)
- [AWS CLI와 SDK](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US/workshop/26-cli-sdk)
- [Building agents](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US/workshop/35-agents)
- [행사 Strands 예제](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US/workshop/35-agents/20-strands)
- [워크숍 내 AgentCore 안내 링크](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US/workshop/35-agents/30-agentcore) — 링크 위치 확인, 본문 미검토
- [워크숍 내 Clean up and export 링크](https://catalog.us-east-1.prod.workshops.aws/event/dashboard/en-US/workshop/90-cleanup) — 링크 위치 확인, 본문 미검토
- [Bedrock 콘솔](https://us-east-1.console.aws.amazon.com/bedrock/home?region=us-east-1)
- [IAM Identity Center 콘솔](https://us-east-1.console.aws.amazon.com/singlesignon/home?region=us-east-1)
- [GitHub 저장소](https://github.com/MinsuKin/aldo-return-agent)

공유 폴더는 Chrome의 `Dataset McGill - OneDrive` 탭 또는 10월 14일 메일에서 다시 연다. 수신자 식별자와 임시 공유 쿼리가 포함된 긴 URL을 문서에 복제하지 않았다.

## 현재 AWS 상태와 기술 참고

Bedrock 콘솔에서 `WSParticipantRole/Participant`, N. Virginia `us-east-1` 로그인 상태를 확인했다. 모델 호출, quota, 개별 모델 사용 권한 및 자원 배포는 시험하지 않았다. IAM Identity Center 화면은 활성화 시작 화면이었으며 어떤 설정도 바꾸지 않았다.

행사 Strands 페이지는 자체 예제가 `strands-agents 1.55.1`, Python 3.10 이상으로 검증됐다고 설명한다. `Agent`, `@tool`, `BedrockModel`, `region_name` 예제가 있으며 `global.amazon.nova-2-lite-v1:0`를 시작 모델로 제시한다. 이는 **행사 가이드의 예제값**이며 이 계정에서 호출 성공을 확인한 값은 아니다. 구현 시 실제 호출 성공 후 버전·모델 ID를 고정한다. 기본 모델은 바뀔 수 있으므로 명시적으로 설정한다.

2026-10-15 공식 문서도 확인했다.

- [Strands Python Quickstart](https://strandsagents.com/docs/user-guide/sdk/quickstart/python/): Python Agent와 사용자 정의 도구 구현 참고. 일반 shell/file/http 도구를 반품 에이전트에 그대로 노출하지 않는다.
- [AWS Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html): 도구 사용 및 Guardrails 지원 설명.
- [AWS Guardrails와 Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-converse-api.html): tool use 입력 등 보호 범위에 제한이 있으므로 사업 규칙과 인가 검사는 서버 도구에서 별도로 수행해야 한다.

## 출처 충돌과 해석

1. **데이터 제공**: S3의 초기 안내는 public/synthetic만 언급하지만 S2는 익명 데이터 제공을 설명하고, 10월 13일 메일은 신규 데이터 제공, 10월 14일 메일은 실제 공유 폴더를 제공한다. 현재 전달된 파일을 로컬 분석 대상으로 사용했다. 초기 문구를 근거로 실제 파일을 무시하지도, 공유받았다는 이유로 공개 재배포 권한을 추정하지도 않는다.
2. **AWS 종료**: dashboard에는 시작 10월 15일 10:25, duration 72 hours 및 약 3일 잔여 표시가 있었다. 동시에 비활성 이벤트 안내도 남아 있었다. 담당자 메일의 더 이른 명시적 종료인 10월 16일 21시 전에 백업한다. 정확한 종료는 현장 담당자에게 확인하되 백업을 미루지 않는다.
3. **심사표**: Phase 1은 30/25/25/20. 현재는 S1 4쪽과 S2 9쪽의 최종 심사표 30/25/15/15/15를 따른다.
4. **데이터 예시와 실제 파일**: S2의 10개 데이터 범주는 제공 가능 필드의 예시다. 현재 7개 파일에 그 범주가 모두 들어 있다고 가정하면 안 된다.
5. **이름과 날짜 범위**: 주문 파일명에는 Feb 2026이 있지만 실제 최대 주문일은 2026-01-31. 교환 데이터는 2026-03-16까지 있어 반품 파일과 기간이 다르다.

## 저장과 검토 범위

원본은 `docs/sources/`, 텍스트 추출물과 계산 결과는 `docs/evidence/`에 보관했다. `data-profile.json`에는 CSV별 SHA-256, 행 수, 컬럼, 빈값, 중복 키, 주요 분포와 조인 커버리지가 있다. `cohort-profile.json`에는 사이즈 사유와 ALDO_CA 부분 집계가 있다. 현재 `.gitignore`는 원본 데이터, 다운로드 자료, 증거 파일 및 자격증명을 Git에서 제외한다.

크롬 연결의 request-header policy 로드가 실패하여 Chrome 네이티브 앱 UI로 읽기·다운로드를 수행했다. PDF 스킬로 로컬 PDF를 추출했고, 텍스트가 없는 최종 심사표 페이지와 일정 페이지는 렌더링해서 확인했다. AWS 자격증명 생성·복사, 계정 설정 변경, 배포, GitHub push는 수행하지 않았다.
