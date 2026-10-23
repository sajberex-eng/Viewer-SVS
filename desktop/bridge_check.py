"""Проверка моста pywebview на CI: python desktop/bridge_check.py <адрес страницы программы> <отчёт>.

Открывает окно pywebview на странице работающей программы и спрашивает у страницы, есть ли
window.pywebview.api.pick_folder. Именно этот мост ломал CSP на macOS (WKWebView запрещает
new Function без 'unsafe-eval'), и «Добавить папку» показывало ввод пути вручную вместо
системного окна (2026-10-23). Из обычных тестов это не видно: там нет окна.
Код возврата 0 — мост есть.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path


class Api:
    def pick_folder(self):
        return None


def main(url: str, report: Path) -> int:
    import webview

    outcome: dict[str, str] = {}

    def probe(window) -> None:
        try:
            window.events.loaded.wait(30)
            # Мост внедряется после загрузки страницы в отдельном потоке — даём ему время
            found = ""
            for _ in range(50):
                found = window.evaluate_js(
                    "typeof (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder)")
                if found == "function":
                    break
                threading.Event().wait(0.2)
            outcome["result"] = str(found)
            outcome["csp"] = str(window.evaluate_js(
                "(document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]') || {}).content || '(в заголовке)'"))
        except Exception as exc:
            outcome["result"] = f"ошибка: {type(exc).__name__}: {exc}"
        finally:
            window.destroy()

    window = webview.create_window("bridge-check", url, js_api=Api(), width=640, height=480)
    webview.start(probe, window)
    result = outcome.get("result", "окно не ответило")
    ok = result == "function"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"pywebview.api.pick_folder: {result}\n{'[OK]' if ok else '[СБОЙ]'} мост pywebview\n",
                      encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
