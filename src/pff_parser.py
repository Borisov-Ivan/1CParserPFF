#!/usr/bin/env python3
"""
Парсер PFF (1С Performance) — двухрежимный вывод: TRACE + PERF.

Следует ТЗ Парсер.md и Идеальный формат отчёта.md:
- TRACE: полный хронологический лог (без сжатия, для reasoning/отладки)
- PERF: дерево критического пути + Hotspots (с фильтрацией шума)

Поддерживает: --entry, --main-block, --threshold, --no-perf, --perf-only, --no-hotspots, --no-compact, --no-context, --no-expand-modules
"""

import sys
import os
import argparse
import re

# ==================================================================================================
# 1. CORE PARSER (STREAM TOKENIZER)
# ==================================================================================================


# Контекст выполнения (1=клиент, 2=сервер, 3–6=клиент → вызов сервера)
CONTEXT_LABELS = {
    1: 'C', 2: 'S',
    3: 'C→S', 4: 'C→S', 5: 'C→S', 6: 'C→S',
}

# Промпты для модели в заголовке отчёта (при включённой опции на форме)
TRACE_MODEL_PROMPT = """=== ПРОМПТ ДЛЯ МОДЕЛИ (TRACE) ===
Ты анализируешь отчёт трассировки выполнения 1С (PFF TRACE).

Правила работы с файлом:
- TRACE содержит три секции: SUMMARY, CALL MAP, MODULES.
- В MODULES данные сгруппированы по (модуль + контекст + расширение).
- Блоки вида [S] Func (lines X-Y) или [C] Proc (lines X-Y) — это идентификаторы диапазонов строк; имена процедур в PFF обычно отсутствуют.
- Внутри блока каждая строка: :Line | Code  Total  Pure (мс).
- В CALL MAP показываются строки, где Budget = Total - Pure > threshold. Это индикатор времени, ушедшего во вложенные вызовы.
- Ссылки вида -> see: ... [S] Func(lines X-Y) [Total: N ms] — эвристические подсказки по совпадению бюджета и модуля, не строгая гарантия.
- Контекстные метки: [C]=Клиент, [S]=Сервер, [C→S]=вызов сервера с клиента, [?]=неизвестно.

Интерпретация:
- Сначала смотри SUMMARY (масштаб и контексты), затем CALL MAP (кандидаты на вызовы), затем MODULES (детали по строкам).
- Для реконструкции цепочки вызовов сопоставляй Budget из CALL MAP с Total блоков в MODULES.
- При ответе явно отделяй подтверждённые факты (строки/время) от гипотез по связям вызовов.

=== КОНЕЦ ПРОМПТА ===
"""

PERF_MODEL_PROMPT = """=== ПРОМПТ ДЛЯ МОДЕЛИ (PERF) ===
Ты анализируешь отчёт производительности 1С (PFF PERF): дерево критического пути и горячие точки.

Правила работы с файлом:
- Дерево PERF: отступ задаёт уровень вложенности вызовов. Имя модуля выводится только при смене; иначе только :НомерСтроки. Модуль для «:Число» совпадает с последним выше указанным полным именем модуля.
- [Всего: X мс] — включительное время узла (узел + все потомки). Порог отсекает мелкие узлы; скрытые показаны как «+ N мелких вызовов: X мс».
- Секция HOTSPOTS: топ по «чистому» времени (без учёта вложенных вызовов). Имя модуля — по тому же правилу (только при смене).
- Сводка в начале отчёта: «Общее время» — сумма по L0; блоки B0, B1, … — сегменты замера.

Интерпретация:
- Сопоставляй узлы дерева с HOTSPOTS: высокое «Всего» при малом «Чистое» — время во вложенных вызовах; высокое «Чистое» — узкое место в самом узле.
- Контекст [C]/[S]/[C→S] при смене помогает отделять клиентскую и серверную нагрузку. [C→S] — выполнение на сервере (Обр. сервером). [?] — неизвестное значение контекста в PFF.
- Для строк только с «:Строка» определяй модуль по последней выше стоящей строке с полным именем модуля.

=== КОНЕЦ ПРОМПТА ===
"""


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
                                    base + 5, base + 6, base + 9, sig_idx=base
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
# 2. FILTERING (Entry Point, Main Block)
# ==================================================================================================

