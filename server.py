#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Офлайн-контрольная по Python с авторизацией и загрузкой кода.
Студенты входят по логину/паролю, пишут код, сервер прогоняет тесты.
Зависимостей нет — стандартная библиотека Python 3.7+.
"""
import http.server
import socketserver
import json
import os
import re
import socket
import sys
import subprocess
import tempfile
import time
import threading
import secrets
import csv
import io
import random
from urllib.parse import parse_qs, urlparse
from datetime import datetime

# ============ НАСТРОЙКИ ============
PORT = 8080
ADMIN_PASSWORD = "teacher2024"           # ← СМЕНИТЬ!
QUESTIONS_DIR = "questions"
FALLBACK_FILE = "questions.json"
STUDENTS_FILE = "students.json"
STUDENTS_CSV = "students.csv"
DEFAULT_DURATION_MIN = 5 #60
RESULTS_FILE = "results.csv"
SUBMISSIONS_DIR = "submissions"
CODE_TIMEOUT = 5
MAX_CODE_SIZE = 100_000
REQUIRE_ADMIN_START = True               # True = ждать кнопку «Начать экзамен»
# ==================================

lock = threading.RLock()
file_lock = threading.Lock()
sessions = {}
submitted = []
admin_tokens = set()
VARIANTS = []
STUDENTS = {}
exam_started = not REQUIRE_ADMIN_START


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def esc(s):
    if s is None:
        return ''
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ============ ЗАГРУЗКА ВАРИАНТОВ ============
def _parse_variant(data, source_name):
    if isinstance(data, list):
        tasks, name, duration = data, os.path.splitext(source_name)[0], DEFAULT_DURATION_MIN
    elif isinstance(data, dict):
        tasks = data.get('tasks') or data.get('questions') or []
        name = data.get('name') or os.path.splitext(source_name)[0]
        duration = int(data.get('duration_min', DEFAULT_DURATION_MIN))
    else:
        raise ValueError("ожидается список задач или объект {tasks:[...]}")
    if not tasks:
        raise ValueError("нет ни одной задачи")
    norm = []
    for i, t in enumerate(tasks):
        title = t.get('title') or f"Задача {i+1}"
        desc = t.get('description') or t.get('text') or ''
        examples = t.get('examples') or []
        tests = t.get('tests') or t.get('test_cases') or []
        if not tests:
            raise ValueError(f"задача #{i+1}: пустой список 'tests'")
        for j, tc in enumerate(tests):
            if 'input' not in tc or 'output' not in tc:
                raise ValueError(f"задача #{i+1} тест #{j+1}: нужны 'input' и 'output'")
        norm.append({
            'title': str(title),
            'description': str(desc),
            'examples': [{'input': str(e.get('input', '')),
                          'output': str(e.get('output', ''))} for e in examples],
            'tests': [{'input': str(tc['input']),
                       'output': str(tc['output'])} for tc in tests],
        })
    return {'name': str(name), 'duration_min': duration,
            'tasks': norm, 'source': source_name}


def load_variants():
    global VARIANTS
    result = []
    if os.path.isdir(QUESTIONS_DIR):
        for fname in sorted(os.listdir(QUESTIONS_DIR)):
            if not fname.lower().endswith('.json'):
                continue
            try:
                with open(os.path.join(QUESTIONS_DIR, fname), 'r', encoding='utf-8') as f:
                    v = _parse_variant(json.load(f), fname)
                result.append(v)
                print(f"  ✓ {fname:30s} → «{v['name']}», "
                      f"{len(v['tasks'])} задач, {v['duration_min']} мин")
            except Exception as e:
                print(f"  ✗ {fname}: {e}")
    if not result and os.path.isfile(FALLBACK_FILE):
        try:
            with open(FALLBACK_FILE, 'r', encoding='utf-8') as f:
                v = _parse_variant(json.load(f), FALLBACK_FILE)
            result.append(v)
            print(f"  ✓ {FALLBACK_FILE:30s} → «{v['name']}», "
                  f"{len(v['tasks'])} задач, {v['duration_min']} мин")
        except Exception as e:
            print(f"  ✗ {FALLBACK_FILE}: {e}")
    VARIANTS = result


# ============ ЗАГРУЗКА СТУДЕНТОВ ============
def load_students():
    global STUDENTS
    result = {}
    if os.path.isfile(STUDENTS_FILE):
        try:
            with open(STUDENTS_FILE, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            for row in data:
                login = str(row.get('login', '')).strip().lower()
                if not login:
                    continue
                result[login] = {
                    'password': str(row.get('password', '')),
                    'name': str(row.get('name', login)),
                    'group': str(row.get('group', '')),
                }
        except Exception as e:
            print(f"  ✗ {STUDENTS_FILE}: {e}")
    if not result and os.path.isfile(STUDENTS_CSV):
        try:
            with open(STUDENTS_CSV, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f, delimiter=';')
                for row in reader:
                    login = (row.get('login') or '').strip().lower()
                    if not login:
                        continue
                    result[login] = {
                        'password': (row.get('password') or '').strip(),
                        'name': (row.get('name') or login).strip(),
                        'group': (row.get('group') or '').strip(),
                    }
        except Exception as e:
            print(f"  ✗ {STUDENTS_CSV}: {e}")
    STUDENTS = result
    return result


# ============ ЗАПУСК КОДА СТУДЕНТА ============
def run_code(code, stdin_str, timeout=CODE_TIMEOUT):
    with tempfile.TemporaryDirectory() as td:
        script_path = os.path.join(td, 'solution.py')
        try:
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(code)
        except Exception as e:
            return '', f'Не удалось сохранить код: {e}', -1
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        try:
            r = subprocess.run(
                [sys.executable, '-s', '-B', script_path],
                input=stdin_str, capture_output=True, text=True,
                timeout=timeout, cwd=td, env=env,
                encoding='utf-8', errors='replace',
            )
            return r.stdout or '', r.stderr or '', r.returncode
        except subprocess.TimeoutExpired:
            return '', f'Превышено время (>{timeout} сек)', -1
        except Exception as e:
            return '', f'Ошибка запуска: {e}', -1


def outputs_match(actual, expected):
    def norm(s):
        s = s.replace('\r\n', '\n').replace('\r', '\n')
        lines = [ln.rstrip() for ln in s.split('\n')]
        while lines and not lines[-1]:
            lines.pop()
        return '\n'.join(lines)
    return norm(actual) == norm(expected)


def evaluate_task(code, tests):
    if not code or not code.strip():
        return 0, len(tests)
    passed = 0
    for t in tests:
        stdout, _, rc = run_code(code, t.get('input', ''))
        if rc == 0 and outputs_match(stdout, t.get('output', '')):
            passed += 1
    return passed, len(tests)


def evaluate_all(sess):
    v = VARIANTS[sess['variant_idx']]
    scores = {}
    tp = tm = 0
    for i, task in enumerate(v['tasks']):
        p, t = evaluate_task(sess['codes'].get(str(i), ''), task['tests'])
        scores[i] = (p, t)
        tp += p
        tm += t
    return {'scores': scores, 'total_passed': tp, 'total_max': tm}


def save_submission_files(sess, variant):
    safe = lambda s: re.sub(r'[^\w\-]+', '_', s, flags=re.UNICODE).strip('_')[:60] or 'anon'
    d = os.path.join(SUBMISSIONS_DIR, f"{safe(sess['group'])}_{safe(sess['name'])}")
    try:
        os.makedirs(d, exist_ok=True)
        for i, task in enumerate(variant['tasks']):
            code = sess['codes'].get(str(i), '')
            if not code.strip():
                continue
            with open(os.path.join(d, f"task_{i+1}.py"), 'w', encoding='utf-8') as f:
                f.write(f"# {sess['name']} ({sess['group']}) — {variant['name']} — Задача {i+1}\n")
                f.write(f"# {task['title']}\n\n{code}")
    except Exception as e:
        print(f"save_submission_files: {e}", flush=True)


# ============ СЕССИИ ============
def append_result_to_file(result, variant):
    with file_lock:
        try:
            new_file = (not os.path.exists(RESULTS_FILE)) or os.path.getsize(RESULTS_FILE) == 0
            enc = 'utf-8-sig' if new_file else 'utf-8'
            with open(RESULTS_FILE, 'a', newline='', encoding=enc) as f:
                w = csv.writer(f, delimiter=';')
                if new_file:
                    w.writerow(['Логин', 'Имя', 'Группа', 'Вариант', 'IP',
                                'Начало', 'Конец', 'Балл', 'Максимум', 'Процент',
                                'Детали по задачам'])
                pct = (round(100 * result['total_passed'] / result['total_max'])
                       if result['total_max'] else 0)
                details = ', '.join(f"З{i+1}: {p}/{t}"
                                    for i, (p, t) in sorted(result['scores'].items()))
                w.writerow([
                    result['login'], result['name'], result['group'],
                    variant['name'], result['ip'],
                    datetime.fromtimestamp(result['start_time']).strftime('%Y-%m-%d %H:%M:%S'),
                    datetime.fromtimestamp(result['end_time']).strftime('%Y-%m-%d %H:%M:%S'),
                    result['total_passed'], result['total_max'], pct, details,
                ])
        except Exception as e:
            print(f"CSV error: {e}", flush=True)


def finish_session(sid):
    with lock:
        sess = sessions.get(sid)
        if not sess or sess['status'] != 'running':
            return
        v = VARIANTS[sess['variant_idx']]
        log(f"Финализация: {sess['name']} ({sess['group']}) — тесты...")
        ev = evaluate_all(sess)
        sess['status'] = 'submitted'
        sess['submitted_at'] = time.time()
        sess['scores'] = ev['scores']
        sess['total_passed'] = ev['total_passed']
        sess['total_max'] = ev['total_max']
        result = {
            'login': sess['login'],
            'name': sess['name'], 'group': sess['group'],
            'variant': v['name'],
            'scores': dict(ev['scores']),
            'total_passed': ev['total_passed'], 'total_max': ev['total_max'],
            'start_time': sess['start_time'], 'end_time': sess['submitted_at'],
            'ip': sess['ip'],
        }
        submitted.append(result)
    try:
        save_submission_files(sess, v)
        append_result_to_file(result, v)
    except Exception as e:
        print(f"CSV/save error: {e}", flush=True)
    log(f"Сдано: {result['name']} — {result['total_passed']}/{result['total_max']}")


def auto_finish_loop():
    while True:
        time.sleep(10)
        now = time.time()
        to_finish = []
        with lock:
            for sid, s in sessions.items():
                if s['status'] == 'running' and now - s['start_time'] >= s['duration_sec']:
                    to_finish.append(sid)
        for sid in to_finish:
            try:
                finish_session(sid)
            except Exception as e:
                print(f"auto_finish: {e}", flush=True)


# ============ HTML / СТИЛИ ============
STYLE = """<style>
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#222;
     background:#fff;max-width:900px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;font-weight:600;margin:0 0 16px}
