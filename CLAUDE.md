# Kasa - segment finansowy Spinacza 2.0

Narzędzie zarządcze (nie księgowe): czyta faktury (KSeF), przelewy (EnableBanking), pliki historii (JPK, arkusze PM) i PDF-y,
łączy przelewy z fakturami, alokuje do obiektów kosztowych (ODM: event; Rentease: pokój×miesiąc), wystawia warstwę metryk
dla agenta AI, dashboardu i kolejki przeglądu. Pełny opis: `docs/BRIEFING.md` (źródło prawdy), decyzje: `docs/DECYZJE.md`.

## Stack
- Python 3.13, PostgreSQL 16 (docelowo Supabase, region Frankfurt). psycopg 3, pydantic 2, pytest. Brak ORM: SQL w `migrations/` i w repozytoriach.
- Lokalna baza: `postgresql://kasa:kasa@localhost:5432/kasa` (env `KASA_DATABASE_URL`). Testy używają bazy `kasa_test` (tworzona automatycznie).
- Uruchamianie: `python -m kasa <komenda>` (CLI). Testy: `pytest -q`.
- Migracje: `migrations/NNNN_nazwa.sql`, idempotentny runner `python -m kasa migrate`. Nigdy nie edytujemy zastosowanej migracji; dopisujemy nową.

## Niezmienne zasady (z briefingu)
1. Cztery półki: `l0_*` surowe (tylko INSERT, SHA-256), `l1_*` fakty (parser/model, wersjonowane, stare oznaczone `superseded_by`),
   `l2_*` sugestie (status: open/accepted/rejected/expired), `l3_*` decyzje (tylko człowiek lub reguła w statusie `auto`).
   Decyzję człowieka może zastąpić tylko decyzja człowieka: pilnuje tego baza (trigger), nie kod aplikacji.
2. Każdy wiersz L1-L3 ma pochodzenie: `source`, `method` (code/rule/model/human), `confidence`, `evidence` (jsonb), `schema_version`, `prompt_version`, `created_at`.
3. Nic nie kasujemy. Cofnięcie = nowa decyzja typu `revoke`.
4. Rdzeń nie wie, co to event. Obiekt kosztowy definiuje rozszerzenie (`kasa/extensions/<firma>/`), rdzeń operuje na `cost_object`.
5. Kwoty w groszach (bigint) + waluta; faktura walutowa trzyma kwotę w walucie jako główną, PLN jako pochodną z kursem.
6. Automat zamyka dopasowanie tylko gdy naraz: pewność ≥ 0,995, twardy dowód, wyraźna przewaga nad drugim kandydatem. Inaczej kolejka.
7. Model AI nigdy nie widzi listy faktur przy opisie tytułu; widzi tekst po pseudonimizacji. Bramka pseudonimizacji nie działa → wywołania nie ma.
8. Agent: tylko odczyt metryk/widoków/dowodów + dopisywanie sugestii. Brak narzędzi "zatwierdź/zapłać/wyślij".
9. Kontrole prawdy po każdym przebiegu: ciągłość sald, pokrycie dopasowań, alokacje = 100%, kompletność KSeF, MPP.

## Dostawcy zewnętrzni (API dodaje użytkownik w trakcie)
Każdy dostawca ma interfejs w `kasa/core/providers/` i dwie implementacje: `mock` (wymyślone dane z `fixtures/`) i prawdziwą
(EnableBanking, KSeF, Vertex AI). Wybór przez env: `KASA_BANK_PROVIDER=mock|enablebanking`, `KASA_KSEF_PROVIDER=mock|ksef`,
`KASA_LLM_PROVIDER=mock|vertex`. Brak kluczy = mock, nigdy wyjątek przy starcie.

## Konwencje
- Język kodu: angielski (nazwy, komentarze). Teksty dla użytkownika i dokumentacja: polski.
- Nie dopisujemy identyfikatorów modeli AI do commitów.
- Każda faza ma bramkę opisaną w `docs/FAZY.md`; test bramki w `tests/gates/`.
