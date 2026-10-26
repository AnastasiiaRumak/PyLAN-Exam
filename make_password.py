#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генерирует students.json из students.txt.
Запуск:  python make_passwords.py
"""
import json
import os
import secrets

INPUT = "students.txt"
OUTPUT = "students.json"

TRANS = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def translit(s):
    out = []
    for ch in s.lower():
        if ch in TRANS:
            out.append(TRANS[ch])
        elif ch.isalnum():
            out.append(ch)
    return ''.join(out)


def make_login(name, used):
    first = name.split()[0] if name.strip() else 'student'
    base = translit(first) or 'student'
    login = base
    n = 1
    while login in used:
        n += 1
        login = f"{base}{n}"
    return login


def make_password(length=8):
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def main():
    if not os.path.isfile(INPUT):
        print(f"❌ Не найден файл {INPUT}.")
        print(f"   Создайте {INPUT} со строками вида:  Группа;Фамилия Имя")
        input("Enter для выхода...")
        return

    students = []
    used = set()
    with open(INPUT, 'r', encoding='utf-8-sig') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(';')]
            if len(parts) < 2:
                print(f"⚠ пропущено (нет ;): {line}")
                continue
            group, name = parts[0], parts[1]
            login = make_login(name, used)
            used.add(login)
            students.append({
                'login': login,
                'password': make_password(),
                'name': name,
                'group': group,
            })

    if not students:
        print("❌ Список пуст.")
        input("Enter...")
        return

    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(students, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Записан {OUTPUT}: {len(students)} студентов\n")
    print(f"{'ЛОГИН':<15} {'ПАРОЛЬ':<12} {'ФИО':<30} {'ГРУППА'}")
    print('-' * 80)
    for s in students:
        print(f"{s['login']:<15} {s['password']:<12} {s['name']:<30} {s['group']}")
    print('-' * 80)
    print("\nРаздайте логины и пароли студентам (или распечатайте).")
    print(f"Файл {OUTPUT} должен лежать рядом с server.py.")
    input("\nEnter для выхода...")


if __name__ == '__main__':
    main()