h2{font-size:15px;font-weight:600;margin:0 0 8px}
a{color:#06c;text-decoration:none}a:hover{text-decoration:underline}
code{background:#f4f4f4;padding:1px 5px;border-radius:3px;font-size:13px}
pre{font-family:Consolas,"Courier New",monospace;font-size:13px;margin:0;
    white-space:pre-wrap;word-wrap:break-word}

input[type=text],input[type=password],textarea{font:inherit;width:100%;padding:8px 10px;
    border:1px solid #ddd;border-radius:3px;background:#fff;color:inherit}
input:focus,textarea:focus{outline:none;border-color:#06c}
textarea{min-height:160px;font-family:Consolas,"Courier New",monospace;font-size:13px;
         resize:vertical;margin-top:8px}

button{font:inherit;padding:8px 16px;border:1px solid #06c;background:#06c;color:#fff;
       border-radius:3px;cursor:pointer}
button:hover{background:#05a}
button.secondary{background:#fff;color:#06c}
button.secondary:hover{background:#f0f6ff}
button.danger{background:#fff;color:#c00;border-color:#c00;padding:5px 10px;font-size:12px}
button.danger:hover{background:#fff5f5}

.task{padding:20px 0;border-top:1px solid #eee}
.task:first-of-type{border-top:none;padding-top:0}
.desc{margin:0 0 12px;color:#333}
.example{background:#f8f8f8;padding:10px 12px;margin:8px 0;border-radius:3px;font-size:13px}
.example b{font-weight:600}

.msg{padding:10px 12px;border-radius:3px;margin:12px 0;border:1px solid}
.msg.error{background:#fff5f5;color:#c00;border-color:#fcc}
.msg.info{background:#f0f6ff;color:#048;border-color:#cee}

.ok{color:#080}.warn{color:#c80}.bad{color:#c00}.muted{color:#888}

table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #eee;vertical-align:top}
th{font-weight:600;color:#555;background:#fafafa}

.timer{position:sticky;top:0;background:#fff;padding:10px 0;margin-bottom:16px;
       font-size:22px;font-weight:600;text-align:right;border-bottom:1px solid #eee;
       z-index:10;font-variant-numeric:tabular-nums}
.timer.warn{color:#c80}.timer.danger{color:#c00}

.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.mt{margin-top:16px}
.check-result{margin-left:8px;font-size:13px}
.file-load{font-size:13px;color:#666;margin:8px 0}
.file-load input{font-size:13px}

.login-form{max-width:340px}
.login-form input{margin-bottom:10px}

.lobby{padding:60px 0;text-align:center}
.lobby .spinner{display:inline-block;width:24px;height:24px;
    border:3px solid #eee;border-top-color:#06c;border-radius:50%;
    animation:spin 1s linear infinite;margin:12px 0}
@keyframes spin{to{transform:rotate(360deg)}}

.submit-bar{position:sticky;bottom:0;background:#fff;padding:12px 0;
            border-top:1px solid #eee;text-align:center;margin-top:20px}
</style>"""


def login_page(error=''):
    err = f'<div class="msg error">{esc(error)}</div>' if error else ''
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Вход</title>{STYLE}</head><body>
<h1>Контрольная работа по Python</h1>
<form method="POST" action="/login" class="login-form">
{err}
<input type="text" name="login" placeholder="Логин" required autofocus autocomplete="off">
<input type="password" name="password" placeholder="Пароль" required>
<button>Войти</button>
</form>
<p class="muted mt">Логин и пароль выдаёт преподаватель.</p>
</body></html>"""


def lobby_page(sess):
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Ожидание</title>{STYLE}<meta http-equiv="refresh" content="3"></head><body>
<div class="lobby">
<h1>Ожидание старта</h1>
<p>{esc(sess['name'])} <span class="muted">· {esc(sess['group'])}</span></p>
<div class="spinner"></div>
<p class="muted">Преподаватель скоро начнёт. Страница обновится автоматически.</p>
</div></body></html>"""


def exam_page(sess, remaining):
    v = VARIANTS[sess['variant_idx']]
    qhtml = []
    for i, task in enumerate(v['tasks']):
        examples = ''
        for k, ex in enumerate(task['examples'], 1):
            examples += (f'<div class="example"><b>Пример {k}</b><br>'
                         f'вход: <pre>{esc(ex["input"])}</pre>'
                         f'выход: <pre>{esc(ex["output"])}</pre></div>')
        saved = sess['codes'].get(str(i), '')
        qhtml.append(f'''
<div class="task">
  <h2>Задача {i+1}. {esc(task["title"])}</h2>
  <p class="desc">{task["description"]}</p>
  {examples}
  <div class="file-load">Файл: <input type="file" accept=".py,.txt"
       onchange="loadFile(this,'code_{i}')"></div>
  <textarea id="code_{i}" data-task="{i}" name="task_{i}"
    placeholder="# ваш код на Python...">{esc(saved)}</textarea>
  <div class="row mt">
    <button type="button" class="secondary" onclick="checkTask({i})">Проверить</button>
    <span id="check_result_{i}" class="check-result"></span>
  </div>
</div>''')

    js = """
    let remaining = __REMAINING__;
    const timerEl = document.getElementById('timer');
    const formEl  = document.getElementById('f');
    function tick(){
        if (remaining < 0) remaining = 0;
        const m = Math.floor(remaining/60), s = remaining % 60;
        timerEl.textContent = String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
        if (remaining <= 300) timerEl.classList.add('warn');
        if (remaining <= 60)  timerEl.classList.add('danger');
        if (remaining <= 0) { formEl.submit(); return; }
        remaining--;
    }
    tick(); setInterval(tick, 1000);
    setInterval(function(){
        const data = {};
        document.querySelectorAll('textarea[data-task]').forEach(function(ta){
            data[ta.dataset.task] = ta.value;
        });
        fetch('/autosave',{method:'POST',body:JSON.stringify(data)}).catch(function(){});
    }, 15000);
    function loadFile(input, targetId){
        const f = input.files[0]; if (!f) return;
        const r = new FileReader();
        r.onload = function(e){ document.getElementById(targetId).value = e.target.result; };
        r.readAsText(f);
    }
    async function checkTask(idx){
        const code = document.getElementById('code_'+idx).value;
        const el = document.getElementById('check_result_'+idx);
        el.textContent = 'проверяю…'; el.className = 'check-result muted';
        try {
            const r = await fetch('/check', {method:'POST',
                body: JSON.stringify({task_idx: idx, code: code})});
            const res = await r.json();
            if (res.error) { el.textContent = res.error; el.className = 'check-result bad'; }
            else {
                el.textContent = 'Пройдено: ' + res.passed + ' / ' + res.total;
                el.className = 'check-result ' +
                    (res.passed === res.total ? 'ok' : 'warn');
            }
        } catch(e){ el.textContent = 'Ошибка'; el.className = 'check-result bad'; }
    }
    """.replace('__REMAINING__', str(int(remaining)))

    head = (f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            f'<title>Экзамен</title>{STYLE}</head><body>'
            f'<div class="timer" id="timer">--:--</div>')
    title = (f'<h1>{esc(sess["name"])} '
             f'<span class="muted" style="font-weight:normal;font-size:14px">'
             f'· {esc(sess["group"])} · вариант «{esc(v["name"])}»</span></h1>')
    form_open = ('<form method="POST" action="/submit" id="f" '
                 'onsubmit="return confirm(\'Сдать работу? После этого изменить нельзя.\')">')
    submit_bar = '<div class="submit-bar"><button>Сдать работу</button></div>'
    tail = f'</form><script>{js}</script></body></html>'
    return head + title + form_open + ''.join(qhtml) + submit_bar + tail


def done_page(sess):
    v = VARIANTS[sess['variant_idx']]
    scores = sess.get('scores', {})
    tp, tm = sess.get('total_passed', 0), sess.get('total_max', 0)
    pct = round(100 * tp / tm) if tm else 0
    rows = ''
    for i, task in enumerate(v['tasks']):
        p, t = scores.get(i, (0, 0))
        cls = 'ok' if p == t else ('warn' if p > 0 else 'bad')
        rows += (f'<tr><td>{i+1}. {esc(task["title"])}</td>'
                 f'<td class="{cls}">{p} / {t}</td></tr>')
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Готово</title>{STYLE}</head><body>
<h1>Работа сдана</h1>
<p>{esc(sess['name'])} <span class="muted">· {esc(sess['group'])} · вариант «{esc(v['name'])}»</span></p>
<p class="mt">Результат: <b>{tp} / {tm}</b> ({pct}%)</p>
<table><tr><th>Задача</th><th>Пройдено</th></tr>{rows}</table>
<p class="muted mt">Окно можно закрыть.</p>
</body></html>"""


def admin_login_page(error=''):
    err = f'<div class="msg error">{esc(error)}</div>' if error else ''
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Админ</title>{STYLE}</head><body>
<h1>Вход для преподавателя</h1>
<form method="POST" action="/admin" class="login-form">
{err}
<input type="password" name="password" placeholder="Пароль" required autofocus>
<button>Войти</button>
</form></body></html>"""


def admin_panel():
    with lock:
        active_sessions = {s['login']: (sid, s) for sid, s in sessions.items()
                           if s['status'] != 'submitted'}
        done_by_login = {r['login']: r for r in submitted}

    total = len(STUDENTS)
    n_wait = sum(1 for _, s in active_sessions.values() if s['status'] == 'waiting')
    n_run = sum(1 for _, s in active_sessions.values() if s['status'] == 'running')
    n_done = len(done_by_login)
    n_off = total - len(active_sessions) - n_done

    if not exam_started:
        start_btn = (f'<form method="POST" action="/admin/start" style="display:inline">'
                     f'<button onclick="return confirm(\'Начать экзамен для всех?\')">'
                     f'Начать экзамен ({n_wait})</button></form>')
    else:
        start_btn = '<span class="ok">экзамен запущен</span>'

    rows = []
    for login, st in sorted(STUDENTS.items()):
        if login in done_by_login:
            r = done_by_login[login]
            pct = round(100 * r['total_passed'] / r['total_max']) if r['total_max'] else 0
            cls = 'ok' if pct >= 70 else ('warn' if pct >= 40 else 'bad')
            status = '<span class="bad">сдал</span>'
            remain = '—'
            score = f'<span class="{cls}">{r["total_passed"]}/{r["total_max"]} ({pct}%)</span>'
            action = ''
        elif login in active_sessions:
            sid, s = active_sessions[login]
            if s['status'] == 'waiting':
                status = '<span class="warn">ожидает</span>'
                remain, score = '—', '—'
            else:
                rr = max(0, int(s['duration_sec'] - (time.time() - s['start_time'])))
                status = '<span>работает</span>'
                remain = f'{rr//60}:{rr%60:02d}'
                score = '—'
            action = (f'<form method="POST" action="/admin/reset" style="display:inline" '
                      f'onsubmit="return confirm(\'Сбросить сессию {esc(login)}?\')">'
                      f'<input type="hidden" name="login" value="{esc(login)}">'
                      f'<button class="danger">сброс</button></form>')
        else:
            status = '<span class="muted">не зашёл</span>'
            remain, score, action = '—', '—', ''

        rows.append(
            f'<tr><td><code>{esc(login)}</code></td>'
            f'<td>{esc(st["name"])}</td>'
            f'<td>{esc(st["group"])}</td>'
            f'<td>{status}</td><td>{remain}</td>'
            f'<td>{score}</td><td>{action}</td></tr>'
        )
    rows_html = ''.join(rows) or '<tr><td colspan="7" class="muted">нет студентов</td></tr>'

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Панель преподавателя</title>{STYLE}
<meta http-equiv="refresh" content="10"></head><body>
<h1>Панель преподавателя</h1>
<p>Всего: <b>{total}</b> · ожидают: <b>{n_wait}</b> ·
   работают: <b>{n_run}</b> · сдали: <b>{n_done}</b> · не зашли: <b>{n_off}</b></p>
<div class="row mt">
{start_btn}
<a href="/export">скачать CSV</a>
<a href="/api/results">JSON</a>
</div>
<table class="mt">
<tr><th>Логин</th><th>Имя</th><th>Группа</th>
<th>Статус</th><th>Осталось</th><th>Балл</th><th></th></tr>
{rows_html}
</table>
<p class="muted mt">Результаты: <code>{RESULTS_FILE}</code> ·
   Коды: <code>{SUBMISSIONS_DIR}/</code></p>
</body></html>"""


# ============ HTTP HANDLER ============
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _cookie(self, name):
        for part in self.headers.get('Cookie', '').split(';'):
            part = part.strip()
            if part.startswith(name + '='):
                return part[len(name) + 1:]
        return None

    def _send(self, content, ctype='text/html; charset=utf-8', status=200, headers=None):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        if headers:
            for k, v in headers:
                self.send_header(k, v)
        self.end_headers()
        if content:
            self.wfile.write(content)

    def _redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header('Location', location)
        if headers:
            for k, v in headers:
                self.send_header(k, v)
        self.end_headers()

    def _session(self):
        sid = self._cookie('sid')
        if not sid:
            return None, None
        with lock:
            return sid, sessions.get(sid)

    def _is_admin(self):
        return self._cookie('admin') in admin_tokens

    def _read_body(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        return self.rfile.read(n).decode('utf-8', errors='replace') if n else ''

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/':              self.h_index()
            elif path == '/exam':        self.h_exam()
            elif path == '/done':        self.h_done()
            elif path == '/admin':       self.h_admin_get()
            elif path == '/api/results': self.h_api_results()
            elif path == '/export':      self.h_export()
            elif path == '/logout':      self.h_logout()
            elif path == '/favicon.ico':
                self.send_response(204); self.end_headers()
            else:                        self._send("Not found", status=404)
        except Exception as e:
            print(f"ERROR GET {path}: {e}", flush=True)
            try: self._send("Server error", status=500)
            except: pass

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_body()
            if path == '/autosave':      self.h_autosave(body); return
            if path == '/check':         self.h_check(body); return
            data = parse_qs(body)
            if path == '/login':          self.h_login(data)
            elif path == '/submit':       self.h_submit(data)
            elif path == '/admin':        self.h_admin_post(data)
            elif path == '/admin/start':  self.h_admin_start()
            elif path == '/admin/reset':  self.h_admin_reset(data)
            else:                         self._send("Not found", status=404)
        except Exception as e:
            print(f"ERROR POST {path}: {e}", flush=True)
            try: self._send("Server error", status=500)
            except: pass

    # ---------- обработчики ----------
    def h_index(self):
        if not VARIANTS:
            self._send("<h1>Ошибка</h1><p>Не загружено ни одного варианта.</p>",
                       status=500); return
        if not STUDENTS:
            self._send("<h1>Ошибка</h1><p>Не загружен список студентов "
                       "(<code>students.json</code>).</p>", status=500); return
        sid, sess = self._session()
        if not sess:
            self._send(login_page()); return
        if sess['status'] == 'submitted':
            self._redirect('/done'); return
        if sess['status'] == 'running' and time.time() - sess['start_time'] >= sess['duration_sec']:
            finish_session(sid); self._redirect('/done'); return
        self._redirect('/exam')

    def h_login(self, data):
        login = (data.get('login', [''])[0] or '').strip().lower()
        password = (data.get('password', [''])[0] or '')
        student = STUDENTS.get(login)
        if not student or student['password'] != password:
            log(f"Неудачный вход: {login} с {self.client_address[0]}")
            self._send(login_page("Неверный логин или пароль")); return

        ip = self.client_address[0]
        now = time.time()
        sid_ = None
        action = None
        with lock:
            existing_sid = None
            for sid_i, s in sessions.items():
                if s['login'] == login:
                    existing_sid = sid_i
                    break
            if existing_sid:
                s = sessions[existing_sid]
                if s['status'] == 'submitted':
                    sid_ = existing_sid
                    action = 'already-done'
                elif s['ip'] != ip:
                    self._send(login_page(
                        f"Логин «{login}» уже используется с другого компьютера "
                        f"({s['ip']}). Попросите преподавателя сбросить сессию."))
                    return
                else:
                    sid_ = existing_sid
                    action = 'resume'
            else:
                v_idx = random.randrange(len(VARIANTS))
                v = VARIANTS[v_idx]
                sid_ = secrets.token_urlsafe(16)
                status = 'running' if exam_started else 'waiting'
                sessions[sid_] = {
                    'login': login,
                    'name': student['name'], 'group': student['group'],
                    'variant_idx': v_idx,
                    'duration_sec': v['duration_min'] * 60,
                    'start_time': now if status == 'running' else None,
                    'last_seen': now,
                    'ip': ip, 'codes': {},
                    'status': status,
                    'submitted_at': None,
                    'scores': {}, 'total_passed': 0, 'total_max': 0,
                }
                action = 'new'

        log(f"Вход: {login} ({student['name']}) с {ip} [{action}]")
        cookie = [('Set-Cookie', f'sid={sid_}; Path=/; HttpOnly; SameSite=Lax')]
        if action == 'already-done':
            self._redirect('/done', cookie)
        else:
            self._redirect('/exam', cookie)

    def h_logout(self):
        self._redirect('/', [('Set-Cookie', 'sid=; Path=/; Max-Age=0; HttpOnly')])

    def h_exam(self):
        sid, sess = self._session()
        if not sess:
            self._redirect('/'); return
        if sess['status'] == 'submitted':
            self._redirect('/done'); return
        if sess['status'] == 'waiting':
            self._send(lobby_page(sess)); return
        remaining = sess['duration_sec'] - (time.time() - sess['start_time'])
        if remaining <= 0:
            finish_session(sid); self._redirect('/done'); return
        with lock:
            sess['last_seen'] = time.time()
        self._send(exam_page(sess, remaining))

    def h_submit(self, data):
        sid, sess = self._session()
        if not sess:
            self._redirect('/'); return
        with lock:
            if sess['status'] == 'submitted':
                self._redirect('/done'); return
            if sess['status'] == 'waiting':
                self._redirect('/exam'); return
            v = VARIANTS[sess['variant_idx']]
            for i in range(len(v['tasks'])):
                code = (data.get(f'task_{i}', [''])[0] or '')
                if code and len(code) <= MAX_CODE_SIZE:
                    sess['codes'][str(i)] = code
        finish_session(sid)
        self._redirect('/done')

    def h_done(self):
        sid, sess = self._session()
        if not sess:
            self._redirect('/'); return
        if sess['status'] == 'running' and \
                time.time() - sess['start_time'] >= sess['duration_sec']:
            finish_session(sid)
        if sess['status'] != 'submitted':
            self._redirect('/exam'); return
        self._send(done_page(sess))

    def h_autosave(self, body):
        sid, sess = self._session()
        if not sess or sess['status'] != 'running':
            self._send('', status=200); return
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        with lock:
            if sess['status'] == 'running':
                for k, v in data.items():
                    if str(k).isdigit() and isinstance(v, str) and len(v) <= MAX_CODE_SIZE:
                        sess['codes'][str(k)] = v
                sess['last_seen'] = time.time()
        self._send('', status=200)

    def h_check(self, body):
        sid, sess = self._session()
        if not sess or sess['status'] != 'running':
            self._send(json.dumps({'error': 'сессия недействительна'}),
                       ctype='application/json; charset=utf-8'); return
        try:
            data = json.loads(body) if body else {}
            idx = int(data.get('task_idx', -1))
            code = data.get('code', '')
        except Exception:
            self._send(json.dumps({'error': 'неверный запрос'}),
                       ctype='application/json; charset=utf-8'); return
        v = VARIANTS[sess['variant_idx']]
        if not (0 <= idx < len(v['tasks'])):
            self._send(json.dumps({'error': 'нет такой задачи'}),
                       ctype='application/json; charset=utf-8'); return
        if not isinstance(code, str) or len(code) > MAX_CODE_SIZE:
            self._send(json.dumps({'error': 'код слишком длинный'}),
                       ctype='application/json; charset=utf-8'); return
        with lock:
            if sess['status'] == 'running':
                sess['codes'][str(idx)] = code
        try:
            passed, total = evaluate_task(code, v['tasks'][idx]['tests'])
        except Exception as e:
            self._send(json.dumps({'error': f'ошибка: {e}'}),
                       ctype='application/json; charset=utf-8'); return
        self._send(json.dumps({'passed': passed, 'total': total}),
                   ctype='application/json; charset=utf-8')

    def h_admin_get(self):
        if not self._is_admin():
            self._send(admin_login_page()); return
        self._send(admin_panel())

    def h_admin_post(self, data):
        pwd = (data.get('password', [''])[0] or '')
        if pwd == ADMIN_PASSWORD:
            tok = secrets.token_urlsafe(24)
            admin_tokens.add(tok)
            self._redirect('/admin', [('Set-Cookie',
                                       f'admin={tok}; Path=/; HttpOnly; SameSite=Lax')])
        else:
            self._send(admin_login_page("Неверный пароль"), status=401)

    def h_admin_start(self):
        global exam_started
        if not self._is_admin():
            self._send("Forbidden", status=403); return
        with lock:
            exam_started = True
            now = time.time()
            n = 0
            for s in sessions.values():
                if s['status'] == 'waiting':
                    s['status'] = 'running'
                    s['start_time'] = now
                    n += 1
        log(f"ЭКЗАМЕН ЗАПУЩЕН. Студентов стартовало: {n}")
        self._redirect('/admin')

    def h_admin_reset(self, data):
        if not self._is_admin():
            self._send("Forbidden", status=403); return
        login = (data.get('login', [''])[0] or '').strip().lower()
        with lock:
            for sid_, s in list(sessions.items()):
                if s['login'] == login:
                    del sessions[sid_]
                    log(f"Сессия сброшена: {login}")
                    break
        self._redirect('/admin')

    def h_api_results(self):
        if not self._is_admin():
            self._send("Forbidden", status=403); return
        with lock:
            active = []
            for s in sessions.values():
                if s['status'] == 'submitted':
                    continue
                v = VARIANTS[s['variant_idx']]
                item = {'login': s['login'], 'name': s['name'], 'group': s['group'],
                        'variant': v['name'], 'ip': s['ip'], 'status': s['status']}
                if s['start_time']:
                    item['elapsed'] = int(time.time() - s['start_time'])
                active.append(item)
            done = [{'login': r['login'], 'name': r['name'], 'group': r['group'],
                     'variant': r['variant'],
                     'total_passed': r['total_passed'], 'total_max': r['total_max'],
                     'scores': {str(k): v for k, v in r['scores'].items()},
                     'end_time': r['end_time']} for r in submitted]
        self._send(json.dumps({'exam_started': exam_started,
                               'active': active, 'done': done},
                              ensure_ascii=False, indent=2),
                   ctype='application/json; charset=utf-8')

    def h_export(self):
        if not self._is_admin():
            self._send("Forbidden", status=403); return
        with lock:
            done = list(submitted)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=';')
        w.writerow(['Логин', 'Имя', 'Группа', 'Вариант', 'IP',
                    'Начало', 'Конец', 'Балл', 'Максимум', 'Процент',
                    'Детали по задачам'])
        for r in done:
            pct = round(100 * r['total_passed'] / r['total_max']) if r['total_max'] else 0
            details = ', '.join(f"З{i+1}: {p}/{t}"
                                for i, (p, t) in sorted(r['scores'].items()))
            w.writerow([
                r['login'], r['name'], r['group'], r['variant'], r['ip'],
                datetime.fromtimestamp(r['start_time']).strftime('%Y-%m-%d %H:%M:%S'),
                datetime.fromtimestamp(r['end_time']).strftime('%Y-%m-%d %H:%M:%S'),
                r['total_passed'], r['total_max'], pct, details,
            ])
        data = ('\ufeff' + buf.getvalue()).encode('utf-8')
        self._send(data, ctype='text/csv; charset=utf-8',
                   headers=[('Content-Disposition', 'attachment; filename=results.csv')])


# ============ ЗАПУСК ============
def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '127.0.0.1'


def main():
    print("=" * 62)
    print("  Контрольная по Python — загрузка конфигурации")
    print("=" * 62)
    load_variants()
    load_students()

    if not VARIANTS:
        print("\n  ❌ Не загружено ни одного варианта!")
        input("\n  Enter для выхода...")
        return
    if not STUDENTS:
        print("\n  ❌ Не загружен список студентов!")
        print("     Создайте students.json (см. make_passwords.py)")
        input("\n  Enter для выхода...")
        return

    print(f"\n  Студентов: {len(STUDENTS)}")
    print(f"  Вариантов: {len(VARIANTS)}")
    print(f"  Режим старта: {'по кнопке в админке' if REQUIRE_ADMIN_START else 'сразу после входа'}")

    threading.Thread(target=auto_finish_loop, daemon=True).start()

    ip = get_lan_ip()
    print()
    print("=" * 62)
    print(f"  Порт: {PORT} · Таймаут теста: {CODE_TIMEOUT} сек")
    print()
    print(f"  👉 Студенты:      http://{ip}:{PORT}/")
    print(f"  👉 Преподаватель: http://{ip}:{PORT}/admin")
    print(f"     пароль:        {ADMIN_PASSWORD}")
    print()
    print("  Ctrl+C для остановки")
    print("=" * 62)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        httpd.server_close()


if __name__ == '__main__':
    main()