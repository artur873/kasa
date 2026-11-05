# Przeglądy sędziowskie

## Faza 1 (5.11.2026)

Blokujące, do zrobienia na początku fazy 2 (migracja 0006):
1. L0: dopuścić wyłącznie operację „purge” (payload → pusty, `purged_at`) przez dedykowaną funkcję retencyjną; retencja z Kroku 9.
2. Pseudonimizacja w L1: tabela klucza `pseudonym_key (company_id, kind, pseudonym, value_hash, value_encrypted)` i kolumny pseudonimowe w `l1_bank_transaction` (tytuł, kontrahent) wypełniane przez parser od pierwszego importu.
3. Split payment jako jedno zdarzenie na poziomie faktów: `event_group_id` w `l1_bank_transaction` nadawany przez kod (warunek kontroli prawdy nr 5).

Ważne (faza w nawiasie):
4. Kontrahenci: tabela kontrahentów + `counterparty_id` w przelewie i dokumencie (2: fakty z NIP/IBAN; 4: decyzje).
5. `awaiting_until` w `l3_transaction_status` dla zaliczki (180 dni) i karty (30 dni) (4).
6. Obserwacje reguł `l2_rule_observation` do liczenia etapów jako stanu pochodnego, w tym dormant (4).
7. Próbka audytowa 2% i wartownik: `l2_audit_sample`, `l2_sentinel` (4).
8. Konwencja: korekta w grupie dopasowań ma `amount_minor` ujemne; `difference_kind='correction'` (4).
9. `signal_strength` (A-D) i `cascade_step` na alokacji (4).
10. Członkowie kandydata na obiekt kosztowy `l2_cost_object_member` z kotwicą i siłą powiązania (4).
11. Zwrot ↔ pierwotna wpłata: `related_transaction_id` w `l3_match` lub `l3_transaction_link` (4).
12. Semantyka modelu także dla pozycji faktur i wierszy arkusza: uogólnić `l1_title_semantics` na subject_type/subject_id (3).

Drobne:
13. Trigger KS003: przy UPDATE człowieka wymagać nowego `decision_id` lub `revoked_by_decision`.
14. CHECK formatu numeru KSeF.
15. Słowa wrażliwe: rdzenie/odmiana zamiast podciągów (3).

## Faza 2 (5.11.2026)

Blokujące (przekazane do fazy 3):
1. `pseudonym.py` `_detect_persons`: nazwy produktów/ulic/firm brane za osoby; osoba tylko z dowodem (imię ze słownika, osoba z arkusza, kontrahent person, kontekst Pan/Pani/tel./PESEL).

Ważne (faza w nawiasie):
2. `bank/ingest.py`: ten sam `entry_reference`, ta sama wersja parsera, inna treść → dziś `unchanged`; powinna być nowa wersja po odcisku treści (przed pierwszym realnym pobraniem EB).
3. Anonimizacja po retencji niszczy tylko klucz; jawne dane zostają w `title_raw`, `counterparty_*_raw`, `extracted.phones`, `l1_pdf_extraction`, `l1_internal_record.fields`, `seller_name` rachunków osób → `kasa_anonymize_persons` jako jedyna ścieżka UPDATE z logiem (5).
4. Revolut: kod rozpoznaje operatora, ale nie wyciąga klienta z tytułu → `payer_hint` (3/4).
5. Blokady kartowe: zastąpienie tylko przy tej samej kwocie; `superseded_by` miesza wersję faktu ze stanem → `resolved_by_transaction_id`, status `expired` (4).
6. Kurs: bank bierze NBP z dnia operacji, przelew zagraniczny z dnia poprzedniego, faktura EUR bez `fx_rate_date` → jedna funkcja „ostatnia tabela A przed datą” (4).
7. Odczyt regexowy mocka zapisany jako `method='model'` → `code` (3).
8. `company.nip` ustawiany bez pochodzenia (3).

Drobne: 9. identyfikator zbiorczy płatności B2B od 2027 (4); 10. depseudonimizacja tylko kodów z wejścia (3); 11. test: semantyka nie czyta wierszy sensitive (3).

Status (faza 3, 5.11.2026): pkt 12 - `l1_semantics` (migracja 0008); pkt 15 - `kasa.core.sensitive` + tabela
`sensitive_term` (stemmer z odmianą polską).

## Faza 2 (5.11.2026) - punkty wykonane w fazie 3

1. BLOKUJĄCE: nadmiar „osób” w pseudonimizacji (nazwy produktów, ulic, firm) → dwa słowa z wielkiej litery są
   osobą tylko z dowodem; `pseudonym_key.basis`; test precyzji na wszystkich tytułach, nazwach i tekstach PDF
   (`tests/semantics/test_pseudonym_precision.py`). Błędne klucze w bazie `kasa` zostają (nic nie kasujemy),
   nowy kod ich nie używa i nie tworzy.
2. `l1_pdf_extraction.method` dopuszcza `code`; odczyt regexem modelu testowego jest `code`.
3. Odczyt PDF odwraca tylko kody obecne w tekście wysłanym do modelu.
4. `company.nip_evidence` - źródło, `raw_id` i czas pierwszego ustalenia NIP-u firmy.
5. Semantyka nigdy nie czyta wierszy z `sensitive = true` (test dla modelu lokalnego i „zewnętrznego”).
6. `extracted.phones` w `l1_bank_transaction` bez jawnego numeru (tylko pseudonim); `PARSER_SCHEMA_VERSION = 2`.


## Faza 3 (5.11.2026)

Blokujące (osobny agent poprawek, równolegle z 4A/4B):
1. Adres bez prefiksu „ul.” nie jest pseudonimizowany („Kwiatowa 7, 05-500 Piaseczno”); dodać wzorce PL (nazwa + numer + kod pocztowy / m. / lok.) i zagraniczne (Street/Quay/Suite).
2. „PESEL 44051401359 Marek Nowak”: nazwisko zostaje jawne, bo maska PESEL zaburza okno osoby; maska = granica, tokeny wielkimi literami poza oknem.
6. „Jan Kowalski Kwiatowa 7” → osoba pochłania ulicę, niestabilny kod; okno osoby = imię + 1-2 nazwiska, token przed liczbą = adres.

Ważne: 3. stemmer 4-literowy: fałszywe trafienia (zdrowa żywność, Szpitalna 5, Terapia sp. z o.o.) → kontekst osoby lub pełne słowo; przycisk „to nie jest wrażliwe” w kolejce (4). 4. `l1_semantics.input_pseudonymized` bez terminu wbrew 90 dniom → tylko skrót w L1 (5). 5. `sensitive` liczone tylko z tytułu, nie z nazwy kontrahenta; blokada nie jest trwałą flagą → ponawiana co przebieg (4). 7. terms/context ładowane raz na przebieg (4/5). 8. `shown_at` nadpisywany każdym GET → zaniżony czas decyzji (4).
Drobne: 9. błędy walidacji pydantic logują fragment odpowiedzi modelu (`input`). 10. wygasanie payloadów tylko ręcznie (5). 11. TLS dla panelu w docs. 12. bramka 3 nie sprawdza adresów.
