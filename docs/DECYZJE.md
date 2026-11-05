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
