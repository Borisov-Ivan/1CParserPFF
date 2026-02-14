# 1CParserPFF

Инструмент для анализа файлов замеров 1С (`.pff`) в двух режимах:

- `TRACE` — анализ поведения и причинно-следственных связей.
- `PERF` — анализ производительности и узких мест.

## Быстрый старт

```bash
python src/pff_parser.py tests/reference.pff --mode TRACE --trace-detail compact
python src/pff_parser.py tests/reference.pff --mode TRACE --trace-detail full
python src/pff_parser.py tests/reference.pff --mode PERF --threshold 10
```

## TRACE v7

TRACE поддерживает только две детализации:

- `compact` (по умолчанию)
- `full`

Секции TRACE-отчёта:

1. `=== TRACE [FULL|COMPACT] ===`
2. `=== TRACE META ===`
3. `=== TRACE COVERAGE ===`
4. `=== MODULES MAP ===`
5. `=== EXECUTION FLOW ===`
6. `=== CALL INDEX ===`
7. `=== MODULES ===`
8. `=== TRACE REPRODUCE ===`

Соглашения:

- `#123` — подтверждённый факт (FACT)
- `?123` — эвристическая связь (INFERRED)
- `M01:Line` — ссылка на модуль по алиасу из `MODULES MAP`

## CLI

```bash
python src/pff_parser.py <input.pff> [output.txt] [options]
```

Основные опции:

- `--mode {TRACE,PERF}`
- `--trace-detail {full|compact}` (`normal` поддержан как deprecated alias -> `compact`)
- `--threshold N` (только PERF)
- `--no-context`
- `--no-model-prompt`
- `--no-compact` (deprecated alias для `--trace-detail full`)
- `--no-expand-modules` (deprecated no-op)

Если `output` не указан, имя формируется автоматически:

- `*_TRACE_COMPACT.txt`
- `*_TRACE_FULL.txt`
- `*_PERF.txt`