def resolve_entry(events, entry_spec):
    """Найти событие по entry_spec: 'Module:Line' или подстрока кода."""
    if not entry_spec:
        return None
    if ':' in entry_spec and not entry_spec.startswith('"'):
        parts = entry_spec.rsplit(':', 1)
        if len(parts) == 2:
            mod, line_str = parts
            try:
                line = int(line_str)
                for e in events:
                    if e['Module'] == mod and e['Line'] == line:
                        return e
            except ValueError:
                pass
    else:
        for e in events:
            if entry_spec in e.get('Code', ''):
                return e
    return None


def filter_by_entry(events, entry_spec):
    """
    Оставить только поддерево точки входа.
    Поддерево = entry + все события после него с Level > entry.Level до первого Level <= entry.Level.
    """
    if not entry_spec:
        return events
    entry_event = resolve_entry(events, entry_spec)
    if not entry_event:
        return events

    # Найти индекс entry (по ссылке на объект)
    idx = None
    for i, e in enumerate(events):
        if e is entry_event:
            idx = i
            break
    if idx is None:
        return events

    entry_level = entry_event['Level']
    result = [events[idx]]
    for j in range(idx + 1, len(events)):
        if events[j]['Level'] <= entry_level:
            break
        result.append(events[j])
    return result


def filter_by_main_block(events, main_block):
    """Оставить только события из блока main_block (0, 1, 2...)."""
    if main_block is None:
        return events
    return [e for e in events if e.get('block_id', 0) == main_block]


def classify_block(events, block_id):
    """
    Эвристика: под (подписчики) — мало L0, осн — много L0, смес — промежуточное.
    """
    block_events = [e for e in events if e.get('block_id') == block_id]
    if not block_events:
        return "неизв"
    l0_count = sum(1 for e in block_events if e['Level'] == 0)
    ratio = l0_count / len(block_events)
    if ratio < 0.1:
        return "под"
    elif ratio > 0.5:
        return "осн"
    else:
        return "смес"


def _strip_extension_prefix(module_name, extension):
    if extension and module_name.startswith(extension + " "):
        return module_name[len(extension) + 1:]
    return module_name


def _module_short_name(module_name):
    parts = module_name.split(".")
    if len(parts) >= 2 and parts[-1] == "Модуль":
        return parts[-2]
    return parts[-1] if parts else module_name


class ProcedureGrouper:
    """Группирует события в блоки [Ctx] Func/Proc (lines X-Y)."""

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
        for e in self.events:
            key = (e["Module"], e.get("Context"), e.get("Extension"))
            if key not in index:
                index[key] = len(groups)
                groups.append({
                    "module": e["Module"],
                    "context": e.get("Context"),
                    "extension": e.get("Extension"),
                    "events": [],
                })
            groups[index[key]]["events"].append(e)
        for g in groups:
            g["blocks"] = self._split_group_to_blocks(g["events"])
        return groups


class CallMapBuilder:
    """Строит CALL MAP по бюджету вложенных вызовов (Total - Pure)."""

    CALL_REGEX = re.compile(r"([\w]+)\s*\.\s*([\w]+)\s*\(")

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
        for match in self.CALL_REGEX.finditer(code or ""):
            return match.group(1)
        return None

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
            result.append(item)
        result.sort(key=lambda x: x["budget"], reverse=True)
        return result


# ==================================================================================================
# 3. REPORT GENERATOR (TRACE & PERF)
# ==================================================================================================

# Ширина колонки контекста (для выравнивания при "только при смене")
CONTEXT_COL_WIDTH = 5  # "[C→S]" = 5 символов



