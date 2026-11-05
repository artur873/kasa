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
