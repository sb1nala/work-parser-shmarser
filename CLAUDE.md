## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for `sb1nala/work-parser-shmarser`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Запуск

- Скопировать `.env.example` в `.env` и заполнить: `BOT_TOKEN` (от @BotFather), `OWNER_ID` (свой Telegram ID), `HH_USER_AGENT` (с контактом), `POSTGRES_*`. `HH_ACCESS_TOKEN` нужен только если включить источник hh.
- `docker compose up --build` поднимает Postgres и бота; при старте применяются миграции (`alembic upgrade head`), первый цикл сбора запускается сразу, дальше раз в `schedule.every_hours` часов.
- Критерии отбора, источники и расписание лежат в `config/config.yaml`; каталог примонтирован в контейнер, после правки достаточно `docker compose restart app`, пересборка не нужна. Источник hh выключен (`enabled: false`): анонимный API hh.ru отдаёт 403. Чтобы включить: токен приложения с dev.hh.ru в `HH_ACCESS_TOKEN` и `sources.hh.enabled: true`. hh отдаёт по запросу не больше 2000 вакансий, поэтому запросы сужены по региону и удалёнке; если в логе ошибка hh «narrow the query», запрос нужно раздробить дальше.
- Для Т-Банка в образ вшит корневой сертификат «Russian Trusted Root CA» (`certs/russian_trusted_root_ca.pem`, используется только для запросов к Т-Банку и Альфа-Банку); он действует до 2032-02-27, к этому сроку его нужно обновить.
- Telegram-каналы (`sources.telegram` в конфиге) читаются через публичное превью `t.me/s/<канал>` без токенов; список каналов, окно `window_days` и `max_pages` правятся в `config/config.yaml`, посты без слова уровня в названии отсеиваются (`require_level_keyword`).
- Если убрать канал из `sources.telegram.channels`, его уже сохранённые вакансии остаются как есть (не закрываются и не удаляются).
- Тесты: `uv run pytest -v` (нужен запущенный Docker: БД поднимается через testcontainers).