class ReportGenerator:
    def __init__(self, events, threshold_ms=None, all_events=None, compact=True, show_context=True, expand_module_names=True):
        self.events = events
        self.all_events = all_events if all_events is not None else events
        self.modules_map = {}
        self.modules_list = []
        self.threshold_ms = threshold_ms
        self.compact = compact
        self.show_context = show_context
        self.expand_module_names = expand_module_names

        if events:
            min_level = min(e['Level'] for e in events)
            for e in events:
                e['Level'] -= min_level

    def get_module_id(self, full_name):
        """При expand_module_names — полное имя; иначе M1, M2 и секция МОДУЛИ."""
        if self.expand_module_names:
            return full_name
        if full_name not in self.modules_map:
            mid = f"M{len(self.modules_map) + 1}"
            self.modules_map[full_name] = mid
            self.modules_list.append(f"{mid} = {full_name}")
        return self.modules_map[full_name]

    def generate_summary(self, entry_info, blocks_info):
        """Секция СВОДКА. Общее время: при наличии _block_total_sec (формат 2b, время+процент) — по блокам;
        иначе сумма по событиям уровня L0."""
        # Формат 2b: общее время блока = time_sec * 100 / percent (одно значение на блок)
        by_block_sec = {}
        for e in self.events:
            if "_block_total_sec" in e:
                bid = e.get("block_id", 0)
                if bid not in by_block_sec:
                    by_block_sec[bid] = e["_block_total_sec"]
        if by_block_sec:
            total_time = sum(by_block_sec.values()) * 1000  # сек -> мс
        else:
            total_time = sum(e["Total"] for e in self.events if e["Level"] == 0)
        main_branch_time = total_time
        pct = 100 if total_time > 0 else 0

        lines = []
        lines.append("=== СВОДКА ===")
        lines.append(f"События: {len(self.events)}")
        lines.append(f"Вход: {entry_info}")
        lines.append(f"Блоки: {blocks_info}")
        lines.append(f"Общее время: {total_time/1000:.2f} с")
        lines.append(f"Основная ветвь (L0): {main_branch_time/1000:.2f} с ({pct:.0f}%)")
        return "\n".join(lines)

    def _format_location(self, module, line, extension=None):
        """Форматирует местоположение: [Ext:Name] Module:Line"""
        mod_name = module
        if extension and module.startswith(extension + ' '):
            mod_name = module[len(extension)+1:]
            
        mid = self.get_module_id(mod_name)
        loc = f"{mid}:{line}" if mid else f":{line}"
        
        if extension:
            return f"[Ext:{extension}] {loc}"
        return loc

    def generate_trace(self):
        """TRACE v3: SUMMARY + CALL MAP + MODULES."""
        lines = []
        grouped = ProcedureGrouper(self.events).group()
        call_map = CallMapBuilder(self.events, grouped, threshold_ms=self.threshold_ms).build()

        by_block_sec = {}
        for e in self.events:
            if "_block_total_sec" in e:
                bid = e.get("block_id", 0)
                if bid not in by_block_sec:
                    by_block_sec[bid] = e["_block_total_sec"]
        if by_block_sec:
            total_time_ms = sum(by_block_sec.values()) * 1000.0
        else:
            total_time_ms = sum(e["Total"] for e in self.events if e.get("Level", 0) == 0)
        if total_time_ms <= 0:
            total_time_ms = sum(e["Total"] for e in self.events)

        blocks_count = len(set(e.get("block_id", 0) for e in self.events))
        contexts = sorted(set(context_label(e.get("Context")) or "?" for e in self.events))

        lines.append("=== TRACE ===")
        lines.append("")
        lines.append("=== SUMMARY ===")
        lines.append(f"Events: {len(self.events)} | Blocks: {blocks_count} | Total: {total_time_ms/1000.0:.2f}s")
        lines.append("Contexts: " + ", ".join(f"[{c}]" for c in contexts))
        lines.append("")

        lines.append("=== CALL MAP ===")
        lines.append("Budget = Total - Pure (время, ушедшее во вложенные вызовы).")
        if not call_map:
            lines.append("Нет строк с заметным бюджетом вложенных вызовов.")
        else:
            for idx, item in enumerate(call_map, 1):
                e = item["event"]
                ctx = context_label(e.get("Context")) or "?"
                mod_name = _strip_extension_prefix(e["Module"], e.get("Extension"))
                mod_short = _module_short_name(mod_name)
                lines.append(f"#{idx} [{ctx}] {mod_short}:{e['Line']} | Budget: {item['budget']:.2f}ms")
                lines.append(f"   {e['Code'].strip().replace(chr(10), ' ')[:180]}")
                ref = item["reference"]
                if ref:
                    g = ref["group"]
                    b = ref["block"]
                    g_ctx = context_label(g.get("context")) or "?"
                    g_mod = _strip_extension_prefix(g["module"], g.get("extension"))
                    g_short = _module_short_name(g_mod)
                    lines.append(
                        f"   -> see: {g_short} [{g_ctx}] {b['type']}(lines {b['line_start']}-{b['line_end']}) [Total: {b['total']:.2f}ms]"
                    )
                lines.append("")

        lines.append("=== MODULES ===")
        for g in grouped:
            ctx = context_label(g.get("context")) or "?"
            mod_name = _strip_extension_prefix(g["module"], g.get("extension"))
            module_display = self.get_module_id(mod_name)
            if g.get("extension"):
                lines.append(f"--- [Ext:{g['extension']}] {module_display} ---")
            else:
                lines.append(f"--- {module_display} ---")
            lines.append("")
            for b in g.get("blocks", []):
                lines.append(
                    f"  [{ctx}] {b['type']} (lines {b['line_start']}-{b['line_end']}) "
                    f"Total: {b['total']:.2f}ms Pure: {b['pure']:.2f}ms"
                )
                for e in b["events"]:
                    code_lines = (e["Code"] or "").replace("\r", "").split("\n")
                    if not code_lines:
                        code_lines = [""]
                    lines.append(f"    :{e['Line']} | {code_lines[0]}  {e['Total']:.3f}  {e['Pure']:.3f}")
                    for extra in code_lines[1:]:
                        lines.append(f"           | {extra}")
                lines.append("")

        return "\n".join(lines)

    def generate_perf(self):
        """PERF: дерево критического пути + Hotspots."""
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
        tree_lines = []
        tree_lines.append("=== PERF (дерево критического пути) ===")
        tree_lines.append(f"Порог: {threshold:.2f} мс (1% от макс.)")
        if self.show_context:
            tree_lines.append("Контекст показывается только при смене; при одном контексте — только у первой строки. [C]=Клиент, [S]=Сервер, [C→S]=Клиент вызывает Сервер.")
        tree_lines.append("-" * 80)

        self._print_tree(root, tree_lines, 0, None, None, None)

        # 4. Hotspots
        hotspots_lines = []
        hotspots_lines.append("")
        hotspots_lines.append("=== HOTSPOTS (топ по чистому времени) ===")
        if self.show_context:
            hotspots_lines.append("Контекст показывается только при смене; при одном контексте — только у первой строки. [C]=Клиент, [S]=Сервер, [C→S]=Клиент вызывает Сервер.")

        agg = {}
        for e in self.events:
            ctx = e.get('Context')
            ext = e.get('Extension')
            key = (e['Module'], e['Line'], ctx, ext)
            if key not in agg:
                agg[key] = {'pure': 0, 'count': 0, 'code': e['Code'], 'mod': e['Module'], 'ctx': ctx, 'ext': ext}
            agg[key]['pure'] += e['Pure']
            agg[key]['count'] += 1

        sorted_agg = sorted(agg.items(), key=lambda x: x[1]['pure'], reverse=True)

        prev_hotspot_ctx = None
        prev_hotspot_mod = None
        prev_hotspot_ext = None
        
        for i, (key, val) in enumerate(sorted_agg[:10]):
            ctx = val['ctx']
            ext = val['ext']
            
            ctx_label = context_label(ctx) if ctx is not None else ""
            show_ctx = self.show_context and (prev_hotspot_ctx != ctx or (ctx_label and prev_hotspot_ctx is None))
            ctx_str = f"[{ctx_label}] " if show_ctx and ctx_label else (" " * (CONTEXT_COL_WIDTH + 1) if self.show_context else "")
            prev_hotspot_ctx = ctx
            
            show_module = (prev_hotspot_mod is None or val['mod'] != prev_hotspot_mod or ext != prev_hotspot_ext)
            
            mod_name = val['mod']
            if ext and mod_name.startswith(ext + ' '):
                mod_name = mod_name[len(ext)+1:]
            
            mid = self.get_module_id(mod_name) if show_module else ""
            
            if show_module:
                if ext:
                    location = f"[Ext:{ext}] {mid}:{key[1]}"
                else:
                    location = f"{mid}:{key[1]}"
            else:
                location = f":{key[1]}"
            
            prev_hotspot_mod = val['mod']
            prev_hotspot_ext = ext
            
            code_snip = val['code'].strip().replace('\n', ' ')[:80]
            hotspots_lines.append(f"#{i+1} {ctx_str}{location} | Чистое: {val['pure']:.2f} мс ({val['count']}x) | {code_snip}")

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

    def _ensure_modules(self):
        """Предзаполнить легенду модулей из событий."""
        for e in self.events:
            self.get_module_id(e['Module'])

    def _get_extensions(self):
        """Собрать уникальные имена расширений из событий."""
        exts = set()
        for e in self.events:
            ext = e.get("Extension")
            if ext:
                exts.add(ext)
        return sorted(exts)

    def get_full_report(self, entry_info="all", blocks_info="", include_trace=True, include_perf=True, include_hotspots=True, include_model_prompt=True):
        parts = []
        self._ensure_modules()

        # Header + Summary
        parts.append("=== ОТЧЁТ PFF ===")
        parts.append("")
        parts.append(self.generate_summary(entry_info, blocks_info))
        parts.append("")

        # Extensions
        extensions = self._get_extensions()
        if extensions:
            parts.append("=== РАСШИРЕНИЯ ===")
            parts.extend(extensions)
            parts.append("")

        # Modules (только при коротких именах M1, M2)
        parts.append("=== МОДУЛИ ===")
        if not self.expand_module_names and self.modules_list:
            parts.extend(self.modules_list)
        parts.append("")

        # Trace
        if include_trace:
            if include_model_prompt:
                parts.append(TRACE_MODEL_PROMPT.strip())
                parts.append("")
            parts.append(self.generate_trace())
            parts.append("")

        # Perf + Hotspots
        if include_perf or include_hotspots:
            if include_model_prompt:
                parts.append(PERF_MODEL_PROMPT.strip())
                parts.append("")
            parts.append(self.generate_perf())

        return "\n".join(parts)


