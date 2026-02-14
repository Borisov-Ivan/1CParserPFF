#!/usr/bin/env python3
"""
Парсер PFF (1С Performance) — двухрежимный вывод: TRACE + PERF.

Следует ТЗ Парсер.md и Идеальный формат отчёта.md:
- TRACE: полный хронологический лог (без сжатия, для reasoning/отладки)
- PERF: дерево критического пути + Hotspots (с фильтрацией шума)

Поддерживает: --threshold, --no-compact, --no-context, --no-expand-modules
"""

import sys
import os
import argparse
import re
import shlex
from collections import defaultdict

# ==================================================================================================
# 1. CORE PARSER (STREAM TOKENIZER)
# ==================================================================================================


# Контекст выполнения (1=клиент, 2=сервер, 3–6=клиент → вызов сервера)
CONTEXT_LABELS = {
    1: 'C', 2: 'S',
    3: 'C→S', 4: 'C→S', 5: 'C→S', 6: 'C→S',
}

SESSION_TYPES = {
    2: "Сервер",
    32: "Тонкий клиент",
    64: "Сервер",
}

# --- v4: Утилитные константы для IssueAnalyzer и ExecutionFlowBuilder ---

CALLBACK_PATTERNS = [
    "ВыполнитьОбработкуОповещения",
    "ВернутьРезультат",
    "ОповеститьОВыборе",
    "ОповеститьОЗакрытии",
    "Закрыть(Истина)",
]

ISSUE_TYPE_PATTERNS = {
    "HTTP": ["Соединение.Получить", "Соединение.ОтправитьДляОбработки",
             "HTTPСоединение", "Соединение.Вызвать"],
    "SQL": ["Запрос.Выполнить", "Выборка.Следующий", "Выбрать()"],
    "Resource": ["ПолучитьОбщийМакет", "Преобразовать(Формат"],
}

STRUCTURAL_NOISE = ["КонецЕсли", "КонецЦикла", "КонецПроцедуры", "КонецФункции"]
CONTROL_FLOW_PREFIXES = (
    "Если",
    "Иначе",
    "Для",
    "Пока",
    "Попытка",
    "Исключение",
)
TERMINAL_FLOW_STATEMENTS = ("Возврат;", "Продолжить;", "Прервать;")

# Промпты для модели в заголовке отчёта (при включённой опции на форме)
TRACE_MODEL_PROMPT = """=== ПРОМПТ ДЛЯ МОДЕЛИ (TRACE) ===
Ты анализируешь отчёт трассировки выполнения 1С (PFF TRACE v4).

Структура отчёта:
- EXECUTION FLOW: хронологическое дерево вызовов (реконструировано эвристически, точность ~90%). Показывает что вызвало что, в порядке выполнения. Колбэк-цепочки (ВыполнитьОбработкуОповещения и т.п.) свёрнуты.
- MODULES: справочник модулей с построчной детализацией.

Правила:
- В EXECUTION FLOW отступ = глубина вызова. Имя модуля — при смене. [Total] — включительное время. [Self: X] — чистое время на листьях (только если оно значимо).
- ⤷ [колбэки ×N: ...] — свёрнутая цепочка асинхронных колбэков. Для анализа потока не существенна.
- В MODULES формат строки: :Line | Code  Total  Pure (мс).
- Контекст: [C]=Клиент, [S]=Сервер, [C→S]=вызов сервера, [?]=неизвестно.

Интерпретация:
- EXECUTION FLOW читай как историю: «сначала произошло X, X вызвал Y, Y вызвал Z...».
- Для верификации EXECUTION FLOW используй MODULES — там точные данные по строкам.
- Отделяй подтверждённые факты (строки/время из MODULES) от гипотез (связи в EXECUTION FLOW).

=== КОНЕЦ ПРОМПТА ===
"""

PERF_MODEL_PROMPT = """=== ПРОМПТ ДЛЯ МОДЕЛИ (PERF) ===
Ты анализируешь отчёт производительности 1С (PFF PERF v4): топ-проблемы с цепочками вызовов и горячие точки.

Структура отчёта:
- TOP ISSUES: ранжированные проблемы производительности, каждая с цепочкой вызовов от точки входа до узкого места и классификацией типа (HTTP, SQL, бизнес-логика, ресурсы).
- HOTSPOTS: топ по чистому (Self) времени с указанием полного (Total) времени.

Правила:
- «Влияние» в Issue — суммарное Self-время всех точек внутри Issue.
- Цепочка вызовов: отступ = уровень вложенности. ★ — узкое место (высокое Self-время).
- Self(мс) — время в самой строке (без вложенных). Total(мс) — время включая вложенные.
- Соотношение Self/Total: высокое Self при высоком Total — узкое место. Малое Self при большом Total — время уходит во вложенные.
- Цепочки реконструированы эвристически по Budget-matching, точность ~90%.
- Контекст: [C]=Клиент, [S]=Сервер, [C→S]=вызов сервера.

Рекомендации:
- Для HTTP-вызовов: оцени возможность кэширования, пакетирования, асинхронности.
- Для SQL-запросов: предложи индексы, упрощение запроса, кэширование.
- Для бизнес-логики: предложи рефакторинг, ленивую инициализацию, объединение серверных вызовов.
- Для ресурсов (макеты, картинки): предложи кэширование, предрасчёт.

=== КОНЕЦ ПРОМПТА ===
"""


TRACE_FORMAT_VERSION = "TRACE v7"
TRACE_FULL = "full"
TRACE_COMPACT = "compact"
TRACE_DETAIL_CHOICES = (TRACE_FULL, TRACE_COMPACT)

TRACE_DETAIL_PROFILES = {
    TRACE_FULL: {
        "flow_base_factor": 0.0001,
        "flow_target_rank": 500,
        "flow_floor_ms": 0.1,
        "modules_ratio": 0.10,
        "modules_floor_ms": 0.1,
        "control_multiplier": 0.20,
        "call_map_min_items": 80,
        "call_map_max_items": 360,
        "call_map_target_coverage": 0.98,
        "collapse_repeat_min_count": 999999,
        "collapse_callback_min_chain": 999999,
        "leaf_self_highlight_min_ms": 5.0,
        "leaf_tiny_total_ms": 0.005,
    },
    TRACE_COMPACT: {
        "flow_base_factor": 0.0009,
        "flow_target_rank": 90,
        "flow_floor_ms": 1.0,
        "modules_ratio": 0.75,
        "modules_floor_ms": 1.0,
        "control_multiplier": 1.60,
        "call_map_min_items": 30,
        "call_map_max_items": 120,
        "call_map_target_coverage": 0.82,
        "collapse_repeat_min_count": 3,
        "collapse_callback_min_chain": 2,
        "leaf_self_highlight_min_ms": 15.0,
        "leaf_tiny_total_ms": 0.020,
    },
}

TRACE_COVERAGE_REASON_KEYS = (
    "structural_noise",
    "control_leaf_noise",
    "terminal_trivial",
    "tiny_leaf",
    "collapsed_repeats",
    "collapsed_callbacks",
    "threshold_hidden",
)


def normalize_trace_detail(value, warn_if_normal=False):
    """Normalize trace_detail to one of full/compact. 'normal' is deprecated alias."""
    if value is None:
        return TRACE_COMPACT
    normalized = str(value).strip().lower()
    if normalized == "normal":
        if warn_if_normal:
            print(
                "Warning: trace_detail 'normal' is deprecated, using 'compact'.",
                file=sys.stderr,
            )
        return TRACE_COMPACT
    if normalized not in TRACE_DETAIL_CHOICES:
        raise ValueError(
            f"Unsupported trace detail '{value}'. Expected one of: full, compact."
        )
    return normalized


def trace_detail_label(trace_detail):
    return normalize_trace_detail(trace_detail).upper()


def _coverage_counters():
    return {key: 0 for key in TRACE_COVERAGE_REASON_KEYS}


def _repro_cli_command(file_path, mode, trace_detail, include_model_prompt=True):
    cmd = ["python", "src/pff_parser.py", file_path, "--mode", mode]
    if mode == "TRACE":
        cmd.extend(["--trace-detail", normalize_trace_detail(trace_detail)])
    if not include_model_prompt:
        cmd.append("--no-model-prompt")
    return " ".join(shlex.quote(str(part)) for part in cmd)


def build_trace_model_prompt(trace_detail):
    mode_label = trace_detail_label(trace_detail)
    lines = [
        f"=== ПРОМПТ ДЛЯ МОДЕЛИ (TRACE {mode_label}) ===",
        f"Ты анализируешь отчёт {TRACE_FORMAT_VERSION} в режиме {mode_label}.",
        "Соглашения формата: #ID = FACT, ?ID = INFERRED, M01:Line = ссылка на модуль.",
        "Сокращение MODULES MAP: 'ОМ.' = 'ОбщийМодуль.'.",
        "Расшифровку M01/M02/... бери из секции MODULES MAP.",
        "Контекст [C]/[S]/[C→S] берётся из данных замера, это не эвристика.",
        "Обязательно ссылайся на EventID и Mxx:Line для каждого важного вывода.",
        "В TRACE запрещено делать выводы о производительности по времени как KPI; для этого нужен PERF.",
    ]
    if normalize_trace_detail(trace_detail) == TRACE_FULL:
        lines.append("TRACE FULL используй для root-cause анализа, когда compact недостаточен.")
    else:
        lines.append("Если данных не хватает, эскалируй анализ до TRACE FULL.")
    lines.extend([
        "Используй TRACE COVERAGE, чтобы учитывать скрытые/свернутые элементы при выводах.",
        "=== КОНЕЦ ПРОМПТА ===",
    ])
    return "\n".join(lines)


def context_label(ctx):  # type: (int | None) -> str
    """Преобразовать числовой контекст в метку. Пустая строка — если None."""
    if ctx is None:
        return ""
    return CONTEXT_LABELS.get(ctx, '?')


