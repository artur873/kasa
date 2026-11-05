# Kasa - segment finansowy Spinacza 2.0

Narzędzie zarządcze dla ODM Partners: czyta faktury (KSeF), przelewy (EnableBanking), pliki historii i PDF-y z maili,
łączy przelewy z fakturami, przypisuje przychody i koszty do eventów, uczy się reguł z Twoich kliknięć i wystawia jedną
warstwę metryk dla agenta AI (MCP), dashboardu i kolejki przeglądu na telefonie. Pełny opis: `docs/BRIEFING.md`.

Stan (5.11.2026): pięć faz zbudowanych, każda z zaliczoną bramką (`docs/FAZY.md`), 599 testów, pełny potok działa na
wymyślonych danych ODM za październik 2026 (`fixtures/`). Zewnętrzne API (KSeF, EnableBanking, Vertex AI) są podłączone
jako interfejsy z implementacją `mock`; prawdziwe implementacje czekają na klucze.

## Uruchomienie od zera (lokalnie)

Wymagania: Python 3.13, PostgreSQL 16, `uv` (albo pip).

```bash
uv pip install --system -e ".[web,dev]"
```

```bash
export KASA_DATABASE_URL=postgresql://kasa:kasa@localhost:5432/kasa
python -m kasa init-db
```

Pełny potok na danych testowych (kolejność z briefingu: bank, KSeF, pliki, PDF):

```bash
python -m kasa bank ingest --company odm --from 2026-10-01 --to 2026-10-31
python -m kasa documents ksef-sync --company odm --from 2026-09-01 --to 2026-10-31
python -m kasa documents import --company odm --mapping pm_sheet fixtures/odm/internal/arkusz_pm_2026-10.csv
python -m kasa documents import-pdf --company odm fixtures/odm/email_pdfs --meta fixtures/odm/email_pdfs/index.json
python -m kasa semantics run --company odm
python -m kasa control-set import-ground-truth --company odm
python -m kasa match calibrate --company odm --control-set ground-truth-transaction
python -m kasa match run --company odm --month 2026-10
python -m kasa cost-objects discover --company odm
python -m kasa allocate run --company odm
python -m kasa queue build --company odm
python -m kasa check
```

Panel na telefonie (kolejka przeglądu, dashboard „Liczby”, etykietowanie):

```bash
python -m kasa web --host 0.0.0.0 --port 8787 --user artur
```

Agent AI przez MCP (stdio; konfiguracja dla Claude Desktop / innych klientów w `docs/AGENT.md`):

```bash
python -m kasa agent serve
```

Testy: `python -m pytest -q` (tworzy bazy testowe `kasa_*_test`).

## Mapa dokumentacji

| Temat | Plik |
| --- | --- |
| Briefing (źródło prawdy) | `docs/BRIEFING.md` |
| Plan faz i bramki | `docs/FAZY.md` |
| Decyzje (Twoje i koordynatora) | `docs/DECYZJE.md` |
| Przeglądy sędziowskie i otwarte znaleziska | `docs/PRZEGLADY.md` |
| Schemat bazy (13 migracji, strażniki KS001-KS012) | `docs/SCHEMAT.md` |
| Bank: EnableBanking, camt.053, MT940, pseudonimizacja, retencja | `docs/BANK.md` |
| KSeF, JPK, kreator importu, PDF, deduplikacja | `docs/DOKUMENTY.md` |
| Semantyka: bramka do modelu, prompty, zbiory kontrolne | `docs/SEMANTYKA.md` |
| Dopasowanie przelew ↔ faktura, kalibracja | `docs/DOPASOWANIE.md` |
| Eventy z danych i alokacja kosztów | `docs/ALOKACJA.md` |
| Kolejka przeglądu | `docs/KOLEJKA.md` |
| Reguły uczone z kliknięć, audyt | `docs/REGULY.md` |
| Metryki, agent MCP, dashboard | `docs/METRYKI.md`, `docs/AGENT.md`, `docs/DASHBOARD.md` |

## Co jest po Twojej stronie, żeby przejść z mocków na prawdziwe dane

1. **KSeF**: certyfikat „tylko odczyt” w Aplikacji Podatnika; zmienne `KASA_KSEF_PROVIDER=ksef`, `KASA_KSEF_TOKEN`
   lub `KASA_KSEF_CERT_PATH`, `KASA_KSEF_NIP` (`docs/DOKUMENTY.md`).
2. **EnableBanking**: darmowe konto, aplikacja, podpięte rachunki mBank i Millennium z rachunkiem VAT, zgoda 180 dni;
   zmienne `KASA_BANK_PROVIDER=enablebanking`, `KASA_EB_APP_ID`, `KASA_EB_PRIVATE_KEY_PATH`, `KASA_EB_SESSION_ID`
   (`docs/BANK.md`). Zapas: eksport camt.053 / MT940 z bankowości (`bank import-camt`, `bank import-mt940`).
3. **Model AI**: projekt Vertex AI w regionie UE; `KASA_LLM_PROVIDER=vertex`, `KASA_VERTEX_PROJECT`,
   `KASA_VERTEX_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS` (`docs/SEMANTYKA.md`). Zawsze ustaw własny
   `KASA_PSEUDONYM_SECRET` przed pierwszym prawdziwym importem (klucz pseudonimów; zmiana później = nowe kody).
4. **Eksport próbny z programu biura** za jeden miesiąc 2025 → `documents import --mapping document`.
5. **Supabase**: baza docelowa (Frankfurt); migracje są zwykłym SQL, `KASA_DATABASE_URL` wskazuje na Supabase.
6. **Zbiory kontrolne**: 600 przelewów i 150 kosztów zaetykietowanych w panelu bez podglądu propozycji; dopiero wtedy
   kalibracja daje gwarancję błędu automatu ≤ 0,5% (dziś, przy 80 etykietach, `match status` ostrzega).
7. **Pytania do agenta**: `docs/PYTANIA_AGENTA.md` (zestaw rośnie w trakcie).
