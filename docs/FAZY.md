# Plan faz i bramki

| Faza | Zakres | Bramka (test w `tests/gates/`) | Status |
| --- | --- | --- | --- |
| 1 Szkielet | Migracje L0-L3 z pochodzeniem i triggerami, rejestr rozszerzeń, rozszerzenie ODM (event) i Rentease (pokój×miesiąc), CLI, runner migracji | `test_gate1`: nowe rozszerzenie (fixture „testco”) działa bez zmiany kodu rdzenia; trigger odrzuca nadpisanie decyzji człowieka przez automat | bramka zaliczona |
| 2 Pozyskanie | Bank (EB mock + camt.053), KSeF FA(3) XML (mock), JPK_V7/JPK_FA, kreator importu CSV/arkuszy PM, PDF z maili (mock LLM), deduplikacja | `test_gate2`: bilans każdego konta zamyka się co do grosza na pełnym miesiącu danych testowych; ta sama faktura z 3 kanałów = 1 dokument | bramka zaliczona (5.11: 282 testy, bilans 3 rachunków, MPP, KSeF kompletny, dedup) |
| 3 Semantyka i zbiory kontrolne | Bramka pseudonimizacji, odczyt tytułów i pozycji (LLM provider), lista słów wrażliwych, panel etykietowania zbiorów kontrolnych | `test_gate3`: żadne wywołanie LLM nie zawiera nazwiska/IBAN/telefonu z fixtures; słowo wrażliwe omija model | - |
| 4 Dopasowanie i eventy | Silnik dopasowania (6 warstw), reguły z etapami, kandydaci na eventy (Fellegi-Sunter), alokacja z kaskadą, kolejka przeglądu (API + widok mobilny) | `test_gate4`: 0 fałszywych dopasowań automatu na zbiorze kontrolnym; 0 eventów bez karty; różnica kontrolna raportu 0,00 | - |
| 5 Metryki, agent, dashboard | Warstwa metryk z wersjami, 17 narzędzi MCP, dashboard z metryk, eksport dla biura | `test_gate5`: 100% trafień na pytaniach o należności/cash/marżę, 100% poprawnych odmów poza zakresem | - |
