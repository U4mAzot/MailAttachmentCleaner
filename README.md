# MailAttachmentCleaner - zwalnianie miejsca w skrzynce pocztowej

Skrypt w Pythonie, który loguje się do skrzynki przez **IMAP** i usuwa załączniki
ze starych wiadomości. Rozwiązuje częste zgłoszenie: **„skrzynka jest pełna, nie mogę
odbierać poczty”**. Same wiadomości (treść, nadawca, data, foldery) zostają, a znikają
tylko duże pliki, które zajmują najwięcej miejsca.

## Jak działa

Dla każdej wiadomości z wybranego okresu skrypt:

1. zapisuje **oryginał jako plik `.eml`** w folderze kopii zapasowej (`backup\`),
2. tworzy wersję wiadomości bez załączników; w miejscu każdego pliku zostaje notka:
   ```
   [Załącznik usunięty w celu zmniejszenia rozmiaru skrzynki]
   Nazwa pliku: faktura_03_2023.pdf
   Rozmiar: 2.4 MB
   Data usunięcia: 2026-10-14
   ```
3. dodaje nową wersję do tego samego folderu, **z oryginalną datą i flagami** (przeczytana, oznaczona itp.),
4. **dopiero po udanym dodaniu** usuwa oryginał. Jeśli serwer odrzuci nową wersję (np. brak miejsca), oryginał zostaje nietknięty.

## Bezpieczeństwo

- **Domyślnie tryb podglądu**: skrypt tylko pokazuje, co i ile miejsca zwolni. Zmiany wprowadza dopiero opcja `--apply`.
- Przed zmianami prosi o potwierdzenie.
- Kopie zapasowe oryginałów (pliki `.eml` otwierają się w Outlooku i Thunderbirdzie).
- Hasło wpisywane jako ukryte (albo przez zmienną środowiskową `IMAP_PASSWORD`), nigdy w parametrach.
- Obrazki osadzone w treści (np. logo w stopce) są domyślnie **zostawiane**, żeby nie psuć wyglądu wiadomości.
- Przy `--all-folders` pomijane są Kosz, Spam i Szkice.
- Gmail jest blokowany, bo etykiety Gmaila działają inaczej niż foldery IMAP.

## Wymagania

- Python 3.8+
- Tylko biblioteka standardowa, nic nie trzeba instalować

## Użycie

```bash
# 1. Podgląd: co zostanie usunięte z wiadomości starszych niż rok (folder Odebrane)
python mail_attachment_cleaner.py --server imap.firma.pl --user jan@firma.pl --older-than-days 365

# 2. Usunięcie załączników z wiadomości sprzed 2024 roku we wszystkich folderach
python mail_attachment_cleaner.py --server imap.firma.pl --user jan@firma.pl --before 2024-01-01 --all-folders --apply

# Konkretny okres (od-do) i tylko duże wiadomości (powyżej 1 MB)
python mail_attachment_cleaner.py --server imap.firma.pl --user jan@firma.pl \
    --since 2020-01-01 --before 2023-01-01 --min-size-kb 1024 --apply

# Wybrane foldery
python mail_attachment_cleaner.py --server imap.firma.pl --user jan@firma.pl --older-than-days 730 \
    --folder INBOX --folder "Archiwum/2022"
```

### Najważniejsze opcje

| Opcja | Opis |
|-------|------|
| `--before RRRR-MM-DD` | wiadomości starsze niż data |
| `--older-than-days N` | wiadomości starsze niż N dni |
| `--since RRRR-MM-DD` | dolna granica okresu (opcjonalnie) |
| `--folder NAZWA` | folder do przetworzenia (domyślnie `INBOX`, można podać kilka) |
| `--all-folders` | wszystkie foldery poza Koszem, Spamem i Szkicami |
| `--min-size-kb N` | pomija wiadomości mniejsze niż N KB (domyślnie 100) |
| `--include-inline` | usuwa też obrazki osadzone w treści |
| `--apply` | wprowadza zmiany (bez tego tylko podgląd) |
| `--backup-dir` / `--no-backup` | folder kopii zapasowych / wyłączenie kopii |
| `--starttls`, `--port` | połączenie STARTTLS (port 143) zamiast SSL (993) |

## Przykładowy wynik (podgląd)

```
======================================================================
Serwer: imap.firma.pl | Konto: jan@firma.pl
Okres: początek - 2025-10-14 | Min. rozmiar: 100 KB | Tryb: PODGLĄD (nic nie zostanie zmienione)
======================================================================
Folder INBOX                          wiadomości do sprawdzenia: 214
  ["14-Mar-2023 09:12:44 +0100"] Biuro Rachunkowe <biuro@... | Faktury 03/2023 | faktura_03.pdf (2.4 MB)
  ["02-Jun-2024 15:40:02 +0200"] Anna Nowak <anna@firma.pl>  | Zdjęcia z wyjazdu | IMG_001.jpg (4.1 MB), IMG_002.jpg (3.9 MB)
  ...
======================================================================
Sprawdzone wiadomości:          214
Wiadomości z załącznikami:      187
Załączniki do usunięcia:        402
Miejsce do zwolnienia:          1.84 GB
Aby usunąć załączniki, uruchom ponownie z opcją --apply
```

## Testy

Projekt ma 20 testów automatycznych (m.in. atrapa serwera IMAP sprawdzająca, że oryginał
jest usuwany dopiero po udanym zapisaniu nowej wersji):

```bash
python -m unittest -v
```

## Wykorzystane umiejętności

- Protokół IMAP: `SEARCH`, `FETCH`, `APPEND`, `STORE`, `UID EXPUNGE`, flagi, foldery w kodowaniu UTF-7
- Budowa wiadomości e-mail (MIME): części, załączniki, kodowanie nazw plików z polskimi znakami
- Bezpieczne operacje na danych użytkownika: podgląd, kopia zapasowa, kolejność „najpierw zapisz, potem usuń”
- Python: `imaplib`, `email`, `argparse`, `logging`, testy jednostkowe z atrapami (`unittest`)
