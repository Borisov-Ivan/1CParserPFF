# 1CParserPFF

Инструмент для анализа файлов замеров 1С (`.pff`) в двух режимах:

- `TRACE` — анализ поведения и причинно-следственных связей.
- `PERF` — анализ производительности и узких мест.

## Быстрый старт

```bash
python src/pff_parser.py tests/reference.pff
```

TRACE по умолчанию запускается в детализации `normal`.

## Режимы

- `TRACE`: отвечает на вопрос «что произошло».
- `PERF`: отвечает на вопрос «почему медленно».

Важно:

- Параметр `--trace-detail` применяется только к `--mode TRACE`.
- Параметр `--threshold` применяется только к `--mode PERF`.

## TRACE detail

Поддерживаются три профиля детализации:

- `full` — максимум деталей, минимальная фильтрация.
- `normal` — основной режим по умолчанию.
- `compact` — агрессивное сжатие для triage.

Пример:

```bash
python src/pff_parser.py tests/reference.pff --mode TRACE --trace-detail compact
```

## CLI

```bash
python src/pff_parser.py <input.pff> [output.txt] [options]
```

Основные опции:

- `--mode {TRACE,PERF}`
- `--trace-detail {full,normal,compact}`
- `--entry "Module:Line"`
- `--main-block N`
- `--threshold N` (только PERF)
- `--no-context`
- `--no-expand-modules`
- `--no-model-prompt`
- `--no-compact` — deprecated alias для `--trace-detail full`

Если одновременно заданы `--trace-detail` и `--no-compact`, используется `full` с предупреждением.

## TRACE v6 формат

TRACE-отчёт содержит секции:

1. `=== TRACE [FULL|NORMAL|COMPACT] ===`
2. `=== TRACE META ===`
3. `=== TRACE COVERAGE ===`
4. `=== EXECUTION FLOW (эвристическая реконструкция) ===`
5. `=== CALL MAP ===`
6. `=== MODULES (справочник модулей) ===`
7. `=== TRACE REPRODUCE ===`

Ключевые свойства TRACE v6:

- сквозные `EventID` между `EXECUTION FLOW`, `CALL MAP`, `MODULES`;
- явная маркировка достоверности: `FACT` и `INFERRED`;
- покрытие скрытий в `TRACE COVERAGE`;
- команды воспроизведения в `TRACE REPRODUCE`.

## GUI

В GUI для TRACE доступен селектор `Детализация TRACE: full / normal / compact`.

Чекбокс «Умное сжатие» удалён; вместо него используется `trace_detail`.
