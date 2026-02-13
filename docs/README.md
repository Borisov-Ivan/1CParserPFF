# Документация 1CParserPFF

## 1. Назначение

`1CParserPFF` преобразует `.pff` в человекочитаемые отчёты для анализа логики (`TRACE`) и производительности (`PERF`).

## 2. Быстрый старт

### CLI

```bash
python src/pff_parser.py tests/reference.pff
python src/pff_parser.py tests/reference.pff --mode TRACE --trace-detail full
python src/pff_parser.py tests/reference.pff --mode PERF --threshold 10
```

### GUI

1. Выберите файл `.pff`.
2. Выберите режим `TRACE` или `PERF`.
3. Для `TRACE` выберите `Детализация TRACE` (`full/normal/compact`).
4. Нажмите «Сформировать».

## 3. TRACE vs PERF

- `TRACE` — анализ поведения и причин.
- `PERF` — анализ времени и узких мест.

Не используйте TRACE как замену PERF для оптимизации времени.

## 4. TRACE detail

- `full`: минимальная фильтрация, максимум контекста.
- `normal`: баланс сигнала/шума (default).
- `compact`: агрессивное сжатие для сравнения прогонов.

Legacy:

- `--no-compact` поддерживается как deprecated alias для `--trace-detail full`.

## 5. Формат TRACE v6

Порядок секций:

1. `=== TRACE [FULL|NORMAL|COMPACT] ===`
2. `=== TRACE META ===`
3. `=== TRACE COVERAGE ===`
4. `=== EXECUTION FLOW (эвристическая реконструкция) ===`
5. `=== CALL MAP ===`
6. `=== MODULES (справочник модулей) ===`
7. `=== TRACE REPRODUCE ===`

## 6. Семантика данных

- `EventID` сквозной для всех TRACE-секций.
- `FACT` — подтверждённые данные трассы.
- `INFERRED` — эвристические связи.

## 7. CLI параметры

- `--mode {TRACE,PERF}`
- `--trace-detail {full,normal,compact}` (только TRACE)
- `--threshold N` (только PERF)
- `--entry`, `--main-block`
- `--no-context`, `--no-expand-modules`, `--no-model-prompt`
- `--no-compact` (deprecated)

## 8. Проверки

Минимальный набор:

```bash
python -m py_compile src/pff_parser.py
python -m unittest discover -s tests -p "test_*.py"
```