# ==================================================================================================
# 4. MAIN EXECUTION
# ==================================================================================================

def process_pff(file_path, entry=None, main_block=None,
                threshold_ms=None, no_perf=False, perf_only=False, no_hotspots=False, no_compact=False,
                no_context=False, expand_module_names=True, include_model_prompt=True):
    if not os.path.exists(file_path):
        return "File not found."

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    parser = PFFStreamParser(content)
    all_evts = list(parser.parse_events())

    if not all_evts:
        return "No trace events found in file."

    # Восстановление стека вызовов (Phase 4.2) - REMOVED CallTreeBuilder
    # builder = CallTreeBuilder(all_evts)
    # all_evts = builder.build()

    num_blocks = len(set(e.get('block_id', 0) for e in all_evts))

    # Фильтрация: при entry — отбор блока, в который входит указанная строка; при main_block — только этот блок
    if entry:
        entry_event = resolve_entry(all_evts, entry)
        if entry_event is not None:
            bid = entry_event.get('block_id', 0)
            events = filter_by_main_block(all_evts, bid)
            entry_info = entry
            blocks_info = f"Блок B{bid}, содержащий «{entry}» ({len(events)} соб.)"
        else:
            events = all_evts
            entry_info = entry
            blocks_info = f"{num_blocks} блоков, точка входа не найдена ({len(events)} соб.)"
    elif main_block is not None:
        events = filter_by_main_block(all_evts, main_block)
        bt = classify_block(all_evts, main_block)
        blocks_info = f"{num_blocks} всего, B{main_block}: {len(events)} соб., {bt}"
        entry_info = f"блок {main_block}"
    elif num_blocks > 1:
        events = all_evts
        parts = []
        for bid in sorted(set(e.get('block_id', 0) for e in all_evts)):
            n = sum(1 for x in all_evts if x.get('block_id') == bid)
            bt = classify_block(all_evts, bid)
            parts.append(f"B{bid}: {n} соб., {bt}")
        blocks_info = " | ".join(parts)
        entry_info = "все"
    else:
        events = all_evts
        blocks_info = "1"
        entry_info = "все"

    generator = ReportGenerator(events, threshold_ms, all_events=all_evts, compact=not no_compact, show_context=not no_context, expand_module_names=expand_module_names)
    if perf_only:
        include_trace = False
        include_perf = True
        include_hotspots = not no_hotspots
    else:
        include_trace = True
        include_perf = not no_perf
        include_hotspots = not no_hotspots
    return generator.get_full_report(
        entry_info=entry_info,
        blocks_info=blocks_info,
        include_trace=include_trace,
        include_perf=include_perf,
        include_hotspots=include_hotspots,
        include_model_prompt=include_model_prompt
    )


