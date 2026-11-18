# Бот поиска стажировок: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram-бот для одного пользователя. Несколько раз в день он собирает стажировки по аналитике и ML с hh.ru, Т-Банка и Сбера, отбирает подходящие, склеивает дубли и присылает новые вакансии с кнопками статусов.

**Architecture:** Один Python-процесс: бот на aiogram и планировщик APScheduler. Планировщик по расписанию запускает адаптеры источников. Каждый адаптер возвращает список `Posting` в едином формате. Конвейер (`pipeline`) прогоняет их через фильтр, склеивает в `Vacancy` и пишет в PostgreSQL. Отдельный отправщик (`notifier`) рассылает всё, что ещё не отправлено. PostgreSQL работает в Docker рядом с приложением.

**Tech Stack:** Python 3.12, uv, aiogram 3, APScheduler 3.10 (AsyncIOScheduler), SQLAlchemy 2 (async) + asyncpg, Alembic, httpx, pydantic-settings, PyYAML, pytest + pytest-asyncio + respx + testcontainers[postgres], Docker Compose, PostgreSQL 16.

**Spec:** `docs/superpowers/specs/2026-11-18-vacancy-bot-design.md`

## Global Constraints

- Бот отвечает только Telegram ID владельца (`OWNER_ID` в `.env`). Апдейты от остальных молча игнорируются.
- Секреты (`BOT_TOKEN`, `OWNER_ID`, `DATABASE_URL`, `HH_USER_AGENT`) только в `.env`. `.env` в `.gitignore`, в репозитории лежит `.env.example`.
- Ноль платных API.
- hh.ru: только публичный `GET https://api.hh.ru/vacancies` без авторизации. В каждом запросе заголовок `HH-User-Agent` из `HH_USER_AGENT`: владелец сам вписывает туда название и свой контакт. Пауза между страницами не меньше 0.3 с.
- Критерии отбора, запросы к источникам, расписание и пороги хранятся в `config/config.yaml`, не в коде.
- Отсев только при **явном** несовпадении. Если поле не указано, вакансия проходит с флагом.
- Исключаем роли, а не компании.
- Склеиваются только открытые одновременно вакансии. Повторно открытая вакансия присылается с пометкой «🔁 снова открыта».
- Первый успешный запуск каждого источника тихий: одно итоговое сообщение «Найдено N открытых подходящих вакансий», дальше `/backlog`.
- Время хранится в UTC (`timestamptz`).
- Код не зависит от ОС хоста: запуск через `docker compose up`.
- Автооткликов нет, но статусы — расширяемый enum.

## Review Focus

1. **Ошибка на середине выдачи источника** (упала одна страница hh, таймаут). Ожидание: запуск помечается неудачным, ни одна вакансия этого источника не закрывается. Иначе при следующем запуске прилетит лавина ложных «🔁». Тест: Task 6 `test_failed_fetch_closes_nothing`.
2. **Одна и та же вакансия дважды в одной выборке** (hh отдаёт её по двум запросам). Ожидание: одна запись, одно уведомление. Тесты: Task 7 `test_dedups_ids_across_queries`, Task 6 `test_duplicate_postings_in_one_batch`.
3. **Telegram отказал посреди рассылки** (`TelegramRetryAfter`, сетевой сбой). Ожидание: вакансии, которые не ушли, остаются с `notified_at IS NULL` и уходят в следующий раз, а уже отправленные не дублируются. Тест: Task 10 `test_notifier_keeps_unsent_on_failure`.
4. **Нажатие кнопки у вакансии, которой больше нет, или кривой callback.** Ожидание: короткий ответ «Вакансия не найдена», без исключения в логах. Тест: Task 10 `test_status_callback_unknown_vacancy`.
5. **Варианты написания одной вакансии**: «Стажёр-аналитик (Data)» и «стажер аналитик data», «ПАО Сбербанк» и «Сбер». Ожидание: один ключ склейки. Тест: Task 4 `test_title_variants_same_key`, `test_company_aliases`.

---

## File Structure

```
pyproject.toml, uv.lock, .gitignore, .env.example, Dockerfile, docker-compose.yml
alembic.ini, migrations/env.py, migrations/versions/0001_initial.py
config/config.yaml                 — критерии, источники, расписание, пороги
CONTEXT.md                         — словарь проекта
src/vacancy_bot/
  settings.py      — env (pydantic-settings)
  domain.py        — Posting, WorkFormat, Employment, VacancyStatus, Decision
  config.py        — загрузка config.yaml → AppConfig / Criteria
  filtering.py     — evaluate(posting, criteria) -> Decision
  dedup.py         — normalize_title, normalize_company, dedup_key
  db/tables.py     — ORM-таблицы
  db/session.py    — engine / session factory
  db/repo.py       — запросы к базе
  pipeline.py      — process_postings, run_source
  health.py        — проверки «источник сломан / подозрительно пуст»
  sources/base.py  — протокол Source
  sources/hh.py, sources/tbank.py, sources/sber.py
  sources/registry.py — build_sources(config, http) -> list[Source]
  bot/messages.py  — тексты и клавиатуры
  bot/handlers.py  — команды и callback'и, middleware владельца
  bot/notifier.py  — рассылка неотправленного
  app.py           — сборка: бот + планировщик, точка входа
tests/
  conftest.py      — контейнер Postgres, миграции, сессия
  fixtures/        — сохранённые ответы источников (JSON)
  test_*.py
```

