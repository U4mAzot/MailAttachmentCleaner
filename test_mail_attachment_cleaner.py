"""Testy MailAttachmentCleaner. Uruchomienie: python -m unittest -v"""

import email
import email.policy
import tempfile
import unittest
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import mail_attachment_cleaner as cleaner

PDF_DATA = b'%PDF-1.4 ' + b'x' * 50_000
PNG_DATA = b'\x89PNG\r\n\x1a\n' + b'\x00' * 2_000


def build_message(attachments=(), inline_image=False, subject='Faktura'):
    message = MIMEMultipart('mixed')
    message['From'] = 'Jan Kowalski <jan@firma.pl>'
    message['To'] = 'anna@firma.pl'
    message['Subject'] = subject
    message['Date'] = 'Mon, 02 Jan 2023 10:00:00 +0100'

    if inline_image:
        related = MIMEMultipart('related')
        related.attach(MIMEText('<p>Treść</p><img src="cid:logo">', 'html', 'utf-8'))
        image = MIMEImage(PNG_DATA, 'png')
        image.add_header('Content-ID', '<logo>')
        image.add_header('Content-Disposition', 'inline', filename='logo.png')
        related.attach(image)
        message.attach(related)
    else:
        message.attach(MIMEText('Treść wiadomości - zażółć gęślą jaźń', 'plain', 'utf-8'))

    for filename, data in attachments:
        part = MIMEApplication(data, 'pdf')
        part.add_header('Content-Disposition', 'attachment', filename=filename)
        message.attach(part)
    return message


def to_bytes(message):
    return message.as_bytes(policy=cleaner.OUTPUT_POLICY)


class StripAttachmentsTests(unittest.TestCase):

    def test_removes_attachment_and_keeps_body(self):
        message = build_message([('faktura.pdf', PDF_DATA)])
        removed = cleaner.strip_attachments(message)

        self.assertEqual(removed, [('faktura.pdf', len(PDF_DATA))])
        reparsed = email.message_from_bytes(to_bytes(message), policy=email.policy.default)
        texts = [p.get_content() for p in reparsed.walk() if p.get_content_type() == 'text/plain']
        self.assertIn('zażółć gęślą jaźń', texts[0])
        self.assertIn('Nazwa pliku: faktura.pdf', texts[1])
        self.assertFalse(any(p.get_content_type() == 'application/pdf' for p in reparsed.walk()))
        self.assertIn('X-Attachments-Removed', reparsed)

    def test_message_without_attachments_is_unchanged(self):
        message = build_message()
        before = to_bytes(message)
        self.assertEqual(cleaner.strip_attachments(message), [])
        self.assertEqual(to_bytes(message), before)

    def test_inline_image_kept_by_default(self):
        message = build_message(inline_image=True)
        self.assertEqual(cleaner.strip_attachments(message), [])

    def test_inline_image_removed_with_option(self):
        message = build_message(inline_image=True)
        removed = cleaner.strip_attachments(message, include_inline=True)
        self.assertEqual([name for name, _ in removed], ['logo.png'])

    def test_polish_filename_is_decoded(self):
        message = build_message([(('utf-8', '', 'Faktura_żółw.pdf'), PDF_DATA)])
        removed = cleaner.strip_attachments(message)
        self.assertEqual(removed[0][0], 'Faktura_żółw.pdf')

    def test_file_without_content_disposition(self):
        message = build_message()
        part = MIMEApplication(PDF_DATA, 'octet-stream', name='stary_program.zip')
        del part['Content-Disposition']
        message.attach(part)
        removed = cleaner.strip_attachments(message)
        self.assertEqual([name for name, _ in removed], ['stary_program.zip'])

    def test_attachment_inside_forwarded_message(self):
        inner = build_message([('umowa.pdf', PDF_DATA)])
        outer = build_message()
        forwarded = email.message.Message()
        forwarded.set_type('message/rfc822')
        forwarded.set_payload([inner])
        outer.attach(forwarded)

        removed = cleaner.strip_attachments(outer)
        self.assertEqual([name for name, _ in removed], ['umowa.pdf'])

    def test_result_is_much_smaller(self):
        message = build_message([('a.pdf', PDF_DATA), ('b.pdf', PDF_DATA)])
        original_size = len(to_bytes(message))
        cleaner.strip_attachments(message)
        self.assertLess(len(to_bytes(message)), original_size * 0.05)


