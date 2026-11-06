# Start następnej sesji (stan na 6.11.2026)

Kod: fazy 1-5 + poprawki kanału bankowego po pierwszym prawdziwym pobraniu EB (wersja parsera 4), testy:
`python -m pytest -q` (bez sekretów produkcyjnych w env - inny `KASA_PSEUDONYM_SECRET` zmienia pseudonimy w złotym
wyniku metryk). Otwarte znaleziska: `docs/PRZEGLADY.md`.

## Pierwsze kroki w nowej sesji
1. Repo jest na GitHubie (`artur873/kasa`, gałąź `master`) - bundle nie jest już potrzebny.
2. Postgres: `service postgresql start`, rola i baza `kasa` (patrz README), `python -m kasa init-db`.
   Uwaga: `KASA_DATABASE_URL` w środowisku wskazuje Supabase (pooler, port 5432), którego kontener nie dosięga
   (timeout) - lokalnie ustaw `KASA_DATABASE_URL=postgresql://kasa:kasa@localhost:5432/kasa`.
3. Zależności: `uv pip install --system -e ".[web,dev]"`.
4. Sprawdź zmienne środowiska BEZ wypisywania wartości: KASA_PSEUDONYM_SECRET, KASA_EB_APP_ID, KASA_EB_PRIVATE_KEY,
   KASA_EB_SESSION_ID, KASA_BANK_PROVIDER.

## EnableBanking (stan 6.11.2026)
- Aplikacja „ODM Partners - wyciagi” (PRODUCTION, aktywna). Adres powrotu = webhook n8n
  `https://odmpartners.app.n8n.cloud/webhook/eb-callback-7d41c9a2`: n8n sam wymienia `code` na sesję, więc
  `session_id` bierze się z n8n → Executions tego workflow (kod z paska adresu jest już zużyty).
- Sesje (zgoda do 4.05.2027): mBank - tryb `personal` (zwykłe logowanie; `business` prowadzi do mBank Company),
  rachunek bieżący PL13 1140 2004 0000 3902 8248 2991; Bank Millennium - tryb `business`, Konto Biznes DE MINIMIS
  PL48 1160 2202 0000 0006 2200 2061. Identyfikatory sesji są w n8n i w env, nie w repo.
- Jedna sesja na proces: ingest uruchamiany osobno dla każdego banku z jego `KASA_EB_SESSION_ID`.
- Rachunków VAT nie da się wybrać w zgodzie PSD2 (ani mBank, ani Millennium) → kontrola MPP czerwona, dopóki
  nie wczytamy wyciągów z rachunków VAT (`bank import-mt940 --kind vat`). Stary import miał trzeci rachunek
  mBanku PL49 1140 2004 0000 3503 0090 4100 - prawdopodobnie VAT.
- Pobranie próbne (lokalna baza, 6.11): mBank 4410 operacji 11.2025-11.2026, Millennium 1666 operacji 08-10.2026;
  ciągłość sald zielona dla wszystkich miesięcy poza 01.2026 i 07.2026 mBanku (anomalie banku, `docs/BANK.md` rozdz. 10).
  Do Supabase jeszcze nic nie zapisano (brak połączenia z kontenera).

## Supabase - porządki 6.11.2026
- Migracja kasy 0015 (`definer_grants`) jest w repo i w bazie (15 migracji).
- Usunięte stare dane finansowe (migracja Supabase `usun_stare_dane_finansowe`): faktury, eventy, operacje i
  rachunki bankowe, wyciągi, rozliczenia, zrzut Airtable, kolejka plików i funkcje importu. Zostały: kasa, poczta
  (`glos`, `rdzen.wiadomosci/kopie/zalaczniki`, `surowe.gmail`, `meta.skrzynki/przebiegi/blokada`), `ruch`/`przebiegi`,
  `zadania`, `rdzen.kontrahenci` + aliasy.
- Do zrobienia przez użytkownika w panelu: pliki w Storage `finanse-pliki` (788) i funkcje Edge `finanse-lustro`,
  `pliki-pobierz` (konektor ich nie usuwa).

## Dalej (kolejność z briefingu)
KSeF (token tylko do odczytu, KASA_KSEF_NIP/TOKEN, domena ksef.mf.gov.pl) → eksport biura i JPK → Vertex AI
(domeny europe-west1-aiplatform.googleapis.com, oauth2.googleapis.com) → Supabase.
Do tego czasu KASA_KSEF_PROVIDER i KASA_LLM_PROVIDER = mock.

## Ustalenia z użytkownikiem
- Pracuje na Macu; nie wkleja kluczy do czatu, tylko w Environment variables środowiska „Spinacz - Finanse”.
- GitHub (artur873/kasa) podpięty do sesji Claude Code od 6.11; zmiany idą gałęzią + PR, scala użytkownik.
- Kategorie ODM: z arkusza użytkownika (w `kasa/extensions/odm/extension.yaml`).
- Pytania do agenta: `docs/PYTANIA_AGENTA.md` wystarczą na start; zestaw rośnie w trakcie.

## Supabase (stan 5.11.2026, uzupełniony 6.11)
- Baza docelowa: projekt **odm** w organizacji ODM Partners (ref `wxdpjdymlbgnjdqkvrnd`, Frankfurt, Postgres 17),
  wspólny z narzędziem „ruch w skrzynkach” (tabele `ruch`, `przebiegi` - nie ruszać) i z pocztą (schematy `glos`,
  `rdzen`, `surowe`, `meta`) oraz zadaniami PM (`zadania`) - nie ruszać.
- Migracje kasy (dziś 15) zastosowane przez konektor Supabase (MCP), wpisy w `schema_migrations` z sumami
  kontrolnymi plików z repo, więc `python -m kasa migrate` na tej bazie widzi stan „nic do zrobienia”.
  Zgodność z lokalną bazą sprawdzona: 45 tabel, 38 widoków, 60 funkcji `kasa_*`.
- Wszystkie tabele w `public` mają włączone RLS bez polityk: API Supabase (anon/authenticated) nie widzi
  danych kasy; kasa łączy się rolą `postgres` (bypassrls), więc dla niej nic się nie zmienia.
- Kontener Claude Code NIE dosięga bazy bezpośrednio: od 6.11 `KASA_DATABASE_URL` jest ustawiony (Session pooler,
  port 5432, użytkownik `postgres.wxdpjdymlbgnjdqkvrnd`) i DNS poolera działa, ale połączenie TCP na 5432 kończy się
  timeoutem (sieć środowiska przepuszcza tylko HTTPS). Do tego czasu zapytania do Supabase idą przez
  konektor (`execute_sql` / `apply_migration`), a CLI kasy działa tylko na lokalnym Postgresie.
- Nowe migracje: najpierw lokalnie (`python -m kasa migrate`), potem ten sam plik przez konektor
  `apply_migration` + ręczny INSERT do `schema_migrations` (version, name, sha256 pliku).