---

### Task 1: Каркас проекта, база в Docker, настройки

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `docker-compose.yml`, `src/vacancy_bot/__init__.py`, `src/vacancy_bot/settings.py`, `tests/test_settings.py`

**Interfaces:**
- Produces: `class Settings(BaseSettings)` с полями `bot_token: str`, `owner_id: int`, `database_url: str` (вида `postgresql+asyncpg://...`), `hh_user_agent: str`, `config_path: Path = Path("config/config.yaml")`; `def get_settings() -> Settings`.

- [ ] **Step 1:** `git init` уже сделан. Создать `pyproject.toml` (uv, пакет `vacancy_bot` в `src/`, зависимости из Tech Stack, dev-группа с pytest, pytest-asyncio, respx, testcontainers[postgres]). В `[tool.pytest.ini_options]` прописать `asyncio_mode = "auto"`, `pythonpath = ["src"]`. `.gitignore`: `.env`, `__pycache__/`, `.venv/`, `.pytest_cache/`. Запустить `uv sync`, ожидание: зависимости установлены без ошибок.
- [ ] **Step 2:** `docker-compose.yml` с сервисом `db` (`postgres:16`, volume `pgdata`, порт `5432`, пользователь, пароль и база из `.env`). `.env.example` со всеми ключами из Settings и `POSTGRES_USER/PASSWORD/DB`. Сервис `app` добавляется в Task 12.
- [ ] **Step 3: Тест.** `test_settings_reads_env(monkeypatch)`: выставить `BOT_TOKEN=x`, `OWNER_ID=42`, `DATABASE_URL=postgresql+asyncpg://u:p@h/db`, `HH_USER_AGENT=ua`; проверить `get_settings().owner_id == 42` и `config_path == Path("config/config.yaml")`.
- [ ] **Step 4:** `uv run pytest tests/test_settings.py -v`, ожидание: FAIL (модуля нет).
- [ ] **Step 5:** Реализовать `settings.py`. Повторить, ожидание: PASS.
- [ ] **Step 6:** `docker compose up -d db`, ожидание: контейнер `healthy` или `running`.
- [ ] **Step 7: Commit** `chore: project skeleton, settings, postgres in docker`.

### Task 2: Доменная модель, конфиг и словарь проекта

**Files:**
- Create: `src/vacancy_bot/domain.py`, `src/vacancy_bot/config.py`, `config/config.yaml`, `CONTEXT.md`, `tests/test_config.py`

**Interfaces:**
- Produces (`domain.py`):
  ```python
  class WorkFormat(StrEnum): REMOTE="remote"; HYBRID="hybrid"; OFFICE="office"
  class Employment(StrEnum): FULL="full"; PART="part"; INTERNSHIP="internship"; PROJECT="project"; OTHER="other"
  class VacancyStatus(StrEnum): NEW="new"; INTERESTING="interesting"; HIDDEN="hidden"; APPLIED="applied"

  @dataclass(frozen=True)
  class Posting:                     # одно объявление в одном источнике
      source: str                    # "hh" | "tbank" | "sber"
      external_id: str
      url: str
      company: str
      title: str
      cities: tuple[str, ...]        # () = не указано
      work_formats: frozenset[WorkFormat]   # пусто = не указано
      employment: Employment | None  # None = не указано
      experience_years_min: int | None      # None = не указано, 0 = без опыта
      description: str
      published_at: datetime | None
      raw: dict

  @dataclass(frozen=True)
  class Decision:
      accepted: bool
      reason: str | None             # код отсева, см. Task 3
      flags: tuple[str, ...]         # "city_unknown" | "format_unknown" | "experience_unknown" | "employment_unknown"
  ```
- Produces (`config.py`):
  ```python
  @dataclass(frozen=True)
  class Criteria:
      level_keywords: tuple[str, ...]
      direction_keywords: tuple[str, ...]
      seniority_stopwords: tuple[str, ...]
      excluded_role_patterns: tuple[str, ...]
      max_experience_years: int
      office_cities: tuple[str, ...]
      allowed_employment: frozenset[Employment]
  @dataclass(frozen=True)
  class AppConfig:
      criteria: Criteria
      company_aliases: dict[str, str]      # нормализованное имя → каноническое
      schedule_every_hours: int
      source_timeout_seconds: int
      empty_alert_after_hours: int
      sources: dict[str, dict]             # имя источника → его параметры
  def load_config(path: Path) -> AppConfig
  ```