def _context_from_three_fields(client, server, obrabotka):
    """
    Вычислить контекст выполнения по трём полям (Клиент, Сервер, ОбработкаСервером).
    Принимает 0/1 (или 0.0/1.0, "0"/"1"), возвращает 1 (C), 2 (S), 4 (C→S) или None для остальных комбинаций.
    """
    try:
        c = 1 if int(client) else 0
        s = 1 if int(server) else 0
        o = 1 if int(obrabotka) else 0
    except (TypeError, ValueError):
        return None
    if (c, s, o) == (1, 0, 0):
        return 1   # C
    if (c, s, o) == (0, 1, 0):
        return 2   # S
    if (c, s, o) == (1, 0, 1):
        return 4   # C→S
    return None


def _context_from_two_fields(client, server):
    """
    Вычислить контекст выполнения по двум полям (Клиент, Сервер) для плоского формата 2c.
    Принимает 0/1 (или 0.0/1.0, "0"/"1"), возвращает 1 (C), 2 (S), 4 (C→S) или None.
    """
    try:
        c = 1 if int(client) else 0
        s = 1 if int(server) else 0
    except (TypeError, ValueError):
        return None
    if (c, s) == (1, 0):
        return 1   # C
    if (c, s) == (0, 1):
        return 2   # S
    if (c, s) == (1, 1):
        return 4   # C→S
    return None


def _parse_sig_extension(sig_raw):  # type: (str) -> str
    """Извлечь последнюю строку в кавычках из блока sig (имя расширения)."""
    if not sig_raw or not isinstance(sig_raw, str):
        return ""
    last_q = sig_raw.rfind('"')
    if last_q < 0:
        return ""
    start = sig_raw.rfind(',"')
    if start < 0:
        return ""
    ext = sig_raw[start + 2:last_q].replace('""', '"').strip()
    return ext if ext else ""


class PFFStreamParser:
    """
    Потоковый парсер формата PFF.
    Читает файл посимвольно, учитывая состояние (в кавычках, в блоке).
    Не использует RegExp. Не загружает весь файл в память (итератор).
    Возвращает события с полем block_id (индекс блока трассировки 0, 1, 2...).
    """
    def __init__(self, content):
        self.content = content
        self.length = len(content)
        self.pos = 0
        self.session_info = {}

    def parse_events(self):
        """
        Генератор, возвращающий структурированные события (dict) с полем block_id.
        Пропускает заголовок и метаданные, извлекает только записи трассировки (Type=0).
        """
        # 1. Вход в корневой объект
        if not self._skip_until('{'): return
        self.pos += 1  # Skip root '{'

        block_id = 0

        # 2. Чтение потока элементов внутри корня
        while self.pos < self.length:
            self._skip_whitespace_and_comma()

            if self.pos >= self.length: break
            if self.content[self.pos] == '}': break  # Конец файла

            if self.content[self.pos] != '{':
                self._read_primitive()
                continue

            record_fields = self._read_record_fields()

            if len(record_fields) > 0:
                # Заголовок сеанса (Type=10)
                if record_fields[0] == 10 and len(record_fields) > 5:
                    self.session_info[block_id] = {
                        "host": record_fields[1],
                        "session_id": record_fields[3],
                        "app_type": record_fields[5],
                    }
                    continue

                # Формат 2a: единственное поле — блок {0, ticks, GUID, {внутренние записи}}
                if len(record_fields) == 1 and isinstance(record_fields[0], str):
                    blk = record_fields[0].strip()
                    if blk.startswith('{0,'):
                        for evt in self._parse_wrapped_block_events(blk):
                            evt['block_id'] = block_id
                            yield evt
                        block_id += 1
                        continue

                # Формат 2b (включая mixed): запись-контейнер с вложенными блоками после служебного заголовка.
                # Приоритет структуры: если есть вложенные {...}, это 2b даже при большом количестве полей.
                if record_fields[0] in (0, None) and len(record_fields) >= 4:
                    has_blocks = False
                    parsed_events = 0
                    for i in range(3, len(record_fields)):
                        blk = record_fields[i]
                        if isinstance(blk, str) and blk.strip().startswith('{'):
                            has_blocks = True
                            for evt in self._parse_block_events(blk):
                                parsed_events += 1
                                evt['block_id'] = block_id
                                yield evt
                    # Если действительно извлекли события, считаем запись блоком 2b.
                    # Иначе даём шанс ветке 2c (в mixed-файлах встречаются неблочные поля, похожие на {...}).
                    if has_blocks and parsed_events > 0:
                        block_id += 1
                        continue

                # Формат 2c: плоский список 13 полей на запись
                if record_fields[0] == 0 and len(record_fields) >= 16:
                    if (isinstance(record_fields[4], str) and '.' in record_fields[4] and
                            isinstance(record_fields[5], (int, float))):
                        for base in range(3, len(record_fields) - 12, 13):
                            try:
                                evt = self._fields_to_event(
                                    record_fields, base + 1, base + 2, base + 3,
                                    base + 5, base + 6, sig_idx=base
                                )
                                # В 2c (flattened 2b) контекст хранится тремя флагами (Клиент/Сервер/Обработка).
                                if base + 11 < len(record_fields):
                                    evt["Context"] = _context_from_three_fields(
                                        record_fields[base + 9], record_fields[base + 10], record_fields[base + 11]
                                    )
                                evt['block_id'] = block_id
                                yield evt
                            except (TypeError, ValueError, IndexError):
                                pass
                        block_id += 1
                        continue

                # Формат 1: плоская запись {0, ..., Module, Line, Code, ..., Total, Pure, Level}
                if len(record_fields) > 12 and record_fields[0] == 0:
                    evt = self._fields_to_event(record_fields, 4, 5, 6, 10, 11, 12, ctx_idx=7)
                    evt['block_id'] = block_id
                    yield evt
                    block_id += 1

    def _fields_to_event(self, fields, mod_idx, line_idx, code_idx, total_idx, pure_idx, level_idx=None,
                         sig_idx=None, ctx_idx=None, client_idx=None, server_idx=None, obrabotka_idx=None,
                         percent_idx=None, count_idx=None, ext_guid_idx=None, ext_type_idx=None, ext_name_idx=None):
        """Преобразует поля записи в событие. При level_idx=None подставляется Level=0 (формат 2b).
        Контекст: при заданных client_idx, server_idx, obrabotka_idx — по трём полям; иначе ctx_idx (форматы 1, 2c).
        При percent_idx (формат 2b): общее время блока выводим как time_sec * 100 / percent (_block_total_sec)."""
        time_sec = float(fields[total_idx])
        evt = {
            "Module": fields[mod_idx],
            "Line": fields[line_idx],
            "Code": fields[code_idx],
            "Total": time_sec * 1000,  # sec -> ms
            "Pure": float(fields[pure_idx]) * 1000,
            "Level": int(fields[level_idx]) if level_idx is not None else 0
        }
        
        if count_idx is not None and count_idx < len(fields):
            try:
                evt["Count"] = int(fields[count_idx])
            except (ValueError, TypeError):
                evt["Count"] = 1
        else:
            evt["Count"] = 1

        if percent_idx is not None and percent_idx < len(fields):
            try:
                pct = float(fields[percent_idx])
                if pct > 0:
                    evt["_block_total_sec"] = time_sec * 100.0 / pct
            except (TypeError, ValueError):
                pass
        if client_idx is not None and server_idx is not None and obrabotka_idx is not None:
            if client_idx < len(fields) and server_idx < len(fields) and obrabotka_idx < len(fields):
                evt["Context"] = _context_from_three_fields(
                    fields[client_idx], fields[server_idx], fields[obrabotka_idx])
            else:
                evt["Context"] = None
        elif ctx_idx is not None and ctx_idx < len(fields):
            try:
                evt["Context"] = int(fields[ctx_idx])
            except (TypeError, ValueError):
                evt["Context"] = None
        else:
            evt["Context"] = None
            
        # Extension fields
        if ext_name_idx is not None and ext_name_idx < len(fields):
            ext_name = fields[ext_name_idx]
            if isinstance(ext_name, str) and ext_name:
                evt["Extension"] = ext_name
        elif sig_idx is not None and sig_idx < len(fields):
            # Legacy parsing from raw signature string (if needed)
            sig_raw = fields[sig_idx]
            if isinstance(sig_raw, str):
                ext = _parse_sig_extension(sig_raw)
                if ext:
                    evt["Extension"] = ext
                    
        if ext_guid_idx is not None and ext_guid_idx < len(fields):
            evt["ExtGUID"] = fields[ext_guid_idx]
            
        if ext_type_idx is not None and ext_type_idx < len(fields):
             try:
                evt["ExtType"] = int(fields[ext_type_idx])
             except (ValueError, TypeError):
                pass

        return evt

    def _parse_wrapped_block_events(self, block_content):
        """Парсит блок вида {0, ticks, GUID, {внутренние записи}}."""
        sub = PFFStreamParser(block_content)
        if not sub._skip_until('{'): return
        sub.pos += 1
        sub._skip_whitespace_and_comma()
        for _ in range(3):
            sub._read_primitive()
            sub._skip_whitespace_and_comma()
        if sub.pos < sub.length and sub.content[sub.pos] == '{':
            inner = sub._read_block_raw()
            for evt in self._parse_block_events(inner):
                yield evt

    def _parse_block_events(self, block_content):
        """Парсит вложенный блок с записями (формат 2b). Раскладка по отчёту АнализЗамеров (Пример замера, BSL-шаблон):
        индексы 8=Модуль, 9=НомерСтроки, 10=Текст, 11=Количество, 12=ВремяЧистоеСВложенными(сек), 13=ВремяЧистое(сек),
        14-15=проценты не для времени, 16=Клиент, 17=Сервер, 18=ОбработкаСервером. Level в записи нет — подставляем 0.
        
        Indices based on flattened signature:
        0: {"",0}
        1: ModGUID
        2: ConfGUID
        3: ObjType
        4: ExtGUID
        5: ExtType
        6: Base64
        7: ExtName
        8: Module
        9: Line
        10: Code
        11: Count
        12: Total
        13: Pure
        14: Tot%
        15: Pure%
        16: Cli
        17: Srv
        18: SrvProc
        """
        sub = PFFStreamParser(block_content)
        if not sub._skip_until('{'): return
        sub.pos += 1

        while sub.pos < sub.length:
            sub._skip_whitespace_and_comma()
            if sub.pos >= sub.length or sub.content[sub.pos] == '}':
                break
            if sub.content[sub.pos] != '{':
                sub._read_primitive()
                continue
            fields = sub._read_record_fields()
            if len(fields) < 14:
                continue
            try:
                yield sub._fields_to_event(
                    fields, 8, 9, 10, 12, 13,
                    sig_idx=0, client_idx=16, server_idx=17, obrabotka_idx=18,
                    percent_idx=14,
                    count_idx=11,
                    ext_guid_idx=4,
                    ext_type_idx=5,
                    ext_name_idx=7
                )
            except (TypeError, ValueError, IndexError):
                pass

    def _read_record_fields(self):
        """Читает поля одной записи {...}. По отчёту АнализЗамеров первое поле может быть блоком {"",0} или двумя полями "", 0."""
        fields = []
        self.pos += 1
        while self.pos < self.length and self.content[self.pos].isspace():
            self.pos += 1

        while self.pos < self.length:
            char = self.content[self.pos]

            if char == '}':
                # По отчёту АнализЗамеров: запись может начинаться с "", 0 (Файл, Число); первый '}' закрывает этот блок — не конец записи.
                if len(fields) == 2 and fields[0] == "" and fields[1] == 0:
                    self.pos += 1
                    self._skip_whitespace_and_comma()
                    fields = ['{"",0}']  # объединяем в один логический блок, как в BSL-шаблоне
                    continue
                self.pos += 1
                return fields

            elif char == '"':
                fields.append(self._read_string())
                self._skip_whitespace_and_comma()

            elif char == '{':
                fields.append(self._read_block_raw())
                self._skip_whitespace_and_comma()

            elif char == ',':
                fields.append(None)
                self.pos += 1

            elif char.isspace():
                self.pos += 1

            else:
                fields.append(self._read_primitive())
                self._skip_whitespace_and_comma()
        return fields

    def _read_string(self):
        self.pos += 1
        res = []
        while self.pos < self.length:
            char = self.content[self.pos]
            if char == '"':
                if self.pos + 1 < self.length and self.content[self.pos + 1] == '"':
                    res.append('"')
                    self.pos += 2
                else:
                    self.pos += 1
                    return "".join(res)
            else:
                res.append(char)
                self.pos += 1
        return "".join(res)

    def _read_block_raw(self):
        start = self.pos
        self.pos += 1
        balance = 1
        while self.pos < self.length and balance > 0:
            char = self.content[self.pos]
            if char == '{':
                balance += 1
            elif char == '}':
                balance -= 1
            elif char == '"':
                self._skip_string_content()
                continue
            self.pos += 1
        return self.content[start:self.pos]

    def _skip_string_content(self):
        self.pos += 1
        while self.pos < self.length:
            char = self.content[self.pos]
            if char == '"':
                if self.pos + 1 < self.length and self.content[self.pos + 1] == '"':
                    self.pos += 2
                else:
                    self.pos += 1
                    return
            else:
                self.pos += 1

    def _read_primitive(self):
        start = self.pos
        while self.pos < self.length:
            char = self.content[self.pos]
            if char in (',', '}'):
                break
            self.pos += 1
        val = self.content[start:self.pos].strip()
        try:
            return float(val) if '.' in val else int(val)
        except ValueError:
            return val

    def _skip_whitespace_and_comma(self):
        while self.pos < self.length:
            char = self.content[self.pos]
            if char.isspace() or char == ',':
                self.pos += 1
            else:
                return

    def _skip_until(self, char):
        while self.pos < self.length:
            if self.content[self.pos] == char:
                return True
            self.pos += 1
        return False


