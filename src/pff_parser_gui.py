#!/usr/bin/env python3
"""
Основной проект: GUI для парсера PFF (1С Performance).

Параметры и сохранение по умолчанию соответствуют форме 1С (Модуль.txt).
Результат выводится в многострочном текстовом поле; путь сохранённого файла — в панели внизу.
Параметры недоступны для неактивного режима.

Общее время в СВОДКА: для формата 2b — по времени и проценту (time_sec * 100 / percent);
иначе — сумма по событиям уровня L0.

Остальные варианты парсера (1CParserPFF, копии из temp) перенесены в папку old.
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Импорт из локального модуля (тот же каталог)
from pff_parser import process_pff

# Ссылки на виджеты для включения/выключения по режиму (заполняются в build_ui)
_widgets_for_mode = None


def get_save_path(file_path: str, mode: str) -> str:
    """Путь сохранения по правилу 1С: каталог + имя_без_расширения + _TRACE|_PERF + .txt"""
    base = os.path.splitext(file_path)[0]
    suffix = "_TRACE" if mode == "TRACE" else "_PERF"
    return base + suffix + ".txt"


def fill_result_text(text_widget: tk.Text, report: str):
    """Заполнить многострочное текстовое поле отчётом."""
    text_widget.delete("1.0", tk.END)
    text_widget.insert(tk.END, report)


def update_mode_sensitivity():
    """Включить/выключить параметры в зависимости от выбранного режима."""
    w = _widgets_for_mode
    if not w:
        return
    mode = w["var_mode"].get()
    # TRACE: Порог недоступен. PERF: Умное сжатие недоступно.
    if mode == "TRACE":
        w["entry_threshold"].config(state=tk.DISABLED)
        w["check_compact"].config(state=tk.NORMAL)
    else:
        w["entry_threshold"].config(state=tk.NORMAL)
        w["check_compact"].config(state=tk.DISABLED)


def run_parser():
    global result_text, var_out_path, btn_copy, btn_open
    path = var_file.get().strip()
    if not path:
        messagebox.showwarning("Внимание", "Выберите файл!")
        return

    if not os.path.isfile(path):
        messagebox.showerror("Ошибка", "Файл не найден.")
        return

    mode = var_mode.get()
    entry = var_entry.get().strip() or None
    try:
        thresh_str = var_threshold.get().strip()
        threshold_ms = float(thresh_str) if thresh_str else None
    except ValueError:
        messagebox.showerror("Ошибка", "Порог должен быть числом (мс).")
        return

    perf_only = mode == "PERF"
    trace_only = mode == "TRACE"
    expand = var_expand_modules.get()
    compact = var_compact.get() if mode == "TRACE" else False  # в PERF не используется
    include_model_prompt = var_include_model_prompt.get()
    highlight_extensions = var_highlight_extensions.get()

    try:
        report = process_pff(
            path,
            entry=entry,
            main_block=None,
            threshold_ms=threshold_ms,
            no_perf=trace_only,
            perf_only=perf_only,
            no_hotspots=trace_only,
            no_compact=not compact,
            no_context=False,
            expand_module_names=expand,
            include_model_prompt=include_model_prompt,
            highlight_extensions=highlight_extensions,
        )
    except Exception as e:
        messagebox.showerror("Ошибка", str(e))
        return

    if report.startswith("File not found") or report.startswith("No trace"):
        messagebox.showerror("Ошибка", report)
        return

    # Вывод в текстовое поле
    fill_result_text(result_text, report)

    out_path = get_save_path(path, mode)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
    except OSError as e:
        messagebox.showerror("Ошибка записи", str(e))
        return

    var_out_path.set(out_path)
    btn_copy.config(state=tk.NORMAL)
    btn_open.config(state=tk.NORMAL)


def choose_file():
    path = filedialog.askopenfilename(
        title="Выберите файл замера (PFF/TXT)",
        filetypes=[
            ("Файлы замеров (*.pff; *.txt)", "*.pff *.txt"),
            ("Все файлы", "*.*"),
        ],
    )
    if path:
        var_file.set(path)


def build_ui(root: tk.Tk):
    global var_file, var_mode, var_entry, var_threshold, var_expand_modules, var_highlight_extensions, var_compact, var_include_model_prompt, result_text
    global var_out_path, btn_copy, btn_open, _widgets_for_mode

    root.title("Парсер замеров производительности")
    root.minsize(520, 480)
    root.resizable(True, True)

    main = ttk.Frame(root, padding=12)
    main.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    main.columnconfigure(0, weight=1)
    main.rowconfigure(12, weight=1)
    main.rowconfigure(13, weight=0)

    # Файл
    ttk.Label(main, text="Путь к файлу:").grid(row=0, column=0, sticky=tk.W, pady=(0, 4))
    var_file = tk.StringVar()
    f_frame = ttk.Frame(main)
    f_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(0, 8))
    entry_file = ttk.Entry(f_frame, textvariable=var_file, width=50)
    entry_file.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
    ttk.Button(f_frame, text="…", command=choose_file, width=3).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(f_frame, text="×", command=lambda: var_file.set(""), width=2).pack(side=tk.LEFT)

    # Режим
    ttk.Label(main, text="Режим:").grid(row=2, column=0, sticky=tk.W, pady=(0, 4))
    var_mode = tk.StringVar(value="TRACE")
    mode_frame = ttk.Frame(main)
    mode_frame.grid(row=3, column=0, sticky=tk.W, pady=(0, 8))
    ttk.Radiobutton(mode_frame, text="Трассировка", variable=var_mode, value="TRACE", command=update_mode_sensitivity).pack(side=tk.LEFT, padx=(0, 16))
    ttk.Radiobutton(mode_frame, text="Производительность", variable=var_mode, value="PERF", command=update_mode_sensitivity).pack(side=tk.LEFT)

    # Точка входа
    ttk.Label(main, text="Точка входа:").grid(row=4, column=0, sticky=tk.W, pady=(0, 4))
    var_entry = tk.StringVar()
    entry_entry = ttk.Entry(main, textvariable=var_entry, width=50)
    entry_entry.grid(row=5, column=0, sticky=(tk.W, tk.E), pady=(0, 8))

    # Порог (для PERF)
    ttk.Label(main, text="Порог:").grid(row=6, column=0, sticky=tk.W, pady=(0, 4))
    var_threshold = tk.StringVar(value="0")
    entry_threshold = ttk.Entry(main, textvariable=var_threshold, width=12)
    entry_threshold.grid(row=7, column=0, sticky=tk.W, pady=(0, 8))

    # Чекбоксы
    var_expand_modules = tk.BooleanVar(value=True)
    check_expand = ttk.Checkbutton(main, text="Разворачивать имена модулей", variable=var_expand_modules)
    check_expand.grid(row=8, column=0, sticky=tk.W, pady=(0, 4))
    
    var_highlight_extensions = tk.BooleanVar(value=True)
    check_highlight_ext = ttk.Checkbutton(main, text="Выделять расширения [Ext:Name]", variable=var_highlight_extensions)
    check_highlight_ext.grid(row=9, column=0, sticky=tk.W, pady=(0, 4))

    var_compact = tk.BooleanVar(value=True)
    check_compact = ttk.Checkbutton(main, text="Умное сжатие", variable=var_compact)
    check_compact.grid(row=10, column=0, sticky=tk.W, pady=(0, 4))
    var_include_model_prompt = tk.BooleanVar(value=True)
    check_model_prompt = ttk.Checkbutton(main, text="Включить промпт для модели в заголовок", variable=var_include_model_prompt)
    check_model_prompt.grid(row=11, column=0, sticky=tk.W, pady=(0, 8))

    _widgets_for_mode = {
        "var_mode": var_mode,
        "entry_threshold": entry_threshold,
        "check_compact": check_compact,
    }
    update_mode_sensitivity()

    # Строка «Результат» + кнопка «Сформировать» справа (кнопка по умолчанию — Enter)
    result_header = ttk.Frame(main)
    result_header.grid(row=12, column=0, sticky=(tk.W, tk.E), pady=(0, 4))
    result_header.columnconfigure(1, weight=1)
    ttk.Label(result_header, text="Результат:").grid(row=0, column=0, sticky=tk.W)
    btn_form = ttk.Button(result_header, text="Сформировать", command=run_parser)
    btn_form.grid(row=0, column=1, sticky=tk.E)
    root.bind("<Return>", lambda e: run_parser())

    # Текстовое поле результата
    table_frame = ttk.Frame(main)
    table_frame.grid(row=12, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 0))
    table_frame.columnconfigure(0, weight=1)
    table_frame.rowconfigure(0, weight=1)

    result_text = tk.Text(table_frame, font=("Courier New", 10), wrap=tk.NONE, height=16)
    scroll_y = ttk.Scrollbar(table_frame)
    scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL)
    result_text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.configure(command=result_text.yview)
    scroll_x.configure(command=result_text.xview)
    result_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
    scroll_y.grid(row=0, column=1, sticky=(tk.N, tk.S))
    scroll_x.grid(row=1, column=0, sticky=(tk.W, tk.E))

    # Панель внизу: путь выходного файла, Копировать, Открыть
    var_out_path = tk.StringVar(value="")
    out_frame = ttk.Frame(main)
    out_frame.grid(row=13, column=0, sticky=(tk.W, tk.E), pady=(8, 0))
    out_frame.columnconfigure(1, weight=1)
    ttk.Label(out_frame, text="Выходной файл:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
    entry_out = ttk.Entry(out_frame, textvariable=var_out_path, state=tk.DISABLED)
    entry_out.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(0, 6))

    def copy_out_path():
        path = var_out_path.get().strip()
        if path:
            root.clipboard_clear()
            root.clipboard_append(path)
            root.update()

    def open_out_path():
        path = var_out_path.get().strip()
        if path and os.path.isfile(path):
            if sys.platform == "win32":
                os.startfile(path)
            else:
                import subprocess
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.run([opener, path], check=False)

    btn_copy = ttk.Button(out_frame, text="Копировать", command=copy_out_path, state=tk.DISABLED)
    btn_copy.grid(row=0, column=2, padx=(0, 4))
    btn_open = ttk.Button(out_frame, text="Открыть", command=open_out_path, state=tk.DISABLED)
    btn_open.grid(row=0, column=3)


def main():
    root = tk.Tk()
    build_ui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