- [ ] **Step 1:** Написать `config/config.yaml` со значениями из спеки:
  - `level_keywords`: стажер, стажёр, стажировка, intern, internship, trainee, junior, младший, начинающий;
  - `direction_keywords`: аналитик, analyst, analytics, аналитика, data scientist, data science, machine learning, ml, nlp, computer vision, cv, bi, product analyst, данных, ds;
  - `seniority_stopwords`: middle, senior, lead, ведущий, старший, главный, руководитель, head, principal;
  - `excluded_role_patterns`: 1с, 1c, бизнес-аналитик, бизнес аналитик, business analyst, системный аналитик, system analyst, аналитик продаж, маркетолог-аналитик;
  - `max_experience_years: 0`;
  - `office_cities`: Москва, Тверь;
  - `allowed_employment`: full, part, internship, project;
  - `company_aliases`: `пао сбербанк: сбер`, `сбербанк: сбер`, `сбер: сбер`, `тинькофф: т-банк`, `т-банк: т-банк`, `тбанк: т-банк`, `ао тбанк: т-банк`;
  - `schedule.every_hours: 4`, `source_timeout_seconds: 120`, `empty_alert_after_hours: 72`;
  - `sources.hh`: `queries` — два текста с `search_field: name`: (1) `аналитик OR analyst OR "data scientist" OR "machine learning" OR ML OR "data science"` с `experience: noExperience`; (2) `(стажер OR стажёр OR intern OR trainee OR стажировка) AND (аналитик OR analyst OR data OR ML)` без фильтра опыта. Плюс `per_page: 100`, `max_pages: 20`;
  - `sources.tbank.enabled: true`, `sources.sber.enabled: true`.
- [ ] **Step 2: Тест.** `test_load_shipped_config`: `load_config(Path("config/config.yaml"))` → `"Москва" in criteria.office_cities`, `criteria.max_experience_years == 0`, `Employment.PART in criteria.allowed_employment`, `company_aliases["пао сбербанк"] == "сбер"`, `"hh" in sources`.
- [ ] **Step 3:** Запустить, ожидание: FAIL. Реализовать `domain.py` и `config.py`. Ключевые слова приводить к нижнему регистру, ё → е. Ожидание: PASS.
- [ ] **Step 4:** Используя скилл `mattpocock-skills:domain-modeling`, создать `CONTEXT.md` с терминами: **Posting** (объявление в одном источнике), **Vacancy** (склеенная вакансия, одна на набор открытых объявлений с одним ключом), **Source** (источник / адаптер), **Decision** (решение фильтра: причина отсева и флаги), **Status**, **Quiet run** (тихий первый запуск), **Backlog**, **Reopened** (вакансия с тем же ключом снова открылась после закрытия).
- [ ] **Step 5: Commit** `feat: domain model, yaml config, CONTEXT.md`.

### Task 3: Фильтр

**Files:**
- Create: `src/vacancy_bot/filtering.py`, `tests/test_filtering.py`

**Interfaces:**
- Consumes: `Posting`, `Decision`, `Criteria`, `WorkFormat`, `Employment`.
- Produces: `def evaluate(posting: Posting, criteria: Criteria) -> Decision`. Коды отсева: `"seniority"`, `"excluded_role"`, `"not_relevant"`, `"experience"`, `"location"`, `"employment"`.

Алгоритм (порядок важен, срабатывает первое правило). `t` = название в нижнем регистре, ё → е. Ключевые слова ищем как целые слова или фразы (по границам слов), чтобы `ml` не совпало с `html`.
1. В `t` есть слово из `seniority_stopwords` → reject `seniority`.
2. В `t` есть шаблон из `excluded_role_patterns` → reject `excluded_role`.
3. В `t` нет ни одного слова из `direction_keywords` → reject `not_relevant`.
4. `experience_years_min` известен и `> max_experience_years` → reject `experience`.
5. Локация. Если `REMOTE in work_formats`, условие выполнено. Иначе, если `work_formats` не пуст (только гибрид или офис): если `cities` пуст, ставим флаг `city_unknown`; если ни один город не входит в `office_cities`, reject `location`. Если `work_formats` пуст, ставим флаг `format_unknown`, город не проверяем.
6. `employment` известен и не входит в `allowed_employment` → reject `employment`.
7. Флаги для неизвестных полей: `experience_unknown`, если `experience_years_min is None` и в `t` нет слова из `level_keywords`; `employment_unknown`, если `employment is None`. Accept.