# ==================================================================================================
# 2. SHARED HELPERS
# ==================================================================================================


def _strip_extension_prefix(module_name, extension):
    if extension and module_name.startswith(extension + " "):
        return module_name[len(extension) + 1:]
    return module_name


def _module_short_name(module_name):
    parts = module_name.split(".")
    if len(parts) >= 2 and parts[-1] == "Модуль":
        return parts[-2]
    return parts[-1] if parts else module_name


def _event_sort_key(event):
    return (
        int(event.get("block_id", 0) or 0),
        int(event.get("Level", 0) or 0),
        str(event.get("Module", "")),
        int(event.get("Line", 0) or 0),
        str(event.get("Code", "")),
        float(event.get("Total", 0) or 0),
        float(event.get("Pure", 0) or 0),
    )


def _event_num(event):
    raw = str(event.get("_event_id", "0"))
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return 0
    return int(digits)


def _compact_eid(event, default_type="fact"):
    mark = "#" if default_type == "fact" else "?"
    return f"{mark}{_event_num(event)}"


def _abbrev_common_module(module_name):
    if module_name.startswith("ОбщийМодуль."):
        return "ОМ." + module_name[len("ОбщийМодуль."):]
    return module_name


def _is_callback(event):
    """Определяет, является ли событие колбэком (для свёртки цепочек)."""
    code = (event.get("Code") or "").strip()
    ctx = event.get("Context")
    # Колбэки обычно C→S (контексты 3-6) или C (контекст 1)
    for pattern in CALLBACK_PATTERNS:
        if pattern in code:
            return True
    return False


def _classify_code(code):
    """Классифицирует код строки по типу: HTTP, SQL, Resource, Callback, Business."""
    code_str = (code or "").strip()
    for issue_type, patterns in ISSUE_TYPE_PATTERNS.items():
        for pattern in patterns:
            if pattern in code_str:
                return issue_type
    for pattern in CALLBACK_PATTERNS:
        if pattern in code_str:
            return "Callback"
    return "Business"


# Расширенный regex для извлечения имени вызываемого модуля из кода
_CALL_REGEX_EXTENDED = re.compile(
    r"(?:"
    r"([\w]+)\s*\.\s*([\w]+)\s*\("    # Модуль.Метод(
    r"|"
    r"РегистрыСведений\s*\.\s*([\w]+)"  # РегистрыСведений.ИмяРегистра
    r"|"
    r"Справочники\s*\.\s*([\w]+)"       # Справочники.ИмяСправочника
    r")"
)


def _extract_called_module(code):
    """Извлекает имя вызываемого модуля из кода строки (расширенная версия)."""
    if not code:
        return None
    for match in _CALL_REGEX_EXTENDED.finditer(code):
        if match.group(1):
            return match.group(1)
        if match.group(3):
            return match.group(3)
        if match.group(4):
            return match.group(4)
    return None


def _is_control_flow_line(code):
    """Проверяет, является ли строка управляющей конструкцией (без привязки к модулю)."""
    code_str = (code or "").strip()
    if not code_str:
        return False
    for prefix in CONTROL_FLOW_PREFIXES:
        if (code_str == prefix or code_str.startswith(prefix + " ")
                or code_str.startswith(prefix + "(")):
            return True
    return False


def _is_leaf_like_event(event, tolerance=0.15):
    """Эвристика: событие почти не делегирует время во вложенные вызовы."""
    total = float(event.get("Total", 0) or 0)
    pure = float(event.get("Pure", 0) or 0)
    if total <= 0:
        return True
    return abs(total - pure) <= max(0.001, total * tolerance)


class ProcedureGrouper:
    """Группирует события в блоки [Ctx] Func/Proc (:X-Y)."""

    def __init__(self, events):
        self.events = events

    @staticmethod
    def _block_type_from_code(code):
        code_norm = (code or "").strip()
        if code_norm == "КонецФункции":
            return "Func"
        if code_norm == "КонецПроцедуры":
            return "Proc"
        return "Block"

    @staticmethod
    def _is_loop_jump(prev_event, curr_event):
        if not prev_event:
            return False
        prev_code = (prev_event.get("Code") or "")
        return "КонецЦикла" in prev_code

    def _finalize_block(self, events, force_type=None):
        if not events:
            return None
        lines = [e["Line"] for e in events]
        btype = force_type or self._block_type_from_code(events[-1].get("Code"))
        return {
            "type": btype,
            "line_start": min(lines),
            "line_end": max(lines),
            "total": sum(e["Total"] for e in events),
            "pure": sum(e["Pure"] for e in events),
            "events": events,
        }

    def _split_group_to_blocks(self, group_events):
        if not group_events:
            return []
        blocks = []
        current = []
        prev = None
        for e in group_events:
            if current and e["Line"] < prev["Line"] and not self._is_loop_jump(prev, e):
                block = self._finalize_block(current)
                if block:
                    blocks.append(block)
                current = []
            current.append(e)
            if self._block_type_from_code(e.get("Code")) in ("Func", "Proc"):
                block = self._finalize_block(current)
                if block:
                    blocks.append(block)
                current = []
            prev = e
        if current:
            block = self._finalize_block(current, force_type="Block")
            if block:
                blocks.append(block)
        return blocks

    def group(self):
        groups = []
        index = {}
        sorted_events = sorted(self.events, key=_event_sort_key)
        for e in sorted_events:
            key = (e["Module"], e.get("Context"), e.get("Extension"), e.get("block_id", 0))
            if key not in index:
                index[key] = len(groups)
                groups.append({
                    "module": e["Module"],
                    "context": e.get("Context"),
                    "extension": e.get("Extension"),
                    "block_id": e.get("block_id", 0),
                    "events": [],
                })
            groups[index[key]]["events"].append(e)
        groups.sort(
            key=lambda g: (
                int(g.get("block_id", 0) or 0),
                str(g.get("module", "")),
                str(g.get("extension", "")),
                int(g.get("context", -1) if g.get("context") is not None else -1),
            )
        )
        for g in groups:
            g["blocks"] = self._split_group_to_blocks(g["events"])
            g["blocks"].sort(key=lambda b: (int(b["line_start"]), int(b["line_end"]), str(b["type"])))
        return groups


