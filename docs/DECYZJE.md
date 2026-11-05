# Rejestr decyzji

Decyzje użytkownika (z briefingu, daty 2.11 i 3.11.2026) oraz decyzje koordynatora podjęte autonomicznie (oznaczone [K]).

| Nr | Data | Decyzja |
| --- | --- | --- |
| D1 | 2.11 | Lista eventów nie przychodzi z Airtable ani arkusza; powstaje z danych, użytkownik zatwierdza kandydatów w karcie. |
| D2 | 2.11 | Arkusz PM to dokument wewnętrzny, ale nie twardy dowód alokacji, dopóki nie dojrzeje reguła. |
| D3 | 2.11 | Automat alokacji tylko dla sygnałów A albo dojrzałej reguły kontrahenta; reszta sugestia. |
| D4 | 2.11 | Ryzyko: EnableBanking może wyłączyć darmowy tryb; zapas camt.053 / MT940. |
| D5 | 2.11 | Dostawca modeli: Google Vertex AI w regionie UE (Gemini i Claude pod jedną umową). |
| D6 | 2.11 | Retencja: rekordy umowa + 1 rok, potem anonimizacja osób; agregaty zostają. |
| D7 | 3.11 | Reguła `auto` bez limitu kwotowego. |
| D8 | 3.11 | Procent pewności w bazie, nie na karcie kolejki; odznaka wysoka/średnia/niska. |
| D9 | - | ZUS i PIT od umów cywilnoprawnych w v1 → UNASSIGNED; arkusz PM ma pole „typ rozliczenia” pod v2. |
| K1 | 5.11 | [K] Stack: Python 3.13 + PostgreSQL 16 lokalnie (zgodny z Supabase), bez ORM, SQL w migracjach. |
| K2 | 5.11 | [K] Brak dostępu do PROJEKT-FIN.md i repozytorium Spinacza: Kasa powstaje jako samodzielne repo `kasa/`, kontrakt odtworzony z briefingu w `CLAUDE.md`. |
| K3 | 5.11 | [K] Brak API (EB, KSeF, Vertex): każdy dostawca ma interfejs + implementację `mock` na danych z `fixtures/`; prawdziwa implementacja dopisywana, gdy użytkownik poda klucze. |
| K4 | 5.11 | [K] Faza „Szkielet” z tabeli faz nie istnieje w repo, więc budujemy ją jako pierwszą (briefing liczy fazy 0-1 jako research i kontrakt). |
| K5 | 5.11 | [K] Kody strażników bazy: KS001 tylko INSERT (L0), KS002 zakaz DELETE/TRUNCATE na wszystkich półkach, KS003 decyzja człowieka nienaruszalna dla automatu, KS004 reguła poza `auto` nie pisze, KS005-KS007 spójność podmiotów, firm i obiektów kosztowych. |
| K6 | 5.11 | [K] W L3 piszą tylko `human` i `rule`; wyjątki: kandydat na obiekt kosztowy i kandydat na regułę (status candidate) oraz systemowy UNASSIGNED (tworzony triggerem per firma). |
| K7 | 5.11 | [K] `l3_match` może wskazywać wiersz arkusza PM (`internal_record_id`) zamiast faktury: BLIK dla hostessy ma dowód w dokumencie wewnętrznym. |
| K8 | 5.11 | [K] Normalizacja numeru faktury usuwa też zera wiodące w segmentach liczbowych („FV 07/10/2026” = „FV 7/10/2026”). |
| K9 | 5.11 | [K] Etap `dormant` reguły liczony jako stan pochodny (12 mies. bez trafienia), nie zapisywany automatem; do rozstrzygnięcia w fazie 4. |
| K10 | 5.11 | [K] Split payment w faktach: kolumny `mpp_group_id`/`mpp_role` (main/vat_leg/vat_rebooking) w `l1_bank_transaction`; nazwa bez słowa „event”, bo rdzeń go nie zna. |
| K11 | 5.11 | [K] Retencja L0: jedyna dozwolona zmiana to `kasa_purge_raw` (payload pusty, sha256 zostaje, wpis w `l0_purge_log`); zwykły UPDATE nadal KS001. |
| K12 | 5.11 | [K] Pseudonimizacja: `pseudonym_key` z HMAC (env `KASA_PSEUDONYM_SECRET`) i pgcrypto; kody stabilne per wartość; klucz można zniszczyć (`shredded_at`) = anonimizacja po retencji. Model dostaje wyłącznie tekst po pseudonimizacji; mock LLM nie opuszcza maszyny. |
| K13 | 5.11 | [K] Kontrahenci jako fakty L1 (`l1_counterparty`, `kasa_upsert_counterparty`): kolejność rozpoznania NIP → zagraniczny id → IBAN → nazwa; z banku confidence 0,3-0,4. |
| K14 | 5.11 | [K] Deduplikacja dokumentów: KSeF > PDF > PDF/JPK > CSV; pewniejszy kanał lub nowszy parser tworzy nową wersję dokumentu (`superseded_by`), obserwacje zostają; niezgodna data/kwota = nowy dokument + sugestia `duplicate`. |
| K15 | 5.11 | [K] Dodatkowe wartości enumów: `document_kind.bill` (rachunek osoby), `op_type` direct_debit/foreign_transfer/interest. |