- [ ] **Step 1: Тесты** (хелпер `make_posting(**overrides)` с нормальными значениями по умолчанию: название «Стажёр-аналитик данных», Москва, HYBRID, INTERNSHIP, опыт 0):
  - `test_accepts_typical_internship` → `accepted`, `flags == ()`
  - `test_rejects_senior` («Senior Data Scientist») → `reason == "seniority"`
  - `test_rejects_1c_analyst` («Стажёр аналитик 1С») → `"excluded_role"`
  - `test_rejects_business_analyst_even_in_bank` (company «Сбер», «Бизнес-аналитик стажёр») → `"excluded_role"`
  - `test_accepts_risk_analyst_in_bank` («Стажёр риск-аналитик», company «Т-Банк») → accepted
  - `test_rejects_irrelevant_title` («Стажёр frontend-разработчик») → `"not_relevant"`
  - `test_ml_does_not_match_html` («HTML-верстальщик стажёр») → `"not_relevant"`
  - `test_rejects_experience_over_zero` (`experience_years_min=1`) → `"experience"`
  - `test_remote_from_any_city` (Казань, `{REMOTE}`) → accepted
  - `test_rejects_office_outside_allowed_cities` (Казань, `{OFFICE}`) → `"location"`
  - `test_accepts_hybrid_in_tver` (Тверь, `{HYBRID}`) → accepted
  - `test_unknown_format_passes_with_flag` (`work_formats=frozenset()`, Казань) → accepted, `"format_unknown" in flags`
  - `test_unknown_city_passes_with_flag` (`cities=()`, `{HYBRID}`) → accepted, `"city_unknown" in flags`
  - `test_unknown_experience_without_level_word_flags` («Аналитик данных», `experience_years_min=None`) → accepted, `"experience_unknown" in flags`
  - `test_unknown_experience_with_level_word_no_flag` («Стажёр аналитик», `None`) → `"experience_unknown" not in flags`
  - `test_rejects_disallowed_employment` (`Employment.OTHER`) → `"employment"`
- [ ] **Step 2:** `uv run pytest tests/test_filtering.py -v`, ожидание: FAIL.
- [ ] **Step 3:** Реализовать `evaluate` по алгоритму выше.
- [ ] **Step 4:** Ожидание: все PASS.
- [ ] **Step 5: Commit** `feat: rule-based vacancy filter`.

### Task 4: Ключ склейки

**Files:**
- Create: `src/vacancy_bot/dedup.py`, `tests/test_dedup.py`

**Interfaces:**
- Produces: `def normalize_title(title: str) -> str`; `def normalize_company(name: str, aliases: dict[str, str]) -> str`; `def dedup_key(company: str, title: str, aliases: dict[str, str]) -> str`, возвращает `f"{normalize_company(...)}|{normalize_title(...)}"`.

Нормализация названия: нижний регистр, ё → е, удалить содержимое круглых скобок вместе со скобками, все не-буквенно-цифровые символы заменить пробелом, схлопнуть пробелы, обрезать. Нормализация компании: нижний регистр, ё → е, убрать кавычки «»"', схлопнуть пробелы, затем найти в `aliases` (если нет, вернуть как есть).

- [ ] **Step 1: Тесты:**
  - `test_title_variants_same_key`: `dedup_key("Сбер", "Стажёр-аналитик (Data)", A) == dedup_key("СБЕР", "стажер аналитик", A)`
  - `test_company_aliases`: `normalize_company("ПАО Сбербанк", A) == "сбер"`, `normalize_company("АО «ТБанк»", A) == "т-банк"` (алиасы — из shipped-конфига через `load_config`)
  - `test_unknown_company_kept`: `normalize_company("ООО Ромашка", A) == "ооо ромашка"`
  - `test_different_titles_differ`: ключи «Стажёр-аналитик» и «Стажёр ML-инженер» одной компании различаются
- [ ] **Step 2:** Запустить, ожидание: FAIL. Реализовать, ожидание: PASS.
- [ ] **Step 3: Commit** `feat: dedup key normalization`.

### Task 5: Схема базы, миграции, репозиторий

**Files:**
- Create: `src/vacancy_bot/db/tables.py`, `src/vacancy_bot/db/session.py`, `src/vacancy_bot/db/repo.py`, `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial.py`, `tests/conftest.py`, `tests/test_repo.py`

**Interfaces:**
- Таблицы:
  - `vacancies`: `id` PK, `dedup_key` text, `company`, `title`, `url`, `cities` text[], `work_formats` text[], `employment` text null, `experience_years_min` int null, `accepted` bool, `reject_reason` text null, `flags` text[], `status` text default `new`, `is_open` bool, `reopened` bool default false, `first_seen_at`, `last_seen_at`, `notified_at` null. **Частичный уникальный индекс** `(dedup_key) WHERE is_open`.
  - `postings`: `id` PK, `vacancy_id` FK, `source`, `external_id`, `url`, `raw` jsonb, `is_open`, `first_seen_at`, `last_seen_at`; `UNIQUE(source, external_id)`.
  - `source_runs`: `id`, `source`, `started_at`, `finished_at`, `ok` bool, `error` text null, `fetched` int.
  - `source_state`: `source` PK, `created_at`, `initialized` bool, `last_nonempty_at` null, `failing` bool, `empty_alerted` bool.
  - ORM-классы: `VacancyRow`, `PostingRow`, `SourceRunRow`, `SourceStateRow`.