class CallMapBuilder:
    """Строит CALL MAP по бюджету вложенных вызовов (Total - Pure)."""

    def __init__(self, events, grouped_modules, threshold_ms=None):
        self.events = events
        self.grouped_modules = grouped_modules
        self.threshold_ms = threshold_ms

    def _calc_threshold(self):
        total_ms = sum(e["Total"] for e in self.events)
        if self.threshold_ms is not None:
            return self.threshold_ms
        return max(0.5, total_ms * 0.01)

    def _extract_target_module_short(self, code):
        return _extract_called_module(code)

    def _find_best_block(self, caller_event, budget_ms):
        target_short = self._extract_target_module_short(caller_event.get("Code"))
        if not target_short:
            return None
        candidates = []
        for g in self.grouped_modules:
            mod_wo_ext = _strip_extension_prefix(g["module"], g.get("extension"))
            if _module_short_name(mod_wo_ext) != target_short:
                continue
            for b in g.get("blocks", []):
                delta = abs(b["total"] - budget_ms)
                tolerance = max(0.5, budget_ms * 0.2)
                if delta <= tolerance:
                    candidates.append((delta, g, b))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]

    def build(self):
        threshold = self._calc_threshold()
        result = []
        for e in self.events:
            budget = e["Total"] - e["Pure"]
            if budget <= threshold:
                continue
            item = {
                "event": e,
                "budget": budget,
                "reference": None,
            }
            best = self._find_best_block(e, budget)
            if best:
                group, block = best
                item["reference"] = {"group": group, "block": block}
                item["reference_inferred"] = True
            result.append(item)
        result.sort(
            key=lambda x: (
                -x["budget"],
                int(x["event"].get("block_id", 0) or 0),
                str(x["event"].get("Module", "")),
                int(x["event"].get("Line", 0) or 0),
            )
        )
        return result


# ==================================================================================================
# 3. EXECUTION FLOW BUILDER (v4)
# ==================================================================================================

class ExecutionFlowBuilder:
    """Build a heuristic execution tree for TRACE."""

    def __init__(self, events, grouped_modules, threshold_ms, detail_profile=None, location_formatter=None):
        self.events = events
        self.grouped_modules = grouped_modules
        self.threshold_ms = threshold_ms
        self.detail_profile = detail_profile or TRACE_DETAIL_PROFILES[TRACE_COMPACT]
        self.location_formatter = location_formatter
        self.block_index = self._build_block_index()
        self.used_blocks = set()
        self.shown_event_ids = set()
        self.hidden_reasons = _coverage_counters()

    def _build_block_index(self):
        """Index blocks by (module_short_name, context)."""
        index = defaultdict(list)
        for g in self.grouped_modules:
            mod_wo_ext = _strip_extension_prefix(g["module"], g.get("extension"))
            short = _module_short_name(mod_wo_ext)
            ctx = g.get("context")
            for b in g.get("blocks", []):
                index[(short, ctx)].append({"group": g, "block": b})
            for b in g.get("blocks", []):
                index[(short, None)].append({"group": g, "block": b})

        for key in list(index.keys()):
            index[key].sort(
                key=lambda x: (
                    -float(x["block"].get("total", 0) or 0),
                    int(x["group"].get("block_id", 0) or 0),
                    str(x["group"].get("module", "")),
                    int(x["block"].get("line_start", 0) or 0),
                )
            )
        return index

    def _find_child_block(self, caller_event, budget_ms):
        """Find child block by target module + budget matching."""
        target_short = _extract_called_module(caller_event.get("Code"))
        if not target_short:
            return None

        candidates = []
        for ctx_key in [caller_event.get("Context"), None]:
            for entry in self.block_index.get((target_short, ctx_key), []):
                b = entry["block"]
                if id(b) in self.used_blocks:
                    continue
                delta = abs(b["total"] - budget_ms)
                tolerance = max(0.5, budget_ms * 0.2)
                if delta <= tolerance:
                    candidates.append((delta, entry))

        if not candidates:
            return None
        candidates.sort(
            key=lambda x: (
                x[0],
                int(x[1]["group"].get("block_id", 0) or 0),
                str(x[1]["group"].get("module", "")),
                int(x[1]["block"].get("line_start", 0) or 0),
            )
        )
        best = candidates[0][1]
        self.used_blocks.add(id(best["block"]))
        return best

    def _build_subtree(self, block, group, depth=0, max_depth=15):
        if depth > max_depth:
            return []

        ctx = group.get("context")
        mod_wo_ext = _strip_extension_prefix(group["module"], group.get("extension"))
        mod_short = _module_short_name(mod_wo_ext)

        nodes = []
        block_events = sorted(block.get("events", []), key=_event_sort_key)
        for e in block_events:
            budget = e["Total"] - e["Pure"]
            is_cb = _is_callback(e)
            code = e.get("Code") or ""
            is_control = _is_control_flow_line(code)
            has_called_module = _extract_called_module(code) is not None

            if (budget <= self.threshold_ms and e["Pure"] <= self.threshold_ms
                    and e["Total"] <= self.threshold_ms and not is_cb):
                self.hidden_reasons["threshold_hidden"] += 1
                continue

            control_threshold = max(
                self.detail_profile.get("flow_floor_ms", 0.5),
                self.threshold_ms * self.detail_profile.get("control_multiplier", 1.0),
            )
            if (is_control and not has_called_module and not is_cb
                    and _is_leaf_like_event(e) and e["Total"] <= control_threshold):
                self.hidden_reasons["control_leaf_noise"] += 1
                continue

            tiny_leaf_limit = self.detail_profile.get("leaf_tiny_total_ms", 0.01)
            if e["Total"] <= tiny_leaf_limit and _is_leaf_like_event(e):
                self.hidden_reasons["tiny_leaf"] += 1
                continue

            node = {
                "event": e,
                "module_short": mod_short,
                "module_full": mod_wo_ext,
                "context": ctx,
                "extension": group.get("extension"),
                "children": [],
                "is_callback": is_cb,
            }

            if budget > self.threshold_ms:
                child_entry = self._find_child_block(e, budget)
                if child_entry:
                    child_nodes = self._build_subtree(
                        child_entry["block"], child_entry["group"], depth=depth + 1, max_depth=max_depth
                    )
                    node["children"] = child_nodes

            nodes.append(node)

        return nodes

    def build(self):
        all_blocks = []
        for g in self.grouped_modules:
            for b in g.get("blocks", []):
                if b["total"] >= self.threshold_ms:
                    all_blocks.append({"group": g, "block": b, "total": b["total"]})
        all_blocks.sort(
            key=lambda x: (
                -float(x["total"]),
                int(x["group"].get("block_id", 0) or 0),
                str(x["group"].get("module", "")),
                int(x["block"].get("line_start", 0) or 0),
            )
        )

        forest = []
        for entry in all_blocks:
            b = entry["block"]
            if id(b) in self.used_blocks:
                continue
            self.used_blocks.add(id(b))
            subtree = self._build_subtree(b, entry["group"])
            if subtree:
                forest.append({"group": entry["group"], "block": b, "nodes": subtree})

        lines = []
        lines.append("=== EXECUTION FLOW ===")
        lines.append("Связи по умолчанию INFERRED (исключения помечены #).")
        lines.append("")

        for tree in forest:
            self._format_tree_nodes(tree["nodes"], lines, indent=0)
            lines.append("")

        return {
            "text": "\n".join(lines),
            "shown_event_ids": set(self.shown_event_ids),
            "hidden_reasons": dict(self.hidden_reasons),
        }

    def _format_tree_nodes(self, nodes, lines, indent):
        i = 0
        while i < len(nodes):
            node = nodes[i]

            if node["is_callback"]:
                cb_chain = [node]
                j = i + 1
                while j < len(nodes) and nodes[j]["is_callback"]:
                    cb_chain.append(nodes[j])
                    j += 1
                collapse_min_chain = self.detail_profile.get("collapse_callback_min_chain", 3)
                if len(cb_chain) >= collapse_min_chain:
                    first_event = cb_chain[0]["event"]
                    first_id = _compact_eid(first_event, default_type="inferred")
                    self.shown_event_ids.add(first_event.get("_event_id", "E00000"))
                    self.hidden_reasons["collapsed_callbacks"] += max(0, len(cb_chain) - 1)

                    parts = []
                    for cb in cb_chain:
                        e = cb["event"]
                        if self.location_formatter:
                            parts.append(self.location_formatter(e["Module"], e["Line"], e.get("Extension")))
                        else:
                            parts.append(f"{cb['module_short']}:{e['Line']}")

                    prefix = "  " * indent
                    lines.append(
                        f"{prefix}{first_id} [callbacks x{len(cb_chain)}: {' -> '.join(parts)}]"
                    )
                    i = j
                    continue

            e = node["event"]
            self.shown_event_ids.add(e.get("_event_id", "E00000"))
            ctx_label_str = context_label(node["context"]) or "?"
            prefix = "  " * indent

            if self.location_formatter:
                location = self.location_formatter(e["Module"], e["Line"], e.get("Extension"))
            else:
                location = f"{node['module_short']}:{e['Line']}"

            code_snip = (e["Code"] or "").strip().replace("\n", " ")
            inferred_id = _compact_eid(e, default_type="inferred")
            lines.append(
                f"{prefix}{inferred_id} [{ctx_label_str}] {location} | {code_snip}"
            )
            if node["children"]:
                self._format_tree_nodes(node["children"], lines, indent + 1)

            i += 1


# ==================================================================================================
# 3b. ISSUE ANALYZER (PERF v4)
# ==================================================================================================


