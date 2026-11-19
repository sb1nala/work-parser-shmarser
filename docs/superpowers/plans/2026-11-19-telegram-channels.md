# Telegram-каналы как источник: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** бот читает три публичных Telegram-канала через `t.me/s/<канал>` и пропускает их посты через существующие фильтр, склейку и рассылку.

**Architecture:** новый адаптер `TelegramSource`, по экземпляру на канал, с именем источника `tg_<канал>`. Он разбирает HTML веб-версии канала в `Posting`. В `Posting` появляется флаг `require_level_keyword`: для постов из каналов фильтр требует слово уровня в названии.

**Tech Stack:** Python 3.12, httpx, beautifulsoup4 (парсер `html.parser`, без нативной сборки), respx, pytest.

**Spec:** `docs/superpowers/specs/2026-11-19-telegram-channels-design.md` (основной: `docs/superpowers/specs/2026-11-18-vacancy-bot-design.md`)

## Global Constraints

- Каналы: `newdirections`, `not_boring_ds_jobs`, `young_june`; список в `config/config.yaml` → `sources.telegram.channels`.
- Окно `window_days: 14`; имя источника `tg_<канал>`; компания по умолчанию `Telegram @<канал>`.
- `require_level_keyword: true` по умолчанию для каналов; причина отсева `no_level_keyword`. Для сайтов поведение фильтра не меняется.
- Без частичных результатов: не-2xx, страница без разметки постов, нечитаемая дата, исчерпанный `max_pages` до выхода за окно — один `RuntimeError` с именем канала (`raise ... from exc`).
- Опыт и занятость у постов не выдумываем (`None`).
- Ноль платных API; без входа в аккаунт Telegram.
- Коммиты без Co-Authored-By и любой атрибуции Claude/ИИ.
- uv не в PATH: команды через `python -m uv ...`; тесты БД требуют запущенный Docker.

## Review Focus

1. **Пост без текста** (только фото или видео): пропускается, а не роняет источник. Тест: Task 2 `test_skips_posts_without_text`.
2. **Канал переименован или удалён** (страница без контейнера постов): `RuntimeError`, и контроль поломок пришлёт алерт. Тест: Task 2 `test_page_without_posts_container_raises`.
3. **Один и тот же пост на двух страницах** выдачи: одна запись. Тест: Task 2 `test_dedups_posts_across_pages`.
4. **В канале нет постов за окно, но канал живой:** пустой список без ошибки. Тест: Task 2 `test_all_posts_older_than_window_returns_empty`.
5. **Реклама со словом «analytics» без слова уровня** («Осталось несколько дней, чтобы подать заявку на AI Analytics Week») отсеивается с `no_level_keyword`. Тест: Task 1 `test_channel_post_without_level_word_rejected`.

---

### Task 1: Флаг `require_level_keyword` и правило фильтра

**Files:**
- Modify: `src/vacancy_bot/domain.py` (Posting), `src/vacancy_bot/filtering.py` (evaluate)
- Test: `tests/test_filtering.py`

**Interfaces:**
- Produces: `Posting.require_level_keyword: bool = False` — последнее поле, со значением по умолчанию, поэтому существующие конструкторы не ломаются. Новая причина отсева `"no_level_keyword"`.

- [ ] **Step 1: Тесты** в `tests/test_filtering.py` (хелпер `make_posting` уже есть):
  - `test_channel_post_without_level_word_rejected`: `make_posting(title="Осталось несколько дней, чтобы подать заявку на AI Analytics Week", experience_years_min=None, require_level_keyword=True)` → `reason == "no_level_keyword"`.
  - `test_channel_post_with_level_word_accepted`: `title="Стажёр-аналитик данных"`, `experience_years_min=None`, `require_level_keyword=True` → `accepted`.
  - `test_site_post_unaffected_by_level_rule`: `title="Аналитик данных"`, `experience_years_min=None`, флаг по умолчанию → `accepted` и `"experience_unknown" in flags` (как раньше).
- [ ] **Step 2:** `python -m uv run pytest tests/test_filtering.py -v`, ожидание: новые тесты FAIL.
- [ ] **Step 3:** Добавить поле в `Posting`. В `evaluate` сразу после проверки `not_relevant` (и до `experience`) вставить: если `posting.require_level_keyword` и в `t` нет слова из `criteria.level_keywords` → `_reject("no_level_keyword")`. Дописать `no_level_keyword` в комментарий к кодам отсева в `domain.py`.
- [ ] **Step 4:** Ожидание: `tests/test_filtering.py` весь PASS, затем полный прогон `python -m uv run pytest -W error`.
- [ ] **Step 5: Commit** `feat(filter): optional level-word requirement for channel posts`.

### Task 2: Адаптер `TelegramSource`, конфиг, подключение

**Files:**
- Create: `src/vacancy_bot/sources/telegram.py`, `tests/test_source_telegram.py`, `tests/fixtures/tg_newdirections_page.html`
- Modify: `config/config.yaml` (`sources.telegram`), `src/vacancy_bot/sources/registry.py` (`build_sources`), `tests/test_app.py` (тесты registry), `pyproject.toml` + `uv.lock` (beautifulsoup4), `CLAUDE.md` («Запуск»: одна строка про каналы)