- `session.py`: `def make_sessionmaker(database_url: str) -> async_sessionmaker[AsyncSession]`.
- `repo.py` (все функции принимают `session: AsyncSession` первым аргументом; коммитит вызывающий):
  ```python
  async def find_posting(s, source: str, external_id: str) -> PostingRow | None
  async def find_open_vacancy(s, dedup_key: str) -> VacancyRow | None
  async def has_closed_vacancy(s, dedup_key: str) -> bool
  async def open_postings_of_source(s, source: str) -> list[PostingRow]
  async def close_vacancies_without_open_postings(s) -> None
  async def get_vacancy(s, vacancy_id: int) -> VacancyRow | None
  async def set_status(s, vacancy_id: int, status: VacancyStatus) -> bool   # False, если нет такой вакансии
  async def unnotified(s) -> list[VacancyRow]       # accepted AND is_open AND notified_at IS NULL, по first_seen_at
  async def mark_notified(s, vacancy_id: int, at: datetime) -> None
  async def list_by_status(s, status: VacancyStatus, offset: int, limit: int) -> tuple[list[VacancyRow], int]  # только accepted AND is_open; второй элемент — всего
  async def get_source_state(s, source: str) -> SourceStateRow   # создаёт строку с initialized=False, если её нет
  async def record_run(s, source: str, started_at, finished_at, ok: bool, error: str | None, fetched: int) -> None
  ```
- `tests/conftest.py`: session-scoped фикстура `pg_url`, которая поднимает `PostgresContainer("postgres:16")` и выполняет `alembic upgrade head`. Фикстура `session`: открывает транзакцию, отдаёт её тесту и откатывает после теста.

- [ ] **Step 1:** Описать таблицы, `alembic init migrations` (async-шаблон), написать миграцию `0001_initial`, включая частичный уникальный индекс.
- [ ] **Step 2: Тесты:**
  - `test_partial_unique_allows_closed_duplicate`: две вакансии с одним `dedup_key`, первая `is_open=False`, вторая `True` → вставка проходит; третья открытая с тем же ключом → `IntegrityError`.
  - `test_unnotified_only_accepted_open`: среди отклонённой, закрытой, уже отправленной и одной подходящей `unnotified` возвращает только подходящую.
  - `test_set_status_unknown_id_returns_false`.
  - `test_list_by_status_paginates`: 12 вакансий со статусом `NEW`, `offset=10, limit=10` → 2 элемента, всего 12.
  - `test_close_vacancies_without_open_postings`: вакансия, у которой все объявления закрыты, становится `is_open=False`; вакансия с одним открытым объявлением остаётся открытой.
- [ ] **Step 3:** `uv run pytest tests/test_repo.py -v` (Docker должен быть запущен), ожидание: FAIL. Реализовать `repo.py`, ожидание: PASS.
- [ ] **Step 4: Commit** `feat: postgres schema, migrations, repository`.

### Task 6: Конвейер обработки

**Files:**
- Create: `src/vacancy_bot/pipeline.py`, `src/vacancy_bot/sources/base.py`, `tests/test_pipeline.py`

**Interfaces:**
- `sources/base.py`: `class Source(Protocol): name: str; async def fetch(self) -> list[Posting]`. Адаптер обязан бросить исключение, если хоть одна страница не загрузилась: частичный результат недопустим.
- `pipeline.py`:
  ```python
  @dataclass
  class RunResult:
      source: str
      ok: bool
      fetched: int
      error: str | None
      quiet_count: int | None   # не None, если запуск тихий: число подходящих открытых вакансий
  async def process_postings(s, source: str, postings: list[Posting], cfg: AppConfig, now: datetime) -> int | None
  async def run_source(sm: async_sessionmaker, src: Source, cfg: AppConfig, now: Callable[[], datetime]) -> RunResult
  ```

`process_postings`, для каждого `Posting` (дубли по `external_id` внутри пачки сначала схлопнуть):
1. `find_posting`. Если объявление есть: обновить `last_seen_at`. Если оно было закрыто, переоткрыть его вместе с вакансией (`is_open=True`, `reopened=True`, `notified_at=None`).
2. Если объявления нет: посчитать `dedup_key`. `find_open_vacancy` нашла вакансию → прикрепить объявление к ней, нового уведомления нет. Не нашла → `evaluate`, создать `Vacancy` с `reopened = await has_closed_vacancy(key)` и прикрепить объявление.
3. Объявления этого источника, открытые в базе, но отсутствующие в пачке, закрыть, затем `close_vacancies_without_open_postings`.
4. Если `source_state.initialized` ложно: всем подходящим открытым вакансиям, которые затронул этот прогон и у которых `notified_at IS NULL`, поставить `notified_at=now`; `initialized=True`; вернуть их число. Иначе вернуть `None`.

`run_source`: `asyncio.wait_for(src.fetch(), cfg.source_timeout_seconds)`. Если упало → `record_run(ok=False)`, `process_postings` не вызывается, возвращается `ok=False`. Если успешно → `process_postings`, обновить `last_nonempty_at`, если пачка не пустая, `record_run(ok=True)`, commit.

