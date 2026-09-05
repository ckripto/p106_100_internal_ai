"""Run manually: venv/bin/python tests/browser_smoke.py (Chromium required)."""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from waitress import create_server
from web_service import Worker, create_app


def main():
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Path(directory) / 'db.sqlite3')
        def runner(prompt, history, on_progress, on_message):
            on_progress('Проверка фонового выполнения')
            on_message({'attempt':1,'sender':'coordinator','recipient':'executor',
                        'kind':'request','content':prompt,'created':time.time(),
                        'response_seconds':None})
            time.sleep(2)
            on_message({'attempt':1,'sender':'executor','recipient':'coordinator',
                        'kind':'response','content':'{"status":"success","summary":"готово"}',
                        'created':time.time(),'response_seconds':2.0})
            return {'type':'final', 'status':'success', 'summary':'Задача выполнена: ' + prompt}
        worker = Worker(app.extensions['store'], runner)
        app.extensions['worker'] = worker
        threading.Thread(target=worker.run, daemon=True).start()
        server = create_server(app, host='localhost', port=0)
        threading.Thread(target=server.run, daemon=True).start()
        url = f'http://localhost:{server.effective_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url)
                page.get_by_role('button', name='Открыть сессии').click()
                page.get_by_role('button', name='Новая сессия').click()
                expect(page.locator('#prompt')).to_be_enabled()
                page.locator('#prompt').fill('Покажи <script>alert(1)</script> как текст')
                page.get_by_role('button', name='Отправить').click()
                expect(page.locator('.prompt-bubble')).to_have_count(1)
                sid = page.evaluate("localStorage.getItem('session')")
                page.close()  # Work must finish while no tab is open.
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url)
                expect(page.locator('.badge.success')).to_have_count(1, timeout=15000)
                expect(page.locator('.answer-text')).to_contain_text('<script>alert(1)</script>')
                expect(page.locator('.agent-messages')).to_have_count(1)
                page.locator('.agent-messages summary').click()
                expect(page.locator('.agent-messages')).to_contain_text('Ответ за 2.00 с')
                expect(page.locator('.agent-messages')).to_contain_text('Координатор')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path='/tmp/agents-mobile.png', full_page=True)
                page.locator('#prompt').fill('Уточнение в той же сессии')
                page.get_by_role('button', name='Отправить').click()
                expect(page.locator('.badge.success')).to_have_count(2, timeout=15000)
                assert page.evaluate("localStorage.getItem('session')") == sid
                page.reload()
                expect(page.locator('.prompt-bubble')).to_have_count(2)
                page.set_viewport_size({'width':1280,'height':900})
                page.screenshot(path='/tmp/agents-desktop.png', full_page=True)
                page.on('dialog', lambda d: d.accept())
                page.get_by_role('button', name='Удалить сессию').click()
                expect(page.locator('.prompt-bubble')).to_have_count(0)
                assert app.extensions['store'].sessions() == []
                assert not errors, errors
                browser.close()
                print('Browser OK: mobile layout, background task, follow-up, reload, safe text, deletion')
        finally:
            worker.stop.set()
            worker.wake.set()
            server.close()


if __name__ == '__main__':
    main()