def main():
    parser = argparse.ArgumentParser(
        description="Парсер PFF (1С Performance) — TRACE + PERF"
    )
    parser.add_argument("file", nargs="?", help="Путь к PFF-файлу")
    parser.add_argument("output", nargs="?", help="Путь к отчёту (опционально)")
    parser.add_argument("--entry", help="Точка входа: Module:Line или подстрока кода")
    parser.add_argument("--main-block", type=int, default=None,
                        help="Показать только блок N (0, 1, 2...). По умолчанию — все блоки")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Порог значимости (ms). По умолчанию: 1%% от max")
    parser.add_argument("--no-perf", action="store_true", help="Только TRACE")
    parser.add_argument("--perf-only", action="store_true",
                        help="Только PERF (без TRACE): дерево критического пути + Hotspots")
    parser.add_argument("--no-hotspots", action="store_true", help="Без HOTSPOTS")
    parser.add_argument("--no-compact", action="store_true",
                        help="Все события с полным префиксом (без объединения продолжений)")
    parser.add_argument("--no-context", action="store_true",
                        help="Не показывать контекст выполнения (C/S/C->S)")
    parser.add_argument("--no-expand-modules", action="store_true",
                        help="Короткие имена модулей (M1, M2) и секция MODULES")
    parser.add_argument("--no-model-prompt", action="store_true",
                        help="Не включать промпт для модели в заголовок отчёта")

    args = parser.parse_args()

    f_name = args.file or "Замер для исследований.pff.txt"
    out_name = args.output

    report = process_pff(
        f_name,
        entry=args.entry,
        main_block=args.main_block,
        threshold_ms=args.threshold,
        no_perf=args.no_perf,
        perf_only=args.perf_only,
        no_hotspots=args.no_hotspots,
        no_compact=args.no_compact,
        no_context=args.no_context,
        expand_module_names=not args.no_expand_modules,
        include_model_prompt=not args.no_model_prompt
    )

    try:
        print(report)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((report + "\n").encode('utf-8'))

    default_report_name = "PFF_Perf.txt" if args.perf_only else "PFF_Report.txt"
    out_path = out_name or os.path.join(os.path.dirname(f_name), default_report_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nОтчёт сохранён: {out_path}")


if __name__ == "__main__":
    main()
