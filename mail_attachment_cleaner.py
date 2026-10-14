#!/usr/bin/env python3
"""
MailAttachmentCleaner - usuwa załączniki ze starych wiadomości w skrzynce IMAP,
żeby zmniejszyć zajętość skrzynki.

Dla każdej wiadomości spełniającej kryteria (okres, minimalny rozmiar) skrypt:
  1. zapisuje oryginał jako plik .eml w folderze kopii zapasowej,
  2. tworzy kopię wiadomości bez załączników - w miejscu każdego załącznika
     zostaje krótka notka z nazwą pliku i rozmiarem,
  3. dodaje nową wersję do tego samego folderu z oryginalną datą i flagami,
  4. dopiero po udanym dodaniu usuwa oryginał.

Domyślnie działa w trybie PODGLĄDU - zmiany wprowadza dopiero z opcją --apply.
Wymaga tylko biblioteki standardowej Pythona (3.8+).
"""

import argparse
import base64
import email
import email.header
import email.policy
import getpass
import imaplib
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IMAP_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
# IMAP wymaga końców linii CRLF
OUTPUT_POLICY = email.policy.compat32.clone(linesep='\r\n')
# foldery pomijane przy --all-folders (flagi SPECIAL-USE z RFC 6154)
SKIPPED_FOLDER_FLAGS = ('\\trash', '\\junk', '\\drafts')

log = logging.getLogger('mail_attachment_cleaner')


# ---------------------------------------------------------------------------
# Funkcje pomocnicze
# ---------------------------------------------------------------------------

def imap_date(value: date) -> str:
    """Data w formacie IMAP (np. 01-Jan-2024) - niezależnie od ustawień regionalnych."""
    return f'{value.day:02d}-{IMAP_MONTHS[value.month - 1]}-{value.year}'


def human_size(size: float) -> str:
    for unit in ('B', 'KB', 'MB'):
        if size < 1024:
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.2f} GB'


def decode_header_value(value) -> str:
    """Dekoduje nagłówki typu =?utf-8?B?...?= (np. polskie znaki w temacie lub nazwie pliku)."""
    if not value:
        return ''
    try:
        return str(email.header.make_header(email.header.decode_header(str(value))))
    except (ValueError, LookupError, UnicodeDecodeError):
        return str(value)


def decode_folder_name(name: str) -> str:
    """Nazwy folderów IMAP są w zmodyfikowanym UTF-7, np. 'Wys&AUI-ane' -> 'Wysłane'."""
    def replace(match):
        chunk = match.group(1)
        if not chunk:
            return '&'
        b64 = chunk.replace(',', '/')
        b64 += '=' * (-len(b64) % 4)
        return base64.b64decode(b64).decode('utf-16-be')
    return re.sub(r'&([^-]*)-', replace, name)