class IssueAnalyzer:
    """Анализирует hotspots и строит TOP ISSUES для PERF v4."""

    def __init__(self, events, grouped_modules, threshold_ms):
        self.events = events
        self.grouped_modules = grouped_modules
        self.threshold_ms = threshold_ms
        self.call_map_builder = CallMapBuilder(events, grouped_modules, threshold_ms)

    def _get_hotspots(self, top_n=30):
        """Получить топ hotspots по Pure (Self) time."""
        agg = {}
        for e in self.events:
            ctx = e.get("Context")
            ext = e.get("Extension")
            key = (e["Module"], e["Line"], ctx, ext)
            if key not in agg:
                agg[key] = {
                    "pure": 0, "total": 0, "count": 0,
                    "code": e["Code"], "mod": e["Module"],
                    "ctx": ctx, "ext": ext, "line": e["Line"],
                    "events": [],
                }
            agg[key]["pure"] += e["Pure"]
            agg[key]["total"] += e["Total"]
            agg[key]["count"] += e.get("Count", 1)
            agg[key]["events"].append(e)

        sorted_agg = sorted(agg.values(), key=lambda x: x["pure"], reverse=True)
        return sorted_agg[:top_n]

    def _classify_issue(self, hotspot_events):
        """Классифицировать тип Issue по коду hotspot-ов."""
        type_counts = defaultdict(float)
        for hs in hotspot_events:
            code = (hs.get("code") or "")
            t = _classify_code(code)
            type_counts[t] += hs["pure"]
        if not type_counts:
            return "Бизнес-логика"
        return max(type_counts, key=type_counts.get)

    def _cluster_hotspots(self, hotspots):
        """Группировка hotspots в Issue по модулю-источнику."""
        clusters = defaultdict(list)
        for hs in hotspots:
            mod_name = _strip_extension_prefix(hs["mod"], hs.get("ext"))
            mod_short = _module_short_name(mod_name)
            clusters[mod_short].append(hs)

        issues = []
        for mod_short, hs_list in clusters.items():
            # Суммарное влияние
            total_impact = sum(h["pure"] for h in hs_list)
            if total_impact < self.threshold_ms:
                continue
            issues.append({
                "module_short": mod_short,
                "hotspots": hs_list,
                "impact": total_impact,
                "count": sum(h["count"] for h in hs_list),
            })

        issues.sort(key=lambda x: x["impact"], reverse=True)
        return issues

    def _build_call_chain(self, hotspot_event):
        """Построить восходящую цепочку вызовов от hotspot до корня."""
        chain = []
        current_event = hotspot_event
        visited = set()
        max_depth = 10

        for _ in range(max_depth):
            if id(current_event) in visited:
                break
            visited.add(id(current_event))

            # Найти, кто вызвал текущий блок
            caller = self._find_caller(current_event)
            if caller is None:
                break
            chain.append(caller)
            current_event = caller["event"]

        chain.reverse()
        return chain

    def _find_caller(self, target_event):
        """Найти вызывающую строку для данного события (по Budget-matching)."""
        target_total = target_event.get("Total", 0)
        if target_total < self.threshold_ms:
            return None

        target_mod = _strip_extension_prefix(
            target_event["Module"], target_event.get("Extension"))
        target_short = _module_short_name(target_mod)

        best = None
        best_delta = float("inf")

        for e in self.events:
            if e is target_event:
                continue
            budget = e["Total"] - e["Pure"]
            if budget < self.threshold_ms:
                continue
            # Извлечь имя вызываемого модуля из кода
            called = _extract_called_module(e.get("Code"))
            if called and called == target_short:
                delta = abs(budget - target_total)
                tolerance = max(0.5, target_total * 0.3)
                if delta <= tolerance and delta < best_delta:
                    best_delta = delta
                    best = e

        if best is None:
            return None

        return {
            "event": best,
            "module_short": _module_short_name(
                _strip_extension_prefix(best["Module"], best.get("Extension"))),
            "context": best.get("Context"),
        }

    def build(self):
        """Построить TOP ISSUES и вернуть форматированные строки."""
        hotspots = self._get_hotspots(top_n=30)
        if not hotspots:
            return ["=== TOP ISSUES ===", "Нет значимых проблем."]

        issues = self._cluster_hotspots(hotspots)
        total_time = sum(e["Total"] for e in self.events if e["Level"] == 0)
        if total_time == 0:
            total_time = sum(e["Total"] for e in self.events)

        lines = []
        lines.append("=== TOP ISSUES (проблемы по убыванию влияния) ===")
        lines.append("")

        for idx, issue in enumerate(issues[:10], 1):
            pct = (issue["impact"] / total_time * 100) if total_time > 0 else 0
            issue_type = self._classify_issue(issue["hotspots"])

            # Описание Issue
            top_hs = issue["hotspots"][0]
            top_code = (top_hs["code"] or "").strip().replace("\n", " ")[:60]
            description = f"{issue['module_short']}: {top_code}"

            lines.append(f"--- ISSUE #{idx}: {description} ---")
            lines.append(f"Влияние: {issue['impact']:.0f} мс ({pct:.1f}% от общего), {issue['count']} вызовов")
            lines.append(f"Тип: {issue_type}")

            # Контекст
            contexts = set()
            for hs in issue["hotspots"]:
                ctx = context_label(hs.get("ctx"))
                if ctx:
                    contexts.add(ctx)
            ctx_str = ", ".join(sorted(contexts)) if contexts else "?"
            ext_names = set()
            for hs in issue["hotspots"]:
                if hs.get("ext"):
                    ext_names.add(hs["ext"])
            ext_str = f" Расширение {', '.join(sorted(ext_names))}" if ext_names else ""
            lines.append(f"Контекст: [{ctx_str}]{ext_str}")
            lines.append("")

            # Цепочка вызовов (для основного hotspot)
            main_event = top_hs["events"][0] if top_hs["events"] else None
            if main_event:
                chain = self._build_call_chain(main_event)
                if chain:
                    lines.append("  Цепочка вызовов:")
                    for depth, link in enumerate(chain):
                        evt = link["event"]
                        link_ctx = context_label(link.get("context")) or "?"
                        link_mod = link["module_short"]
                        link_code = (evt["Code"] or "").strip().replace("\n", " ")[:60]
                        indent = "    " + "  " * depth
                        lines.append(
                            f"{indent}[{link_ctx}] {link_mod}:{evt['Line']} | {link_code}  [{evt['Total']:.0f}ms]"
                        )
                    # Добавляем сам hotspot как лист
                    leaf_ctx = context_label(main_event.get("Context")) or "?"
                    leaf_mod = _module_short_name(
                        _strip_extension_prefix(main_event["Module"], main_event.get("Extension")))
                    leaf_code = (main_event["Code"] or "").strip().replace("\n", " ")[:60]
                    leaf_indent = "    " + "  " * len(chain)
                    lines.append(
                        f"{leaf_indent}[{leaf_ctx}] {leaf_mod}:{main_event['Line']} | "
                        f"★ Self: {main_event['Pure']:.0f}ms | {leaf_code}"
                    )
                    lines.append("")

            # Ключевые Self-точки
            lines.append("  Ключевые Self-точки:")
            for hs in issue["hotspots"][:5]:
                hs_mod = _module_short_name(
                    _strip_extension_prefix(hs["mod"], hs.get("ext")))
                hs_code = (hs["code"] or "").strip().replace("\n", " ")[:60]
                lines.append(f"    {hs_mod}:{hs['line']} | Self: {hs['pure']:.0f}ms | {hs_code}")
            lines.append("")

        return "\n".join(lines)


# ==================================================================================================
# 4. REPORT GENERATOR (TRACE & PERF)
# ==================================================================================================

# Ширина колонки контекста (для выравнивания при "только при смене")
CONTEXT_COL_WIDTH = 5  # "[C→S]" = 5 символов