- [ ] **Step 1: Тесты** (с `FakeSource`, который возвращает заданный список или бросает исключение; фикстура `session` или `pg_url`):
  - `test_first_run_is_quiet`: 3 подходящих объявления → `quiet_count == 3`, `unnotified()` пуст.
  - `test_second_run_new_posting_is_unnotified`: после тихого запуска добавилось одно новое → `unnotified()` содержит ровно его.
  - `test_same_vacancy_from_two_sources_merges`: объявление hh и объявление tbank с одной компанией и одним нормализованным названием → одна вакансия, у неё два объявления.
  - `test_duplicate_postings_in_one_batch`: один `external_id` дважды в пачке → одна вакансия.
  - `test_disappeared_posting_closes_vacancy`: объявление пропало из следующей пачки → вакансия `is_open=False`.
  - `test_new_season_same_title_is_reopened`: вакансия закрыта, затем пришло новое объявление (новый `external_id`) с тем же ключом → новая вакансия, `reopened=True`, она в `unnotified()`.
  - `test_failed_fetch_closes_nothing`: источник бросил исключение → `ok=False`, все ранее открытые вакансии остались открытыми, в `source_runs` есть строка с `ok=False`.
  - `test_rejected_posting_stored_with_reason`: senior-вакансия сохранена с `accepted=False`, `reject_reason="seniority"`.
- [ ] **Step 2:** Запустить, ожидание: FAIL. Реализовать, ожидание: PASS.
- [ ] **Step 3: Commit** `feat: ingestion pipeline with merge, close/reopen, quiet first run`.

### Task 7: Адаптер hh.ru

**Files:**
- Create: `src/vacancy_bot/sources/hh.py`, `tests/fixtures/hh_page.json`, `tests/test_source_hh.py`

**Interfaces:**
- Produces: `class HHSource: name = "hh"; def __init__(self, http: httpx.AsyncClient, user_agent: str, params: dict); async def fetch(self) -> list[Posting]`.

Для каждого запроса из `params["queries"]` листать страницы `GET https://api.hh.ru/vacancies?text=…&search_field=name&per_page=…&page=N[&experience=…]` до `pages` из ответа или `max_pages`, с паузой 0.3 с между страницами. Ответ не 2xx → исключение. Схлопнуть по `id`. Маппинг элемента `items[]`:
- `external_id=id`, `url=alternate_url`, `company=employer.name`, `title=name`, `cities=(area.name,)`;
- `work_formats`: если есть поле `work_format` (список `{id}`), то `REMOTE→REMOTE`, `HYBRID→HYBRID`, `ON_SITE`/`FIELD_WORK→OFFICE`. Иначе `schedule.id == "remote"` → `{REMOTE}`, любое другое значение `schedule` → `{OFFICE}`; `schedule` нет → пусто;
- `employment.id`: `full→FULL`, `part→PART`, `probation→INTERNSHIP`, `project→PROJECT`, `volunteer→OTHER`;
- `experience.id`: `noExperience→0`, `between1And3→1`, `between3And6→3`, `moreThan6→6`;
- `description` = `snippet.requirement + "\n" + snippet.responsibility` (то, что есть), `published_at` = `published_at`, `raw` = весь элемент.

- [ ] **Step 1:** Получить реальный пример: `curl -H "HH-User-Agent: <из .env>" "https://api.hh.ru/vacancies?text=стажер%20аналитик&search_field=name&per_page=5"` и сохранить в `tests/fixtures/hh_page.json`. Если в выдаче нет `work_format`, вручную добавить его в один элемент, чтобы проверить обе ветки маппинга.
- [ ] **Step 2: Тесты** (respx мокает `api.hh.ru`):
  - `test_maps_fixture_items`: число `Posting` равно `len(items)`; поля первого элемента совпадают со значениями в фикстуре; `source == "hh"`.
  - `test_work_format_preferred_over_schedule`: элемент с `work_format=[{"id":"HYBRID"}]` и `schedule.id="fullDay"` → `{HYBRID}`.
  - `test_dedups_ids_across_queries`: оба запроса возвращают один и тот же `id` → один `Posting`.
  - `test_paginates_until_pages`: `pages=2` → 2 запроса на каждый query.
  - `test_raises_on_http_error`: вторая страница отвечает 503 → `fetch` бросает исключение.
  - `test_sends_hh_user_agent`: в запросе есть заголовок `HH-User-Agent`.
- [ ] **Step 3:** Запустить, ожидание: FAIL. Реализовать, ожидание: PASS.
- [ ] **Step 4: Commit** `feat: hh.ru source adapter`.

### Task 8: Адаптер Т-Банка

**Files:**
- Create: `src/vacancy_bot/sources/tbank.py`, `tests/fixtures/tbank_list.json`, `tests/test_source_tbank.py`

**Interfaces:**
- Produces: `class TBankSource: name = "tbank"; def __init__(self, http: httpx.AsyncClient, params: dict); async def fetch(self) -> list[Posting]`. `company` всегда `"Т-Банк"`.

- [ ] **Step 1: Разведка.** Открыть карьерный сайт Т-Банка (раздел стажировок и вакансий) во встроенном браузере и найти в сетевых запросах JSON-эндпоинт со списком вакансий и его параметры (пагинация, фильтр по направлению). Записать эндпоинт и параметры в docstring модуля. Сохранить реальный ответ в `tests/fixtures/tbank_list.json`. **Если JSON-эндпоинта нет и нужен Playwright, остановиться и сообщить владельцу**, не добавляя Playwright самостоятельно.
- [ ] **Step 2: Тесты** (respx):
  - `test_maps_fixture`: число `Posting` равно числу вакансий в фикстуре; `title`, `url`, `external_id` первой вакансии совпадают с фикстурой; `company == "Т-Банк"`; поля, которых нет на сайте, равны `None`, `()` или пустому множеству, а не выдуманным значениям.
  - `test_paginates` (если у эндпоинта есть пагинация).
  - `test_raises_on_http_error`.