class HelperTests(unittest.TestCase):

    def test_imap_date_is_locale_independent(self):
        self.assertEqual(cleaner.imap_date(date(2024, 1, 5)), '05-Jan-2024')
        self.assertEqual(cleaner.imap_date(date(2023, 12, 31)), '31-Dec-2023')

    def test_decode_folder_name(self):
        self.assertEqual(cleaner.decode_folder_name('Wys&AUI-ane'), 'Wysłane')
        self.assertEqual(cleaner.decode_folder_name('Elementy usuni&ARk-te'), 'Elementy usunięte')
        self.assertEqual(cleaner.decode_folder_name('A&-B'), 'A&B')

    def test_human_size(self):
        self.assertEqual(cleaner.human_size(500), '500 B')
        self.assertEqual(cleaner.human_size(2048), '2.0 KB')
        self.assertEqual(cleaner.human_size(5 * 1024 ** 3), '5.00 GB')

    def test_search_criteria(self):
        criteria = cleaner.build_search_criteria(date(2024, 1, 1), date(2023, 1, 1), 100)
        self.assertEqual(criteria, ['UNDELETED', 'BEFORE', '01-Jan-2024', 'SINCE', '01-Jan-2023', 'LARGER', '102400'])

    def test_parse_fetch_response_with_flags_after_body(self):
        data = [(b'1 (UID 7 BODY[] {5}', b'RAW!!'), b' FLAGS (\\Seen \\Recent $Label1) INTERNALDATE "02-Jan-2023 10:00:00 +0100")']
        raw, flags, internal_date = cleaner.parse_fetch_response(data)
        self.assertEqual(raw, b'RAW!!')
        self.assertEqual(flags, ('\\Seen', '$Label1'))
        self.assertEqual(internal_date, '"02-Jan-2023 10:00:00 +0100"')

    def test_list_folders_skips_trash_and_noselect(self):
        class Conn:
            def list(self):
                return 'OK', [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\HasNoChildren \\Sent) "/" "Wys&AUI-ane"',
                    b'(\\HasNoChildren \\Trash) "/" "Kosz"',
                    b'(\\Noselect \\HasChildren) "/" "Archiwum"',
                    (b'(\\HasNoChildren) "/" {14}', b'Archiwum/2023'),
                ]
        self.assertEqual(cleaner.list_folders(Conn()), ['INBOX', 'Wys&AUI-ane', 'Archiwum/2023'])