def quote_folder(name: str) -> str:
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def safe_filename(text: str, max_length: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', text).strip(' .')
    return text[:max_length] or 'bez_tematu'


# ---------------------------------------------------------------------------
# Usuwanie załączników z wiadomości
# ---------------------------------------------------------------------------

def is_attachment(part, include_inline: bool = False) -> bool:
    """Czy dana część wiadomości jest załącznikiem do usunięcia."""
    disposition = part.get_content_disposition()  # 'attachment', 'inline' albo None

    if disposition == 'attachment':
        return True

    # obrazki osadzone w treści HTML (np. logo w stopce) - tylko na życzenie
    if disposition == 'inline' or part.get('Content-ID'):
        return include_inline and part.get_content_maintype() not in ('text', 'multipart')

    # starsze programy pocztowe: plik z nazwą, ale bez nagłówka Content-Disposition
    return (disposition is None
            and bool(part.get_filename())
            and part.get_content_maintype() not in ('text', 'multipart', 'message'))


def attachment_size(part) -> int:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return len(payload)
    return len(part.as_bytes())  # np. załączona wiadomość (message/rfc822)


def make_placeholder(filename: str, size: int) -> MIMEText:
    text = (
        '[Załącznik usunięty w celu zmniejszenia rozmiaru skrzynki]\n'
        f'Nazwa pliku: {filename}\n'
        f'Rozmiar: {human_size(size)}\n'
        f'Data usunięcia: {date.today():%Y-%m-%d}\n'
    )
    placeholder = MIMEText(text, 'plain', 'utf-8')
    placeholder.add_header('Content-Disposition', 'inline')
    return placeholder


def strip_attachments(message, include_inline: bool = False):
    """
    Usuwa załączniki z wiadomości (modyfikuje ją w miejscu).
    Zwraca listę krotek (nazwa_pliku, rozmiar_w_bajtach) usuniętych załączników.
    """
    removed = []

    def walk(container):
        payload = container.get_payload()
        if not isinstance(payload, list):
            return
        new_payload = []
        for part in payload:
            if is_attachment(part, include_inline):
                filename = decode_header_value(part.get_filename()) or f'bez_nazwy.{part.get_content_subtype()}'
                size = attachment_size(part)
                removed.append((filename, size))
                new_payload.append(make_placeholder(filename, size))
            else:
                if part.is_multipart():
                    walk(part)  # np. załączniki w przekazanej dalej wiadomości
                new_payload.append(part)
        container.set_payload(new_payload)

    # wiadomość, która w całości jest jednym plikiem (bez części tekstowej), zostawiamy bez zmian
    if message.is_multipart():
        walk(message)

    if removed:
        message['X-Attachments-Removed'] = f'{len(removed)}; {date.today():%Y-%m-%d}'
    return removed


# ---------------------------------------------------------------------------
# Obsługa IMAP
# ---------------------------------------------------------------------------

def connect(server: str, port: int, user: str, password: str, starttls: bool):
    if starttls:
        conn = imaplib.IMAP4(server, port or 143)
        conn.starttls()
    else:
        conn = imaplib.IMAP4_SSL(server, port or 993)
    conn.login(user, password)

    # wiele serwerów podaje pełną listę możliwości (np. UIDPLUS) dopiero po zalogowaniu
    typ, data = conn.capability()
    if typ == 'OK' and data and data[-1]:
        conn.capabilities = tuple(data[-1].decode().upper().split())
    return conn


def list_folders(conn, skip_special: bool = True):
    """Zwraca listę folderów, w których można wyszukiwać wiadomości."""
    typ, data = conn.list()
    if typ != 'OK':
        raise RuntimeError('Nie udało się pobrać listy folderów')

    folders = []
    for line in data:
        if isinstance(line, tuple):  # nazwa przesłana jako literał {n}
            line = re.sub(rb'\{\d+\}$', b'', line[0]) + b'"' + line[1] + b'"'
        if not line:
            continue
        line = line.decode('utf-8', errors='replace')
        match = re.match(r'\((?P<flags>[^)]*)\) (?P<delimiter>"[^"]*"|NIL) (?P<name>.+)$', line)
        if not match:
            continue
        flags = match['flags'].lower()
        if '\\noselect' in flags or '\\nonexistent' in flags:
            continue
        if skip_special and any(flag in flags for flag in SKIPPED_FOLDER_FLAGS):
            continue
        name = match['name'].strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        folders.append(name)
    return folders


def parse_fetch_response(data):
    """Wyciąga z odpowiedzi FETCH: surową wiadomość, flagi i datę wewnętrzną."""
    raw, meta = None, b''
    for item in data:
        if isinstance(item, tuple):
            meta += item[0] + b' '
            raw = item[1]
        elif isinstance(item, bytes):
            meta += item  # część serwerów wysyła FLAGS po treści wiadomości

    flags = tuple(f.decode() for f in imaplib.ParseFlags(meta) if f.lower() != b'\\recent')
    date_match = re.search(rb'INTERNALDATE "([^"]+)"', meta)
    internal_date = '"' + date_match.group(1).decode() + '"' if date_match else None
    return raw, flags, internal_date


def build_search_criteria(before=None, since=None, min_size_kb=0):
    criteria = ['UNDELETED']
    if before:
        criteria += ['BEFORE', imap_date(before)]
    if since:
        criteria += ['SINCE', imap_date(since)]
    if min_size_kb > 0:
        criteria += ['LARGER', str(min_size_kb * 1024)]
    return criteria


def add_to_stats(stats, removed, saved):
    stats['messages'] += 1
    stats['attachments'] += len(removed)
    stats['bytes_saved'] += saved


def process_folder(conn, folder, args, stats):
    display_name = decode_folder_name(folder)
    typ, _ = conn.select(quote_folder(folder), readonly=not args.apply)
    if typ != 'OK':
        log.warning('Pominięto folder %s (nie można go otworzyć)', display_name)
        return

    criteria = build_search_criteria(args.before, args.since, args.min_size_kb)
    typ, data = conn.uid('SEARCH', *criteria)
    uids = data[0].split() if typ == 'OK' and data and data[0] else []
    log.info('Folder %-30s wiadomości do sprawdzenia: %d', display_name, len(uids))

    uidplus = 'UIDPLUS' in conn.capabilities
    needs_expunge = False

    for uid in uids:
        uid = uid.decode()
        typ, data = conn.uid('FETCH', uid, '(FLAGS INTERNALDATE BODY.PEEK[])')
        if typ != 'OK' or not data or data[0] is None:
            log.warning('  UID %s: nie udało się pobrać wiadomości', uid)
            stats['errors'] += 1
            continue

        raw, flags, internal_date = parse_fetch_response(data)
        if raw is None:
            continue
        stats['scanned'] += 1

        message = email.message_from_bytes(raw, policy=email.policy.compat32)
        removed = strip_attachments(message, args.include_inline)
        if not removed:
            continue

        new_raw = message.as_bytes(policy=OUTPUT_POLICY)
        saved = max(0, len(raw) - len(new_raw))
        subject = decode_header_value(message.get('Subject')) or '(bez tematu)'
        sender = decode_header_value(message.get('From'))
        files = ', '.join(f'{name} ({human_size(size)})' for name, size in removed)

        log.info('  [%s] %s | %s | %s', internal_date or '?', sender[:40], subject[:60], files)

        if not args.apply:
            add_to_stats(stats, removed, saved)
            continue

        # 1. kopia zapasowa oryginału
        if args.backup_dir:
            backup_folder = Path(args.backup_dir) / safe_filename(display_name)
            backup_file = backup_folder / f'{uid}_{safe_filename(subject)}.eml'
            try:
                backup_folder.mkdir(parents=True, exist_ok=True)
                backup_file.write_bytes(raw)
            except OSError as error:
                log.error('  UID %s: nie udało się zapisać kopii (%s) - wiadomość pominięta', uid, error)
                stats['errors'] += 1
                continue

        # 2. dodanie wersji bez załączników
        flag_list = '(' + ' '.join(flags) + ')' if flags else None
        typ, response = conn.append(quote_folder(folder), flag_list, internal_date, new_raw)
        if typ != 'OK':
            log.error('  UID %s: serwer odrzucił nową wersję (%s) - oryginał pozostawiony', uid, response)
            stats['errors'] += 1
            continue

        # 3. usunięcie oryginału - dopiero po udanym dodaniu nowej wersji
        conn.uid('STORE', uid, '+FLAGS.SILENT', '(\\Deleted)')
        if uidplus:
            conn.uid('EXPUNGE', uid)
        else:
            needs_expunge = True
        add_to_stats(stats, removed, saved)

    if needs_expunge:
        # serwer bez UIDPLUS - EXPUNGE usuwa wszystkie wiadomości z flagą \Deleted w folderze
        conn.expunge()


# ---------------------------------------------------------------------------
# Program główny
# ---------------------------------------------------------------------------

def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise argparse.ArgumentTypeError(f'niepoprawna data "{value}" - użyj formatu RRRR-MM-DD')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Usuwa załączniki ze starych wiadomości w skrzynce IMAP, żeby zwolnić miejsce.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Przykłady:\n'
            '  Podgląd - załączniki starsze niż rok w Odebranych:\n'
            '    python mail_attachment_cleaner.py --server imap.firma.pl --user jan@firma.pl --older-than-days 365\n\n'
            '  Usunięcie załączników > 1 MB z wiadomości sprzed 2024 we wszystkich folderach:\n'
            '    python mail_attachment_cleaner.py --server imap.firma.pl --user jan@firma.pl \\\n'
            '        --before 2024-01-01 --all-folders --min-size-kb 1024 --apply\n'
        ),
    )
    connection = parser.add_argument_group('połączenie')
    connection.add_argument('--server', required=True, help='adres serwera IMAP')
    connection.add_argument('--user', required=True, help='login (zwykle adres e-mail)')
    connection.add_argument('--port', type=int, help='port (domyślnie 993, a z --starttls 143)')
    connection.add_argument('--starttls', action='store_true', help='połączenie STARTTLS zamiast SSL')

    period = parser.add_argument_group('okres i wybór wiadomości')
    period_choice = period.add_mutually_exclusive_group(required=True)
    period_choice.add_argument('--before', type=parse_date, metavar='RRRR-MM-DD',
                               help='wiadomości starsze niż podana data')
    period_choice.add_argument('--older-than-days', type=int, metavar='DNI',
                               help='wiadomości starsze niż podana liczba dni')
    period.add_argument('--since', type=parse_date, metavar='RRRR-MM-DD',
                        help='opcjonalnie: tylko wiadomości nowsze niż ta data (zakres od-do)')
    period.add_argument('--folder', action='append', dest='folders', metavar='NAZWA',
                        help='folder do przetworzenia (można podać kilka razy, domyślnie INBOX)')
    period.add_argument('--all-folders', action='store_true',
                        help='wszystkie foldery poza Koszem, Spamem i Szkicami')
    period.add_argument('--min-size-kb', type=int, default=100,
                        help='pomijaj wiadomości mniejsze niż podany rozmiar (domyślnie 100 KB)')
    period.add_argument('--include-inline', action='store_true',
                        help='usuwaj także obrazki osadzone w treści (np. logo w stopce)')

    safety = parser.add_argument_group('bezpieczeństwo')
    safety.add_argument('--apply', action='store_true',
                        help='wprowadź zmiany (bez tej opcji działa tylko podgląd)')
    safety.add_argument('--yes', action='store_true', help='nie pytaj o potwierdzenie przy --apply')
    safety.add_argument('--backup-dir', default=str(SCRIPT_DIR / 'backup'),
                        help='folder na kopie oryginalnych wiadomości (domyślnie ./backup)')
    safety.add_argument('--no-backup', action='store_true', help='nie zapisuj kopii zapasowych (niezalecane)')

    args = parser.parse_args(argv)
    if args.older_than_days is not None:
        args.before = date.today() - timedelta(days=args.older_than_days)
    if args.since and args.since >= args.before:
        parser.error('data --since musi być wcześniejsza niż koniec okresu')
    if args.no_backup:
        args.backup_dir = None
    return args


