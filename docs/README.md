# Документация 1CParserPFF

## 1. Назначение

`1CParserPFF` преобразует `.pff` в отчёты двух типов:

- `TRACE` — сценарий выполнения (логика, причинно-следственные связи).
- `PERF` — производительность (проблемы и горячие точки).

## 2. Быстрый старт

```bash
python src/pff_parser.py tests/reference.pff --mode TRACE --trace-detail compact
python src/pff_parser.py tests/reference.pff --mode TRACE --trace-detail full
python src/pff_parser.py tests/reference.pff --mode PERF --threshold 10
```

## 3. TRACE v7

### Детализация TRACE

- `compact` (по умолчанию)
- `full`

`normal` не используется и автоматически переводится в `compact` с предупреждением.

### Порядок секций TRACE

1. `=== TRACE [FULL|COMPACT] ===`
2. `=== TRACE META ===`
3. `=== TRACE COVERAGE ===`
4. `=== MODULES MAP ===`
5. `=== EXECUTION FLOW ===`
6. `=== CALL INDEX ===`
7. `=== MODULES ===`
8. `=== TRACE REPRODUCE ===`

### Формат ссылок

- `#NNN` — факт
- `?NNN` — эвристика
- `MNN:Line` — ссылка на модуль через алиасы `MODULES MAP`

## 4. CLI параметры

- `--mode {TRACE,PERF}`
- `--trace-detail {full|compact}`
- `--threshold N` (только PERF)
- `--no-context`
- `--no-model-prompt`
- `--no-compact` — deprecated alias для `full`
- `--no-expand-modules` — deprecated no-op

## 5. Выходные файлы

Если путь отчёта не указан, имя файла формируется автоматически:

- `*_TRACE_COMPACT.txt`
- `*_TRACE_FULL.txt`
- `*_PERF.txt`
