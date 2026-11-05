# Start następnej sesji (stan na 5.11.2026)

Kod: wszystkie fazy 1-5 zrobione i zacommitowane, 625 testów (`python -m pytest -q`). Ostatnie kroki: strona
`/settings` w panelu (klucze w `.kasa/secrets.env`), poprawki przeglądów. Otwarte znaleziska: `docs/PRZEGLADY.md`.

## Pierwsze kroki w nowej sesji
1. Odtwórz repo z pliku `kasa-backup.bundle` (wgrany przez użytkownika): `git clone kasa-backup.bundle kasa`.
2. Postgres: `service postgresql start`, rola i baza `kasa` (patrz README), `python -m kasa init-db`.
3. Zależności: `uv pip install --system -e ".[web,dev]"`.
4. Sprawdź zmienne środowiska BEZ wypisywania wartości (tylko „puste / ustawione, N znaków”):
   KASA_PSEUDONYM_SECRET, KASA_EB_APP_ID, KASA_EB_PRIVATE_KEY (PEM w jednej linii w cudzysłowie ze znakami \n),
   KASA_EB_SESSION_ID, KASA_BANK_PROVIDER. Kasa sama zamienia KASA_EB_PRIVATE_KEY na plik (`load_secrets`).
5. Sieć: `api.enablebanking.com` i `api.nbp.pl` są dopuszczone (sprawdzone 5.11: HTTP 301).

## Następne zadanie: sesja EnableBanking (mBank, Millennium, rachunek VAT)
- Application ID = nazwa pliku .pem użytkownika (do potwierdzenia z panelem EB).
- Brakuje KASA_EB_SESSION_ID: przeprowadzić użytkownika przez autoryzację (POST /auth → link do banku →
  redirect z `code` → POST /sessions), opis w `docs/BANK.md`. Potem `python -m kasa bank consent`,
  `bank accounts`, `bank ingest` za pełny miesiąc i kontrola ciągłości sald (bramka: bilans co do grosza).
- Uwaga z przeglądu fazy 2 pkt 2 (PRZEGLADY.md): przed pierwszym realnym pobraniem EB sprawdzić, że zmiana
  treści przy tym samym `entry_reference` tworzy nową wersję faktu.

## Dalej (kolejność z briefingu)
KSeF (token tylko do odczytu, KASA_KSEF_NIP/TOKEN, domena ksef.mf.gov.pl) → eksport biura i JPK → Vertex AI
(domeny europe-west1-aiplatform.googleapis.com, oauth2.googleapis.com) → Supabase.
Do tego czasu KASA_KSEF_PROVIDER i KASA_LLM_PROVIDER = mock.

## Ustalenia z użytkownikiem
- Pracuje na Macu; nie wkleja kluczy do czatu, tylko w Environment variables środowiska „Spinacz - Finanse”.
- GitHub (artur873/kasa) niepodpięty do Claude Code; kod przenoszony plikiem bundle.
- Kategorie ODM: z arkusza użytkownika (w `kasa/extensions/odm/extension.yaml`).
- Pytania do agenta: `docs/PYTANIA_AGENTA.md` wystarczą na start; zestaw rośnie w trakcie.

## Supabase (stan 5.11.2026, wieczór)
- Baza docelowa: projekt **odm** w organizacji ODM Partners (ref `wxdpjdymlbgnjdqkvrnd`, Frankfurt, Postgres 17),
  wspólny z narzędziem „ruch w skrzynkach” (tabele `ruch`, `przebiegi` - nie ruszać).
- Wszystkie 14 migracji kasy zastosowane przez konektor Supabase (MCP), wpisy w `schema_migrations` z sumami
  kontrolnymi plików z repo, więc `python -m kasa migrate` na tej bazie widzi stan „nic do zrobienia”.
  Zgodność z lokalną bazą sprawdzona: 45 tabel, 38 widoków, 60 funkcji `kasa_*`.
- Wszystkie tabele w `public` mają włączone RLS bez polityk: API Supabase (anon/authenticated) nie widzi
  danych kasy; kasa łączy się rolą `postgres` (bypassrls), więc dla niej nic się nie zmienia.
- Kontener Claude Code NIE dosięga bazy bezpośrednio (brak DNS do `*.supabase.co` / `*.pooler.supabase.com`).
  Dopóki użytkownik nie doda tych hostów do listy dozwolonych środowiska i nie ustawi `KASA_DATABASE_URL`
  (Session pooler, port 5432, użytkownik `postgres.wxdpjdymlbgnjdqkvrnd`), zapytania do Supabase idą przez
  konektor (`execute_sql` / `apply_migration`), a CLI kasy działa tylko na lokalnym Postgresie.
- Nowe migracje: najpierw lokalnie (`python -m kasa migrate`), potem ten sam plik przez konektor
  `apply_migration` + ręczny INSERT do `schema_migrations` (version, name, sha256 pliku).