class ReportGenerator:
    def __init__(self, events, session_info=None, threshold_ms=None, all_events=None,
                 trace_detail=TRACE_COMPACT, show_context=True, expand_module_names=True,
                 source_file_path=None, trace_request_meta=None, include_model_prompt=True):
        self.events = events
        self.session_info = session_info or {}
        self.all_events = all_events if all_events is not None else events
        self.modules_map = {}
        self.modules_list = []
        self.trace_module_aliases = {}
        self.trace_module_order = []
        self.trace_module_titles = {}
        self.trace_extension_aliases = {}
        self.trace_extension_order = []
        self.threshold_ms = threshold_ms
        self.trace_detail = normalize_trace_detail(trace_detail)
        self.trace_profile = TRACE_DETAIL_PROFILES[self.trace_detail]
        self.show_context = show_context
        self.expand_module_names = expand_module_names
        self.source_file_path = source_file_path
        self.trace_request_meta = trace_request_meta or {}
        self.include_model_prompt = include_model_prompt
        self.trace_coverage = {}
        self.trace_internal_metrics = {}
        self._assign_event_ids()

        # v4: определяем, есть ли реальные уровни вложенности (Level > 0)
        self.has_level = any(e["Level"] > 0 for e in events) if events else False

        if events:
            min_level = min(e['Level'] for e in events)
            for e in events:
                e['Level'] -= min_level

    def _assign_event_ids(self):
        ordered = list(self.all_events) if self.all_events is not None else list(self.events)
        for idx, event in enumerate(ordered, 1):
            event["_event_id"] = f"E{idx:05d}"

    def get_module_id(self, full_name):
        """При expand_module_names — полное имя; иначе M1, M2 и секция МОДУЛИ."""
        if self.expand_module_names:
            return full_name
        if full_name not in self.modules_map:
            mid = f"M{len(self.modules_map) + 1}"
            self.modules_map[full_name] = mid
            self.modules_list.append(f"{mid} = {full_name}")
        return self.modules_map[full_name]

    def _session_labels(self):
        labels = []
        seen = set()
        for bid in sorted(self.session_info.keys()):
            app_type = self.session_info.get(bid, {}).get("app_type")
            label = SESSION_TYPES.get(app_type, f"Тип {app_type}") if app_type else None
            if label and label not in seen:
                labels.append(label)
                seen.add(label)
        return labels

    def generate_summary(self):
        """Краткая сводка без блоков/точки входа/хостнеймов."""
        by_block_sec = {}
        for e in self.events:
            if "_block_total_sec" in e:
                bid = e.get("block_id", 0)
                if bid not in by_block_sec:
                    by_block_sec[bid] = e["_block_total_sec"]
        if by_block_sec:
            total_time = sum(by_block_sec.values()) * 1000
        else:
            total_time = sum(e["Total"] for e in self.events if e["Level"] == 0)
        sessions = ", ".join(self._session_labels()) or "н/д"
        lines = [
            "=== СВОДКА ===",
            f"События: {len(self.events)} | Сессии: {sessions} | Время: {total_time/1000:.2f}с",
        ]
        return "\n".join(lines)

    @staticmethod
    def _trace_module_key(module, extension):
        mod_wo_ext = _strip_extension_prefix(module, extension)
        mod_abbrev = _abbrev_common_module(mod_wo_ext)
        return (extension or "", mod_abbrev)

    def _extension_alias(self, extension):
        if not extension:
            return None
        if extension not in self.trace_extension_aliases:
            alias = f"X{len(self.trace_extension_aliases) + 1}"
            self.trace_extension_aliases[extension] = alias
            self.trace_extension_order.append(extension)
        return self.trace_extension_aliases[extension]

    def _module_alias(self, module, extension):
        key = self._trace_module_key(module, extension)
        if key not in self.trace_module_aliases:
            alias = f"M{len(self.trace_module_aliases) + 1:02d}"
            self.trace_module_aliases[key] = alias
            self.trace_module_order.append(key)
            ext_alias = self._extension_alias(extension)
            display_mod = key[1]
            if ext_alias:
                self.trace_module_titles[key] = f"[{ext_alias}] {display_mod}"
            else:
                self.trace_module_titles[key] = display_mod
        return self.trace_module_aliases[key]

    def _trace_location(self, module, line, extension):
        return f"{self._module_alias(module, extension)}:{line}"

    def generate_trace(self):
        """TRACE v7: META + COVERAGE + MODULES MAP + FLOW + CALL INDEX + MODULES + REPRODUCE."""
        lines = []
        grouped = ProcedureGrouper(self.events).group()
        trace_flow_threshold = self._calc_trace_flow_threshold()
        trace_modules_threshold = self._calc_trace_modules_threshold(trace_flow_threshold)
        call_map_all = CallMapBuilder(self.events, grouped, threshold_ms=trace_flow_threshold).build()
        call_map, call_map_omitted = self._limit_call_map(call_map_all)

        mode_label = trace_detail_label(self.trace_detail)
        lines.append(f"=== TRACE [{mode_label}] ===")
        lines.append("")

        ef_builder = ExecutionFlowBuilder(
            self.events,
            grouped,
            trace_flow_threshold,
            detail_profile=self.trace_profile,
            location_formatter=self._trace_location,
        )
        ef_result = ef_builder.build()
        flow_shown_ids = set(ef_result.get("shown_event_ids", set()))
        flow_modules = self._collect_flow_modules(flow_shown_ids)

        modules_result = self._build_modules_section(
            grouped,
            trace_modules_threshold,
            flow_modules=flow_modules if self.trace_detail == TRACE_COMPACT else None,
        )
        call_index_lines = self._build_call_index(call_map)

        coverage_reasons = self._merge_coverage(
            ef_result.get("hidden_reasons", {}),
            modules_result.get("hidden_reasons", {}),
        )
        coverage_reasons["threshold_hidden"] += call_map_omitted
        events_total = len(self.events)
        modules_shown_ids = set(modules_result.get("shown_event_ids", set()))
        self.trace_coverage = {
            "events_total": events_total,
            "events_shown": len(flow_shown_ids),
            "modules_total_events": events_total,
            "modules_shown_events": len(modules_shown_ids),
            "reasons": coverage_reasons,
        }
        self.trace_internal_metrics = {
            "flow_threshold_ms": trace_flow_threshold,
            "modules_threshold_ms": trace_modules_threshold,
        }

        lines.extend(self._build_trace_meta_lines())
        lines.append("")
        lines.extend(self._build_trace_coverage_lines())
        lines.append("")
        lines.extend(self._build_modules_map_lines())
        lines.append("")
        lines.append((ef_result.get("text", "") or "").rstrip())
        lines.append("")
        lines.extend(call_index_lines)
        lines.append("")
        lines.append((modules_result.get("text", "") or "").rstrip())
        lines.append("")
        lines.extend(self._build_trace_reproduce_lines())
        return "\n".join(lines)

    @staticmethod
    def _merge_coverage(*counters):
        merged = _coverage_counters()
        for counter in counters:
            for key in TRACE_COVERAGE_REASON_KEYS:
                merged[key] += int(counter.get(key, 0))
        return merged

    def _collect_flow_modules(self, shown_event_ids):
        flow_modules = set()
        shown = set(shown_event_ids)
        for e in self.events:
            if e.get("_event_id") in shown:
                flow_modules.add(self._trace_module_key(e["Module"], e.get("Extension")))
        return flow_modules

    def _build_modules_map_lines(self):
        lines = ["=== MODULES MAP ==="]
        for key in self.trace_module_order:
            lines.append(f"{self.trace_module_aliases[key]}: {self.trace_module_titles[key]}")
        for ext in self.trace_extension_order:
            lines.append(f"{self.trace_extension_aliases[ext]}: {ext}")
        if len(lines) == 1:
            lines.append("(empty)")
        return lines

    def _build_trace_meta_lines(self):
        sessions = ", ".join(self._session_labels()) or "н/д"
        return [
            "=== TRACE META ===",
            f"format: {TRACE_FORMAT_VERSION}",
            f"detail: {self.trace_detail}",
            f"sessions: {sessions}",
        ]

    def _build_trace_coverage_lines(self):
        c = self.trace_coverage
        r = c.get("reasons", {})
        lines = ["=== TRACE COVERAGE ==="]
        lines.append(
            f"events: {c.get('events_shown', 0)}/{c.get('events_total', 0)} | "
            f"modules: {c.get('modules_shown_events', 0)}/{c.get('modules_total_events', 0)}"
        )
        lines.append(
            "hidden: "
            f"structural={int(r.get('structural_noise', 0))} "
            f"control_leaf={int(r.get('control_leaf_noise', 0))} "
            f"trivial={int(r.get('terminal_trivial', 0))} "
            f"tiny={int(r.get('tiny_leaf', 0))} "
            f"threshold={int(r.get('threshold_hidden', 0))}"
        )
        return lines

    def _build_trace_reproduce_lines(self):
        lines = ["=== TRACE REPRODUCE ==="]
        if not self.source_file_path:
            lines.append("same: (source file path unavailable)")
            lines.append("full: (source file path unavailable)")
            return lines

        same_cmd = _repro_cli_command(
            self.source_file_path,
            mode="TRACE",
            trace_detail=self.trace_detail,
            include_model_prompt=self.include_model_prompt,
        )
        full_cmd = _repro_cli_command(
            self.source_file_path,
            mode="TRACE",
            trace_detail=TRACE_FULL,
            include_model_prompt=self.include_model_prompt,
        )
        flow_threshold = float(self.trace_internal_metrics.get("flow_threshold_ms", 0.0))
        modules_threshold = float(self.trace_internal_metrics.get("modules_threshold_ms", 0.0))
        lines.append(f"same: {same_cmd}")
        lines.append(f"full: {full_cmd}")
        lines.append(
            "params: "
            f"flow_threshold_ms={flow_threshold:.2f} "
            f"modules_threshold_ms={modules_threshold:.2f} "
            f"collapse_repeats_min={int(self.trace_profile.get('collapse_repeat_min_count', 0))} "
            f"collapse_callbacks_min={int(self.trace_profile.get('collapse_callback_min_chain', 0))}"
        )
        return lines

    def _build_call_index(self, call_map):
        lines = ["=== CALL INDEX ==="]
        if not call_map:
            lines.append("Нет ключевых узлов.")
            return lines

        by_event = {id(item["event"]): item for item in call_map}

        def next_child(item):
            ref = item.get("reference")
            if not ref:
                return None
            block_events = ref["block"].get("events", [])
            candidates = []
            for e in block_events:
                child = by_event.get(id(e))
                if child and child is not item:
                    candidates.append(child)
            if not candidates:
                return None
            candidates.sort(key=lambda x: -float(x.get("budget", 0.0)))
            return candidates[0]

        main_chain = []
        used_events = set()
        cursor = call_map[0]
        while cursor and len(main_chain) < 8:
            eid = id(cursor["event"])
            if eid in used_events:
                break
            used_events.add(eid)
            main_chain.append(cursor["event"])
            cursor = next_child(cursor)

        if len(main_chain) < 2:
            for item in call_map:
                eid = id(item["event"])
                if eid in used_events:
                    continue
                used_events.add(eid)
                main_chain.append(item["event"])
                if len(main_chain) >= min(6, len(call_map)):
                    break

        if main_chain:
            chain = []
            for e in main_chain:
                chain.append(
                    f"{_compact_eid(e, default_type='fact')} "
                    f"{self._trace_location(e['Module'], e['Line'], e.get('Extension'))}"
                )
            lines.append(" → ".join(chain))

        if self.trace_detail == TRACE_COMPACT:
            return lines

        for item in call_map:
            e = item["event"]
            if id(e) in used_events:
                continue
            ctx = context_label(e.get("Context")) or "?"
            loc = self._trace_location(e["Module"], e["Line"], e.get("Extension"))
            code = (e.get("Code") or "").strip().replace("\n", " ")
            suffix = " (leaf)" if not item.get("reference") else ""
            lines.append(f"{_compact_eid(e, default_type='fact')} [{ctx}] {loc} {code}{suffix}".rstrip())
            used_events.add(id(e))
            if len(lines) >= 10:
                break
        return lines

    def _build_modules_section(self, grouped, modules_threshold, flow_modules=None):
        lines = ["=== MODULES ==="]
        shown_event_ids = set()
        hidden_reasons = _coverage_counters()

        for g in grouped:
            module_key = self._trace_module_key(g["module"], g.get("extension"))
            if flow_modules is not None and module_key not in flow_modules:
                continue

            module_events = sorted(g.get("events", []), key=_event_sort_key)
            module_total = sum(e.get("Total", 0) for e in module_events)
            if module_total < modules_threshold:
                hidden_reasons["threshold_hidden"] += len(module_events)
                if self.trace_detail == TRACE_COMPACT:
                    continue

            ctx = context_label(g.get("context")) or "?"
            module_alias = self._module_alias(g["module"], g.get("extension"))
            module_lines = []
            module_lines.append("")
            module_lines.append(f"--- [{ctx}] {module_alias} ---")

            hidden_blocks = 0

            for b in g.get("blocks", []):
                block_events = sorted(b.get("events", []), key=_event_sort_key)
                if b["total"] < modules_threshold:
                    hidden_reasons["threshold_hidden"] += len(block_events)
                    hidden_blocks += 1
                    continue

                if self.trace_detail == TRACE_FULL:
                    module_lines.append(
                        f"{b['type']}(:{b['line_start']}-{b['line_end']}) Total: {b['total']:.0f}ms (ref)"
                    )
                else:
                    module_lines.append(f"{b['type']}(:{b['line_start']}-{b['line_end']})")

                filtered_events = self._filter_block_events(block_events, modules_threshold, hidden_reasons)
                collapsed = self._collapse_repeated_events(filtered_events, hidden_reasons)
                if not collapsed:
                    hidden_blocks += 1
                    continue

                for item in collapsed:
                    if item["type"] == "single":
                        e = item["event"]
                        shown_event_ids.add(e.get("_event_id", "E00000"))
                        code_lines = (e["Code"] or "").replace("\r", "").split("\n")
                        if not code_lines:
                            code_lines = [""]
                        module_lines.append(
                            f"  {_compact_eid(e, default_type='fact')} :{e['Line']} | {code_lines[0]}"
                        )
                        for extra in code_lines[1:]:
                            module_lines.append(f"    | {extra}")
                    elif item["type"] == "collapsed":
                        first_num = int("".join(ch for ch in item["first_event_id"] if ch.isdigit()) or "0")
                        shown_event_ids.add(item["first_event_id"])
                        module_lines.append(
                            f"  #{first_num} :{item['line_start']}-{item['line_end']} | "
                            f"{item['count']}x {item['pattern']}"
                        )

                module_lines.append("")

            if hidden_blocks > 0 and self.trace_detail == TRACE_FULL:
                module_lines.append(f"  [+ {hidden_blocks} функций, Total < {modules_threshold:.1f}ms]")

            if self.trace_detail == TRACE_COMPACT and len(module_lines) <= 2:
                continue
            lines.extend(module_lines)

        return {
            "text": "\n".join(lines),
            "shown_event_ids": shown_event_ids,
            "hidden_reasons": hidden_reasons,
        }

    def generate_perf(self):
        """PERF v4: TOP ISSUES (для формата 2b/2c без Level) или дерево (для формата 1 с Level) + Hotspots."""
        tree_lines = []

        if self.has_level:
            # Формат 1: Level присутствует — используем классическое дерево
            # 1. Build Tree
            root = {'children': [], 'event': None, 'total': 0}
            stack = [root]

            for e in self.events:
                node = {'children': [], 'event': e, 'total': e['Total'], 'significant': False}
                target_idx = e['Level'] + 1

                while len(stack) > target_idx:
                    stack.pop()

                parent = stack[-1]
                parent['children'].append(node)
                stack.append(node)

            # 2. Threshold
            max_duration = 0
            for child in root['children']:
                if child['total'] > max_duration:
                    max_duration = child['total']

            threshold = self.threshold_ms if self.threshold_ms is not None else max(1.0, max_duration * 0.01)

            self._mark_significant(root, threshold)

            # 3. Tree Output
            tree_lines.append("=== PERF (дерево критического пути) ===")
            tree_lines.append(f"Порог: {threshold:.2f} мс (1% от макс.)")
            if self.show_context:
                tree_lines.append("Контекст показывается только при смене; при одном контексте — только у первой строки. [C]=Клиент, [S]=Сервер, [C→S]=Клиент вызывает Сервер.")
            tree_lines.append("-" * 80)

            self._print_tree(root, tree_lines, 0, None, None, None)
        else:
            # Формат 2b/2c: Level=0 — генерируем TOP ISSUES вместо плоского дерева
            grouped = ProcedureGrouper(self.events).group()
            issue_threshold = self._calc_modules_threshold()
            analyzer = IssueAnalyzer(self.events, grouped, issue_threshold)
            tree_lines.append(analyzer.build())

        # 4. Hotspots (v4: обогащённые — добавлен Total)
        hotspots_lines = []
        hotspots_lines.append("")
        hotspots_lines.append("=== HOTSPOTS (топ по чистому времени) ===")
        hotspots_lines.append("#  | Self(мс) | Total(мс) | Ctx | Module:Line | Code")

        agg = {}
        for e in self.events:
            ctx = e.get('Context')
            ext = e.get('Extension')
            key = (e['Module'], e['Line'], ctx, ext)
            if key not in agg:
                agg[key] = {'pure': 0, 'total': 0, 'count': 0, 'code': e['Code'],
                            'mod': e['Module'], 'ctx': ctx, 'ext': ext}
            agg[key]['pure'] += e['Pure']
            agg[key]['total'] += e['Total']
            agg[key]['count'] += 1

        sorted_agg = sorted(agg.items(), key=lambda x: x[1]['pure'], reverse=True)

        for i, (key, val) in enumerate(sorted_agg[:15]):
            ctx = val['ctx']
            ext = val['ext']
            ctx_label_str = context_label(ctx) if ctx is not None else "?"

            mod_name = val['mod']
            if ext and mod_name.startswith(ext + ' '):
                mod_name = mod_name[len(ext)+1:]

            mod_short = _module_short_name(mod_name)
            location = f"{mod_short}:{key[1]}"

            code_snip = val['code'].strip().replace('\n', ' ')[:60]
            hotspots_lines.append(
                f"{i+1:<3}| {val['pure']:>8.2f} | {val['total']:>9.2f} | {ctx_label_str:<3} | {location} | {code_snip}"
            )

        return "\n".join(tree_lines + hotspots_lines)

    def _mark_significant(self, node, threshold):
        is_sig = node['event'] is None or node['total'] >= threshold
        child_sig = False
        for child in node['children']:
            if self._mark_significant(child, threshold):
                child_sig = True
        node['significant'] = is_sig or child_sig
        return node['significant']

    def _print_tree(self, node, lines, indent_level, prev_context, prev_module, prev_extension):
        """Рекурсивный вывод дерева. Возвращает (последний модуль, последнее расширение)."""
        if node['event'] is None:
            last_mod = prev_module
            last_ext = prev_extension
            for child in node['children']:
                last_mod, last_ext = self._print_tree(child, lines, 0, prev_context, last_mod, last_ext)
            return last_mod, last_ext

        if not node['significant']:
            return prev_module, prev_extension

        e = node['event']
        ctx = e.get('Context')
        ext = e.get('Extension')
        
        ctx_label = context_label(ctx) if ctx is not None else ""
        show_ctx = self.show_context and (prev_context != ctx or (ctx_label and prev_context is None))
        ctx_str = f"[{ctx_label}] " if show_ctx and ctx_label else (" " * (CONTEXT_COL_WIDTH + 1) if self.show_context else "")
        
        show_module = (prev_module is None or e['Module'] != prev_module or ext != prev_extension)
        
        mod_name = e['Module']
        if ext and mod_name.startswith(ext + ' '):
            mod_name = mod_name[len(ext)+1:]
            
        mid = self.get_module_id(mod_name) if show_module else ""
        
        if show_module:
            if ext:
                location = f"[Ext:{ext}] {mid}:{e['Line']}"
            else:
                location = f"{mid}:{e['Line']}"
        else:
            location = f":{e['Line']}"
            
        indent = "  " * indent_level
        code_snip = e['Code'].strip().replace('\n', ' ')[:60]
        lines.append(f"{indent}{ctx_str}{location} | {code_snip} [Всего: {e['Total']:.2f} мс]")

        hidden_count = 0
        hidden_time = 0
        last_mod = e['Module']
        last_ext = ext
        
        for child in node['children']:
            if child['significant']:
                last_mod, last_ext = self._print_tree(child, lines, indent_level + 1, ctx, last_mod, last_ext)
            else:
                hidden_count += 1
                hidden_time += child['total']

        if hidden_count > 0:
            lines.append(f"{indent}  [+ {hidden_count} мелких вызовов: {hidden_time:.2f} мс]")
        return last_mod, last_ext

    def _calc_trace_flow_threshold(self):
        """Адаптивный порог значимости для TRACE (без хардкода по модулям/именам)."""
        if self.threshold_ms is not None:
            return max(0.0, float(self.threshold_ms))

        budgets = sorted(
            (e["Total"] - e["Pure"] for e in self.events if (e["Total"] - e["Pure"]) > 0),
            reverse=True
        )
        if not budgets:
            return float(self.trace_profile.get("flow_floor_ms", 0.5))

        total_ms = sum(e["Total"] for e in self.events)
        base_threshold = max(
            float(self.trace_profile.get("flow_floor_ms", 0.5)),
            total_ms * float(self.trace_profile.get("flow_base_factor", 0.0004)),
        )

        # Держим плотность EXECUTION FLOW управляемой: срез по 150-му значимому budget.
        target_rank = min(int(self.trace_profile.get("flow_target_rank", 150)), len(budgets))
        rank_idx = max(0, target_rank - 1)
        rank_threshold = budgets[rank_idx]

        return max(base_threshold, rank_threshold)

    def _calc_trace_modules_threshold(self, flow_threshold):
        """MODULES threshold for TRACE based on active trace_detail profile."""
        ratio = float(self.trace_profile.get("modules_ratio", 0.25))
        floor = float(self.trace_profile.get("modules_floor_ms", 0.5))
        return max(floor, flow_threshold * ratio)

    def _limit_call_map(self, call_map):
        """Ограничить длину CALL MAP по покрытию бюджета и максимальному размеру."""
        if not call_map:
            return call_map, 0

        max_items_profile = int(self.trace_profile.get("call_map_max_items", 240))
        min_items_profile = int(self.trace_profile.get("call_map_min_items", 60))
        max_items = max(1, min(max_items_profile, max(1, len(call_map))))
        min_items = min(max_items, max(1, min_items_profile))
        target_coverage = float(self.trace_profile.get("call_map_target_coverage", 0.90))
        total_budget = sum(item["budget"] for item in call_map)

        selected = []
        covered = 0.0
        for item in call_map:
            selected.append(item)
            covered += item["budget"]
            if len(selected) >= max_items:
                break
            if len(selected) >= min_items and total_budget > 0:
                if covered / total_budget >= target_coverage:
                    break

        omitted = max(0, len(call_map) - len(selected))
        return selected, omitted

    def _calc_modules_threshold(self):
        """Вычислить порог для MODULES: используем threshold_ms или 1% от общего времени."""
        if self.threshold_ms is not None:
            return self.threshold_ms
        total_ms = sum(e["Total"] for e in self.events)
        return max(0.5, total_ms * 0.01)

    def _filter_block_events(self, events, modules_threshold=0.0, hidden_reasons=None):
        """Правило 2: фильтр строк — убрать структурный шум и тривиальные строки."""
        result = []
        hidden_reasons = hidden_reasons if hidden_reasons is not None else _coverage_counters()
        control_threshold = max(
            0.05,
            modules_threshold * float(self.trace_profile.get("control_multiplier", 1.0)),
        )
        tiny_leaf_limit = float(self.trace_profile.get("leaf_tiny_total_ms", 0.01))
        for e in events:
            code = (e.get("Code") or "").strip()

            # Убрать структурный шум
            if any(code.startswith(noise) for noise in STRUCTURAL_NOISE):
                hidden_reasons["structural_noise"] += 1
                continue

            # Убрать пустые управляющие переходы (без информативной нагрузки).
            if code in TERMINAL_FLOW_STATEMENTS and e["Total"] <= max(0.1, modules_threshold):
                hidden_reasons["terminal_trivial"] += 1
                continue

            # Убрать дешёвые управляющие конструкции (Если/Иначе/Цикл), если они не вызывают модуль.
            has_called_module = _extract_called_module(code) is not None
            if (_is_control_flow_line(code) and not has_called_module
                    and _is_leaf_like_event(e) and e["Total"] <= control_threshold):
                hidden_reasons["control_leaf_noise"] += 1
                continue

            # Убрать тривиальные строки: Total < 0.01 мс И Pure ≈ Total (разница < 10%)
            if e["Total"] < tiny_leaf_limit:
                if e["Total"] == 0 or _is_leaf_like_event(e):
                    hidden_reasons["tiny_leaf"] += 1
                    continue
            result.append(e)
        return result

    def _collapse_repeated_events(self, events, hidden_reasons=None):
        """Правило 3: коллапс повторов — если N >= 5 подряд с одинаковым паттерном кода."""
        if not events:
            return []
        hidden_reasons = hidden_reasons if hidden_reasons is not None else _coverage_counters()

        def _code_pattern(code):
            """Извлечь паттерн кода (до первой скобки или первое слово)."""
            code = (code or "").strip().split("\n")[0]
            paren = code.find("(")
            if paren > 0:
                return code[:paren].strip()
            return code[:40].strip()

        result = []
        i = 0
        while i < len(events):
            pat = _code_pattern(events[i].get("Code"))
            j = i + 1
            while j < len(events) and _code_pattern(events[j].get("Code")) == pat:
                j += 1
            count = j - i
            collapse_min_count = int(self.trace_profile.get("collapse_repeat_min_count", 5))
            if count >= collapse_min_count:
                total = sum(e["Total"] for e in events[i:j])
                first_event_id = events[i].get("_event_id", "E00000")
                last_event_id = events[j - 1].get("_event_id", first_event_id)
                hidden_reasons["collapsed_repeats"] += max(0, count - 1)
                result.append({
                    "type": "collapsed",
                    "line_start": events[i]["Line"],
                    "line_end": events[j-1]["Line"],
                    "count": count,
                    "pattern": pat + "(...)",
                    "total": total,
                    "first_event_id": first_event_id,
                    "last_event_id": last_event_id,
                })
            else:
                for k in range(i, j):
                    result.append({"type": "single", "event": events[k]})
            i = j
        return result

    def _ensure_modules(self):
        """Предзаполнить легенду модулей из событий."""
        for e in sorted(self.events, key=_event_sort_key):
            self.get_module_id(e['Module'])

    def _get_extensions(self):
        """Собрать уникальные имена расширений из событий."""
        exts = set()
        for e in self.events:
            ext = e.get("Extension")
            if ext:
                exts.add(ext)
        return sorted(exts)

    def get_full_report(self, mode="TRACE", include_model_prompt=True):
        parts = []
        include_model_prompt = bool(include_model_prompt)

        # Header + Summary
        parts.append("=== ОТЧЁТ PFF ===")
        parts.append("")
        parts.append(self.generate_summary())
        parts.append("")

        # Trace
        if mode == "TRACE":
            if include_model_prompt:
                parts.append(build_trace_model_prompt(self.trace_detail).strip())
                parts.append("")
            parts.append(self.generate_trace())
            parts.append("")

        # Perf + Hotspots
        if mode == "PERF":
            if include_model_prompt:
                parts.append(PERF_MODEL_PROMPT.strip())
                parts.append("")
            parts.append(self.generate_perf())

        return "\n".join(parts)