- [ ] **Step 3:** Запустить, ожидание: FAIL. Реализовать, ожидание: PASS.
- [ ] **Step 4: Commit** `feat: T-Bank careers source adapter`.

### Task 9: Адаптер Сбера

Точно как Task 8, но для карьерного сайта Сбера: `class SberSource: name = "sber"`, `company` всегда `"Сбер"`, файлы `src/vacancy_bot/sources/sber.py`, `tests/fixtures/sber_list.json`, `tests/test_source_sber.py`. Те же три теста и то же правило остановки, если нужен Playwright.

- [ ] **Step 1:** Разведка и фикстура.
- [ ] **Step 2:** Тесты, ожидание: FAIL.
- [ ] **Step 3:** Реализовать, ожидание: PASS.
- [ ] **Step 4: Commit** `feat: Sber careers source adapter`.

### Task 10: Бот — сообщения, кнопки, команды, рассылка

**Files:**
- Create: `src/vacancy_bot/bot/messages.py`, `src/vacancy_bot/bot/handlers.py`, `src/vacancy_bot/bot/notifier.py`, `tests/test_bot_messages.py`, `tests/test_bot_handlers.py`, `tests/test_notifier.py`

**Interfaces:**
- `messages.py`:
  ```python
  FLAG_TEXT = {"city_unknown": "⚠️ город не указан", "format_unknown": "⚠️ формат не указан",
               "experience_unknown": "⚠️ опыт не указан", "employment_unknown": "⚠️ занятость не указана"}
  def format_vacancy(v: VacancyRow) -> str            # HTML
  def status_keyboard(vacancy_id: int) -> InlineKeyboardMarkup
  def parse_status_callback(data: str) -> tuple[int, VacancyStatus] | None
  def format_quiet_summary(source: str, count: int) -> str
  def format_page(rows: list[VacancyRow], total: int, offset: int) -> str
  def page_keyboard(kind: str, offset: int, total: int, page_size: int) -> InlineKeyboardMarkup | None
  ```
  Формат сообщения: первая строка `🔁 снова открыта`, если `reopened`; затем `<b>{company}</b> — {title}`; строка `📍 {города через запятую или «город не указан»} · {форматы: удалёнка/гибрид/офис}`; строки флагов из `FLAG_TEXT`; ссылка `url`. Callback кнопок: `st:{id}:{status}` для `interesting` («⭐ Интересно»), `hidden` («🙈 Скрыть»), `applied` («✅ Откликнулся»). Итог тихого запуска: `Найдено {count} открытых подходящих вакансий ({source}). Посмотреть: /backlog`.
- `handlers.py`: `def build_router(sm: async_sessionmaker, owner_id: int) -> Router`. Outer-middleware молча отбрасывает message и callback, если `from_user.id != owner_id`. Команды: `/start` (короткая справка), `/backlog` (статус `NEW`), `/interesting`, `/applied`, по 10 на страницу, кнопка «Дальше» с callback `pg:{kind}:{offset}`. Callback статуса: `set_status`. Если вакансии нет или callback кривой → `answer("Вакансия не найдена")`. Если всё хорошо → `answer("Готово")` и убрать клавиатуру у сообщения.
- `notifier.py`: `async def send_unnotified(bot: Bot, sm: async_sessionmaker, owner_id: int, now: Callable[[], datetime]) -> int`. Отправляет по одной вакансии с паузой 1 с. После каждой успешной отправки сразу `mark_notified` и commit. При `TelegramRetryAfter` ждать `retry_after` и повторить один раз. При любой другой ошибке остановить рассылку (оставшиеся уйдут в следующий раз). Возвращает число отправленных.

- [ ] **Step 1: Тесты:**
  - `test_format_vacancy_reopened_and_flags`: вакансия с `reopened=True`, `flags=["format_unknown"]` → текст начинается с `🔁 снова открыта` и содержит `⚠️ формат не указан`.
  - `test_format_vacancy_escapes_html`: название `"<script>"` экранировано.
  - `test_parse_status_callback_roundtrip` и `test_parse_status_callback_garbage` (`"st:abc:zzz"` → `None`).
  - `test_status_callback_unknown_vacancy`: callback с несуществующим id → ответ «Вакансия не найдена», без исключения (тест вызывает обработчик с фейковым `CallbackQuery`).
  - `test_non_owner_ignored`: апдейт от чужого id не доходит до обработчика.
  - `test_notifier_keeps_unsent_on_failure`: 3 неотправленные вакансии, фейковый бот падает на второй (сетевая ошибка) → функция вернула 1, у первой `notified_at` выставлен, у второй и третьей остался `NULL`.
  - `test_notifier_retries_after_retry_after`: первая отправка бросает `TelegramRetryAfter(retry_after=0)`, повтор успешен → вакансия помечена отправленной.