**Interfaces:**
- Consumes: `Posting` с `require_level_keyword` (Task 1), `WorkFormat`, протокол `Source`.
- Produces: `class TelegramSource: def __init__(self, http: httpx.AsyncClient, channel: str, params: dict, delay: float = 0.3, now: Callable[[], datetime] = <utc now>)`; `name = f"tg_{channel}"`; `async def fetch(self) -> list[Posting]`. `build_sources` создаёт по `TelegramSource` на каждый канал из `sources.telegram.channels`, если `enabled`; клиент общий (`http`).

Конфиг:
```yaml
  telegram:
    enabled: true
    window_days: 14
    max_pages: 10
    require_level_keyword: true
    channels: [newdirections, not_boring_ds_jobs, young_june]
```

Получение: `GET https://t.me/s/<канал>`, затем `GET https://t.me/s/<канал>?before=<наименьший номер поста на странице>`. Пауза `delay` между запросами. Остановиться, когда на странице есть пост старше окна (`now() - window_days`) или страница пуста. Если `max_pages` исчерпан раньше, `RuntimeError`. Посты старше окна в результат не попадают.

Разбор одного поста (`div.tgme_widget_message` с `data-post="<канал>/<номер>"`):
- `external_id` — номер; `url` — `https://t.me/<канал>/<номер>`; `published_at` — из `time[datetime]` (ISO с поясом; без пояса считать UTC);
- текст из `.tgme_widget_message_text`, где `<br>` — перевод строки. Нет текста — пост пропускается;
- название — первая непустая строка, эмодзи и пробелы по краям обрезаются;
- если в названии есть разделитель ` / `, ` — ` или ` | ` (первое вхождение), левая часть — должность, правая — компания; иначе компания `Telegram @<канал>`;
- строка, начинающаяся с 🗺 или 📍: части через «.» или «,»; «удалённо»/«удаленно»/«удалёнка»/«удаленка»/«remote» → REMOTE, «гибрид»/«hybrid» → HYBRID, «офис»/«office» → OFFICE, остальное (непустое) — города;
- `description` — текст без первой строки; `experience_years_min=None`, `employment=None`; `require_level_keyword` из конфига; `raw = {"channel", "post", "text"[:2000]}`.

- [ ] **Step 1: Фикстура.** Сохранить реальную страницу `https://t.me/s/newdirections` (обычный UA браузера, один запрос), урезать до 4–6 постов: хотя бы один со структурой «Должность / Компания» и строкой 🗺, одна реклама, один пост без текста (если нет — добавить вручную блок с медиа без `.tgme_widget_message_text`). Убрать из фикстуры персональные данные (имена, телефоны, e-mail контактов в тексте постов заменить заглушками). В шапке файла — HTML-комментарий с URL и датой снимка.
- [ ] **Step 2: Тесты** (respx, без сети; `now` фиксирован так, чтобы посты фикстуры были внутри окна, если тесту не нужно обратное):
  - `test_maps_structured_post`: пост «Должность / Компания» → `title` и `company` разобраны, `cities`/`work_formats` из строки 🗺, `url == "https://t.me/newdirections/<номер>"`, `published_at.tzinfo` не None, `experience_years_min is None`, `employment is None`, `require_level_keyword is True`, `source == "tg_newdirections"`;
  - `test_company_defaults_to_channel`: пост без разделителя → `company == "Telegram @newdirections"`;
  - `test_skips_posts_without_text`;
  - `test_paginates_with_before_until_window`: первая страница вся в окне, вторая содержит пост старше окна → ровно 2 запроса, второй с `before=<наименьший номер первой>`; старый пост не в результате;
  - `test_dedups_posts_across_pages`;
  - `test_all_posts_older_than_window_returns_empty`;
  - `test_page_without_posts_container_raises` (HTML без `tgme_widget_message`, но не «пустая лента») → `RuntimeError` с именем канала. Различить «канал живой, постов в окне нет» (есть посты, но старые) и «нет ленты вообще» (ни одного `tgme_widget_message` на первой странице) → второе ошибка;
  - `test_raises_on_http_error` (503), `test_raises_on_bad_date`, `test_raises_when_max_pages_exhausted`;
  - в `tests/test_app.py`: `build_sources` с `sources.telegram.enabled: true` и двумя каналами → два `TelegramSource` с именами `tg_<канал>`; с `enabled: false` → ни одного.
- [ ] **Step 3:** `python -m uv run pytest tests/test_source_telegram.py tests/test_app.py -v`, ожидание: FAIL.
- [ ] **Step 4:** Добавить зависимость `beautifulsoup4` (`python -m uv add beautifulsoup4`). Реализовать адаптер, конфиг, подключение в registry, строку в `CLAUDE.md`.
- [ ] **Step 5:** Ожидание: тесты PASS; полный прогон `python -m uv run pytest -W error`.
- [ ] **Step 6: Живая проверка.** Из временного скрипта вне репозитория прогнать `TelegramSource` по трём каналам: число постов в окне, сколько принимает `evaluate` с конфигом проекта, первые 5 принятых названий. Стек docker compose не трогать.
- [ ] **Step 7: Commit** `feat(sources): Telegram channels via t.me/s public preview`.
