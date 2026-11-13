"""Owner-only console: keys are entered privately, never passed on the command line."""
import argparse
import getpass
from pathlib import Path
import warnings

from auth_store import AuthStore

def read_key():
    # Refuse any fallback that would echo a secret into a redirected console/log.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        first = getpass.getpass('Ключ (16–128 символів, введення приховане): ')
        second = getpass.getpass('Повторіть ключ: ')
    if first != second:
        raise ValueError('Ключі не збігаються')
    return first

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=r'D:\argusDB')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    auth = AuthStore(Path(args.root) / 'private' / ('users-test.sqlite3' if args.test else 'users.sqlite3'))
    print('Користувачі ARGUS. Ключі задає власник; переглянути збережений ключ неможливо.')
    while True:
        print('\n1 Створити користувача\n2 Змінити ключ\n3 Вимкнути доступ\n4 Увімкнути доступ\n5 Список користувачів\n0 Вийти')
        choice = input('Дія: ').strip()
        if choice == '0':
            return
        try:
            if choice == '5':
                for item in auth.list_users():
                    print(item['username'], '— активний' if item['enabled'] else '— вимкнений')
                continue
            if choice not in ('1', '2', '3', '4'):
                continue
            name = input('Логін: ').strip()
            if choice == '1':
                auth.create(name, read_key())
            elif choice == '2':
                auth.reset_key(name, read_key())
            else:
                auth.set_enabled(name, choice == '4')
            print('Збережено. При зміні ключа або статусу попередні сеанси більше не діють.')
        except (ValueError, getpass.GetPassWarning) as error:
            print('Не виконано:', error)

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nЗакрито без подальших змін.')