class FakeIMAP:
    """Atrapa serwera IMAP - zapamiętuje wykonane operacje."""

    def __init__(self, messages, uidplus=True, append_ok=True):
        self.messages = dict(messages)  # uid -> surowa wiadomość
        self.capabilities = ('IMAP4REV1', 'UIDPLUS') if uidplus else ('IMAP4REV1',)
        self.append_ok = append_ok
        self.operations = []
        self.appended = []

    def select(self, mailbox, readonly=False):
        self.operations.append(('SELECT', mailbox, readonly))
        return 'OK', [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        self.operations.append((command, args[0] if args else None))
        if command == 'SEARCH':
            return 'OK', [b' '.join(str(uid).encode() for uid in self.messages)]
        if command == 'FETCH':
            raw = self.messages[int(args[0])]
            header = f'1 (UID {args[0]} FLAGS (\\Seen) INTERNALDATE "02-Jan-2023 10:00:00 +0100" BODY[] {{{len(raw)}}}'
            return 'OK', [(header.encode(), raw), b')']
        if command == 'EXPUNGE':
            self.messages.pop(int(args[0]))
        return 'OK', [None]

    def append(self, mailbox, flags, date_time, message):
        self.operations.append(('APPEND', mailbox))
        self.appended.append((flags, date_time, message))
        return ('OK', [b'APPEND completed']) if self.append_ok else ('NO', [b'Quota exceeded'])

    def expunge(self):
        self.operations.append(('EXPUNGE-ALL', None))
        return 'OK', [None]


class ProcessFolderTests(unittest.TestCase):

    def setUp(self):
        self.backup_dir = tempfile.mkdtemp()
        self.with_attachment = to_bytes(build_message([('faktura.pdf', PDF_DATA)]))
        self.without_attachment = to_bytes(build_message(subject='Zwykły mail'))
        self.stats = {'scanned': 0, 'messages': 0, 'attachments': 0, 'bytes_saved': 0, 'errors': 0}

    def make_args(self, *extra):
        base = ['--server', 'imap.firma.pl', '--user', 'jan@firma.pl', '--before', '2024-01-01',
                '--backup-dir', self.backup_dir]
        return cleaner.parse_args(base + list(extra))

    def test_preview_changes_nothing(self):
        conn = FakeIMAP({1: self.with_attachment, 2: self.without_attachment})
        cleaner.process_folder(conn, 'INBOX', self.make_args(), self.stats)

        self.assertEqual(conn.operations[0], ('SELECT', '"INBOX"', True))  # tylko do odczytu
        self.assertEqual(conn.appended, [])
        self.assertNotIn('STORE', [op[0] for op in conn.operations])
        self.assertEqual(self.stats['messages'], 1)
        self.assertEqual(self.stats['scanned'], 2)
        self.assertGreater(self.stats['bytes_saved'], len(PDF_DATA))

    def test_apply_backs_up_appends_then_deletes(self):
        conn = FakeIMAP({1: self.with_attachment, 2: self.without_attachment})
        cleaner.process_folder(conn, 'INBOX', self.make_args('--apply'), self.stats)

        # kopia zapasowa identyczna z oryginałem
        backups = list(Path(self.backup_dir).rglob('*.eml'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.with_attachment)

        # nowa wersja z oryginalnymi flagami i datą, bez załącznika
        flags, date_time, new_raw = conn.appended[0]
        self.assertEqual(flags, '(\\Seen)')
        self.assertEqual(date_time, '"02-Jan-2023 10:00:00 +0100"')
        new_message = email.message_from_bytes(new_raw)
        self.assertFalse(any(p.get_content_type() == 'application/pdf' for p in new_message.walk()))

        # kolejność: najpierw APPEND, potem oznaczenie i usunięcie oryginału
        names = [op[0] for op in conn.operations]
        self.assertLess(names.index('APPEND'), names.index('STORE'))
        self.assertEqual(conn.operations[names.index('STORE')], ('STORE', '1'))
        self.assertEqual(list(conn.messages), [2])  # mail bez załącznika nietknięty
        self.assertEqual(self.stats['attachments'], 1)

    def test_failed_append_keeps_original(self):
        conn = FakeIMAP({1: self.with_attachment}, append_ok=False)
        cleaner.process_folder(conn, 'INBOX', self.make_args('--apply'), self.stats)

        self.assertNotIn('STORE', [op[0] for op in conn.operations])
        self.assertIn(1, conn.messages)
        self.assertEqual(self.stats['errors'], 1)
        self.assertEqual(self.stats['messages'], 0)

    def test_server_without_uidplus_uses_expunge(self):
        conn = FakeIMAP({1: self.with_attachment}, uidplus=False)
        cleaner.process_folder(conn, 'INBOX', self.make_args('--apply'), self.stats)
        self.assertEqual(conn.operations[-1], ('EXPUNGE-ALL', None))


class ArgumentTests(unittest.TestCase):

    def test_older_than_days_sets_before(self):
        args = cleaner.parse_args(['--server', 's', '--user', 'u', '--older-than-days', '30'])
        self.assertEqual((date.today() - args.before).days, 30)
        self.assertFalse(args.apply)

    def test_since_must_be_before_end(self):
        with self.assertRaises(SystemExit):
            cleaner.parse_args(['--server', 's', '--user', 'u', '--before', '2023-01-01', '--since', '2024-01-01'])


if __name__ == '__main__':
    unittest.main()