def setup_logging():
    log_dir = SCRIPT_DIR / 'logs'
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f'cleaner_{datetime.now():%Y%m%d_%H%M%S}.log'

    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(message)s'))
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
    log.addHandler(console)
    log.addHandler(file_handler)
    return log_file


def main(argv=None):
    args = parse_args(argv)
    log_file = setup_logging()

    if 'gmail.com' in args.server.lower() or 'googlemail.com' in args.server.lower():
        log.error('Gmail nie jest obsługiwany: etykiety Gmaila działają inaczej niż foldery IMAP '
                  'i usunięcie wiadomości z folderu nie usuwa jej z "Wszystkich wiadomości".')
        return 2

    password = os.environ.get('IMAP_PASSWORD') or getpass.getpass(f'Hasło dla {args.user}: ')

    mode = 'ZMIANY' if args.apply else 'PODGLĄD (nic nie zostanie zmienione)'
    log.info('=' * 70)
    log.info('Serwer: %s | Konto: %s', args.server, args.user)
    log.info('Okres: %s - %s | Min. rozmiar: %d KB | Tryb: %s',
             args.since or 'początek', args.before, args.min_size_kb, mode)
    log.info('=' * 70)

    if args.apply and not args.yes:
        answer = input('Załączniki zostaną usunięte z wiadomości na serwerze. Kontynuować? [tak/nie]: ')
        if answer.strip().lower() not in ('t', 'tak', 'y', 'yes'):
            log.info('Przerwano.')
            return 1

    stats = {'scanned': 0, 'messages': 0, 'attachments': 0, 'bytes_saved': 0, 'errors': 0}

    try:
        conn = connect(args.server, args.port, args.user, password, args.starttls)
    except (imaplib.IMAP4.error, OSError) as error:
        log.error('Nie udało się połączyć lub zalogować: %s', error)
        return 1

    try:
        folders = list_folders(conn) if args.all_folders else (args.folders or ['INBOX'])
        for folder in folders:
            try:
                process_folder(conn, folder, args, stats)
            except imaplib.IMAP4.error as error:
                log.error('Błąd w folderze %s: %s', decode_folder_name(folder), error)
                stats['errors'] += 1
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    log.info('=' * 70)
    log.info('Sprawdzone wiadomości:          %d', stats['scanned'])
    log.info('Wiadomości z załącznikami:      %d', stats['messages'])
    log.info('Załączniki %-20s%d', 'usunięte:' if args.apply else 'do usunięcia:', stats['attachments'])
    log.info('Miejsce %-23s%s', 'zwolnione:' if args.apply else 'do zwolnienia:', human_size(stats['bytes_saved']))
    if stats['errors']:
        log.info('Błędy:                          %d', stats['errors'])
    if args.apply and args.backup_dir and stats['messages']:
        log.info('Kopie oryginałów:               %s', args.backup_dir)
    if not args.apply and stats['messages']:
        log.info('Aby usunąć załączniki, uruchom ponownie z opcją --apply')
    log.info('Log: %s', log_file)
    return 0 if stats['errors'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