# ==================================================================================================
# 4. MAIN EXECUTION
# ==================================================================================================

def process_pff(file_path, threshold_ms=None, mode="TRACE", trace_detail=TRACE_COMPACT, no_compact=False,
                no_context=False, expand_module_names=True, include_model_prompt=True):
    if not os.path.exists(file_path):
        return "File not found."

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    parser = PFFStreamParser(content)
    all_evts = list(parser.parse_events())
    session_info = parser.session_info

    if not all_evts:
        return "No trace events found in file."

    # Восстановление стека вызовов (Phase 4.2) - REMOVED CallTreeBuilder
    # builder = CallTreeBuilder(all_evts)
    # all_evts = builder.build()

    mode = (mode or "TRACE").upper()
    normalized_trace_detail = normalize_trace_detail(trace_detail)
    events = all_evts

    effective_threshold_ms = threshold_ms
    if mode == "TRACE":
        effective_threshold_ms = None
        if no_compact:
            normalized_trace_detail = TRACE_FULL

    generator = ReportGenerator(
        events,
        session_info=session_info,
        threshold_ms=effective_threshold_ms,
        all_events=all_evts,
        trace_detail=normalized_trace_detail,
        show_context=not no_context,
        expand_module_names=expand_module_names,
        source_file_path=file_path,
        trace_request_meta={},
        include_model_prompt=include_model_prompt,
    )
    return generator.get_full_report(mode=mode, include_model_prompt=include_model_prompt)