- [ ] **Step 2:** Запустить, ожидание: FAIL. Реализовать, ожидание: PASS.
- [ ] **Step 3: Commit** `feat: telegram bot messages, status buttons, backlog, notifier`.

### Task 11: Контроль поломок источников

**Files:**
- Create: `src/vacancy_bot/health.py`, `tests/test_health.py`

**Interfaces:**
- Produces: `async def health_alert(s, result: RunResult, cfg: AppConfig, now: datetime) -> str | None`. Возвращает текст для владельца или `None` и обновляет `source_state`.

Правила:
- `result.ok == False` и `failing` было ложно → `failing=True`, текст `❗ Источник {source} упал: {error}`. Если `failing` уже было истинно → `None`, чтобы не повторять алерт каждый запуск.
- `result.ok == True` и `failing` было истинно → `failing=False`, текст `✅ Источник {source} снова работает`.
- `result.ok == True`, `last_nonempty_at or created_at` старше `empty_alert_after_hours` и `empty_alerted` ложно → `empty_alerted=True`, текст `⚠️ Источник {source} ничего не возвращает больше {N} ч — возможно, сломался парсер`. Непустая выборка сбрасывает `empty_alerted`.

- [ ] **Step 1: Тесты:** `test_alerts_once_on_failure` (два неудачных запуска подряд → текст только в первый раз), `test_recovery_message`, `test_empty_too_long_alerts_once`, `test_nonempty_resets_empty_alert`.
- [ ] **Step 2:** Запустить, ожидание: FAIL. Реализовать, ожидание: PASS.
- [ ] **Step 3: Commit** `feat: source health alerts`.

### Task 12: Сборка приложения, расписание, Docker

**Files:**
- Create: `src/vacancy_bot/sources/registry.py`, `src/vacancy_bot/app.py`, `Dockerfile`, `tests/test_app.py`
- Modify: `docker-compose.yml` (сервис `app`), `CLAUDE.md` (раздел «Запуск»)

**Interfaces:**
- `registry.py`: `def build_sources(cfg: AppConfig, http: httpx.AsyncClient, settings: Settings) -> list[Source]`, только источники с `enabled` (у hh `enabled` по умолчанию истинно).
- `app.py`:
  ```python
  async def run_cycle(bot: Bot, sm, sources: list[Source], cfg: AppConfig, owner_id: int, now) -> None
  async def main() -> None
  ```
  `run_cycle`: для каждого источника `run_source`. Если `quiet_count is not None`, отправить `format_quiet_summary`. Затем `health_alert`, если вернулся текст, отправить его. После всех источников `send_unnotified`. Ошибка одного источника не мешает остальным.
  `main`: загрузить настройки и конфиг, `make_sessionmaker`, `Bot` (parse_mode HTML), `Dispatcher` с `build_router`, `AsyncIOScheduler` с интервалом `schedule_every_hours` и `max_instances=1`, первый `run_cycle` сразу при старте, затем `dp.start_polling(bot)`.

- [ ] **Step 1: Тест** `test_run_cycle_isolates_source_failures`: два фейковых источника, первый бросает исключение, второй возвращает одно подходящее объявление (тихий запуск уже был) → фейковый бот получил алерт о первом источнике и сообщение с вакансией из второго.
- [ ] **Step 2:** Запустить, ожидание: FAIL. Реализовать `registry.py` и `app.py`, ожидание: PASS.
- [ ] **Step 3:** `Dockerfile` (python:3.12-slim, uv, `uv sync --frozen --no-dev`, команда `alembic upgrade head && python -m vacancy_bot.app`). Сервис `app` в compose: `depends_on: db` (condition `service_healthy`, healthcheck через `pg_isready`), `env_file: .env`, `restart: unless-stopped`, том `./config:/app/config:ro`, чтобы критерии можно было менять без пересборки.
- [ ] **Step 4:** Полный прогон `uv run pytest -v`, ожидание: все PASS.
- [ ] **Step 5: Ручная проверка с владельцем.** Владелец заполняет `.env` (токен бота от @BotFather, свой Telegram ID, `HH_USER_AGENT` со своим контактом). Затем `docker compose up --build`. Ожидание: в логах миграции применены и первый цикл прошёл; в Telegram пришли итоговые сообщения тихого запуска по каждому источнику; `/backlog` показывает вакансии; кнопка «⭐ Интересно» меняет статус, и вакансия появляется в `/interesting`.
- [ ] **Step 6:** Дописать в `CLAUDE.md` раздел «Запуск»: `docker compose up --build`, где лежат `.env` и `config/config.yaml`, как запускать тесты (нужен Docker).
- [ ] **Step 7: Commit** `feat: app wiring, scheduler, docker deployment`.

---

## После плана (не входит в эту реализацию)

Завести в GitHub Issues по задаче на каждый следующий источник, по порядку: Альфа-Банк, ВТБ, Яндекс, VK, Ozon, Авито. Каждый делается по образцу Task 8.