def _default_output_path(file_path, mode, trace_detail=None):
    base_dir = os.path.dirname(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    if (mode or "").upper() == "TRACE":
        detail = normalize_trace_detail(trace_detail)
        suffix = f"_TRACE_{detail.upper()}"
    else:
        suffix = "_PERF"
    return os.path.join(base_dir, f"{base_name}{suffix}.txt")


def main():
    parser = argparse.ArgumentParser(
        description="Парсер PFF (1С Performance) — TRACE + PERF"
    )
    parser.add_argument("file", nargs="?", help="Путь к PFF-файлу")
    parser.add_argument("output", nargs="?", help="Путь к отчёту (опционально)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Порог значимости (мс) для PERF. В TRACE игнорируется (порог=0).")
    parser.add_argument("--mode", choices=["TRACE", "PERF"], default="TRACE",
                        help="Режим: TRACE (трассировка) или PERF (производительность)")
    parser.add_argument("--trace-detail", default=None,
                        help="TRACE detail: full | compact (default: compact).")
    parser.add_argument("--no-compact", action="store_true",
                        help="DEPRECATED alias for --trace-detail full")
    parser.add_argument("--no-context", action="store_true",
                        help="Не показывать контекст выполнения (C/S/C->S)")
    parser.add_argument("--no-expand-modules", action="store_true",
                        help="DEPRECATED no-op (оставлено для обратной совместимости)")
    parser.add_argument("--no-model-prompt", action="store_true",
                        help="Не включать промпт для модели в заголовок отчёта")

    args = parser.parse_args()

    f_name = args.file
    if not f_name:
        print("Error: No file specified.")
        return
    out_name = args.output
    if args.mode == "TRACE":
        try:
            trace_detail = normalize_trace_detail(
                args.trace_detail if args.trace_detail is not None else TRACE_COMPACT,
                warn_if_normal=True,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return
        if args.no_compact and trace_detail != TRACE_FULL:
            print(
                "Warning: --no-compact is deprecated and overrides --trace-detail to 'full'.",
                file=sys.stderr,
            )
            trace_detail = TRACE_FULL
        elif args.no_compact:
            trace_detail = TRACE_FULL
    else:
        trace_detail = TRACE_COMPACT
        if args.trace_detail is not None:
            print("Warning: --trace-detail is used only with --mode TRACE.", file=sys.stderr)
        if args.no_compact:
            print("Warning: --no-compact is used only with --mode TRACE.", file=sys.stderr)
    if args.no_expand_modules:
        print("Warning: --no-expand-modules is deprecated and ignored.", file=sys.stderr)

    report = process_pff(
        f_name,
        threshold_ms=args.threshold,
        mode=args.mode,
        trace_detail=trace_detail,
        no_compact=args.no_compact,
        no_context=args.no_context,
        expand_module_names=True,
        include_model_prompt=not args.no_model_prompt
    )

    try:
        print(report)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((report + "\n").encode('utf-8'))

    out_path = out_name or _default_output_path(f_name, args.mode, trace_detail)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nОтчёт сохранён: {out_path}")


if __name__ == "__main__":
    main()
