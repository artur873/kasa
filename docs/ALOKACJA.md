# Obiekty kosztowe i alokacja (faza 4, część B)

Ta część robi trzy rzeczy z Kroku 5 i Kroku 8 briefingu:

1. **Odkrywa kandydatów na obiekty kosztowe z danych** (ODM: event, Rentease: pokój × miesiąc) - lista
   eventów nie przychodzi z arkusza ani z Airtable (D1); Ty zatwierdzasz kandydata w karcie.
2. **Alokuje** pozycje faktur, dokumenty, wiersze arkuszy i przelewy bez dokumentu do obiektów:
   sygnały A-D, kaskada faktury zbiorczej, automat tylko dla sygnału A (D3), arkusz PM nie jest twardym
   dowodem (D2).
3. **Liczy raport firmy** w dwóch ujęciach, które nigdy nie są jednym KPI: memoriałowym i kasowym, z
   czterema wierszami i różnicą kontrolną 0,00 zł z konstrukcji.

Kod: `kasa/core/cost_objects_discovery/` (kandydaci), `kasa/core/allocation/` (alokacja),
`kasa/core/reports/` (raport i eksport), CLI `kasa/cli/allocation.py`, schemat
`migrations/0010_phase4_allocation.sql` (opis tabel: `docs/SCHEMAT.md`), hooki rozszerzeń
`kasa/extensions/odm/hooks.py`, `kasa/extensions/rentease/hooks.py`, testy `tests/allocation/`.
Rdzeń nie zna słowa „event” - test bramki 1 przeszukuje teraz cały `kasa/core/**`.

```
pozycje (l1_document_line + l1_semantics line_entities) ─┐
wiersze arkuszy (l1_internal_record + semantyka)         ├─► rekordy ─► model par (Fellegi-Sunter) ─► grupy
przelewy (tytuł po pseudonimizacji, kontrahent, data)    │                │ wagi: pair_model_weights
dokumenty bez pozycji (kontrahent + okres usługi)        ┘                ▼
                       kandydat l3_cost_object (candidate, code) + l2_cost_object_member + karta l2_suggestion
                                                          │  Ty: zatwierdź / podziel / połącz / odrzuć / na pewno różne
                                                          ▼  (każde kliknięcie = dowód → nowa wersja wag)
                                         obiekt approved ─► sygnały A-D ─► ≤ 3 sugestie alokacji na podmiot
                                                          │  automat: tylko A (builtin:allocation_explicit_identifier)
                                                          ▼  Ty: akceptuj / alternatywa / Inne…
                                         l3_allocation (signal_strength, cascade_step) ─► raport firmy
```

## 1. Kandydaci na obiekty kosztowe

### Rekordy

`kasa/core/cost_objects_discovery/records.py` czyta bieżące wersje faktów:

| Rekord | Nazwa | Klient | Okno dat | Miasto | Osoby |
| --- | --- | --- | --- | --- | --- |
| pozycja dokumentu | `candidate_name` z `l1_semantics` (cel `line_entities`) | nabywca faktury sprzedaży; dla zakupu `client_hint` rozpoznany jako znany nabywca | daty / okres z tekstu; inaczej okres usługi lub data sprzedaży dokumentu | z tekstu; dla zakupu bez nazwy - miejsce wystawienia (hotel, sala) | osoba wystawiająca rachunek (pseudonim) |
| wiersz arkusza | `candidate_name` albo `cost_object_ref` | `client_hint` | data wiersza | z tekstu | osoba (pseudonim) |
| przelew | słowa tytułu po pseudonimizacji | płacący (wpływ od znanego nabywcy), `counterparty_hint` | daty z tytułu („03.10”, „15-16.10”); inaczej data księgowania (najsłabsza) | `place` z semantyki | kontrahent-osoba (pseudonim), kody osób |
| dokument bez pozycji | - | nabywca | okres / data sprzedaży | miejsce wystawienia | - |

Tokeny: małe litery bez ogonków, lekki rdzeń polski („Gali” = „Gala”, „Krakowie” = „Kraków”), bez liczb,
pseudonimów i słów nieznaczących; kody z cyfrą („P3”, „A12”) zostają - tak nazywa się pokoje i sale.
Przelewy własne nie są rekordami; podatki, ZUS i opłaty bankowe nie wchodzą do odkrywania (wchodzą do alokacji).

### Model par (Fellegi-Sunter, `pairs.py`)

Każdą parę rekordów porównujemy na sześciu cechach; cecha brakująca po którejkolwiek stronie nic nie wnosi.

| Cecha | Poziomy | Co porównujemy |
| --- | --- | --- |
| `name` | exact / high / partial / none | nakładanie tokenów nazwy ważone rzadkością (IDF): ≥ 0,85 / ≥ 0,6 / ≥ 0,3 / mniej |
| `client` | agree / disagree | tokeny klienta (bez form prawnych) |
| `dates` | overlap / near / mid / far | dni między oknami: 0 (krótkie okna) / ≤ 2 / ≤ 10 / więcej; data księgowania nigdy „far” w obrębie miesiąca |
| `city` | agree / disagree | miasto |
| `rare_token` | yes / no | wspólny rzadki token, który nie jest klientem ani miastem |
| `persons` | shared / none | wspólne osoby z plików wewnętrznych (pseudonimy) |

Waga poziomu = `log2(m/u)`, prawdopodobieństwo „ten sam obiekt” = logistyka z priora (`__prior__`) i sumy wag.
**Żadna waga nie jest w kodzie**: `m`, `u`, prior, progi grupowania (`merge` 0,9, `min_link` 0,5, `member` 0,6,
`high` 0,9, `margin` 0,15) i parametr rzadkości są wierszami `pair_model_weights` (wersja 0 = priory migracji).

**Uczenie** (`retrain`, wołane po każdej decyzji): z dziennika decyzji (`kind = 'cost_object'`, bez cofniętych)
powstają pary oznaczone - zatwierdzenie = kotwica ↔ każdy członek „ten sam”, połączenie = „ten sam” między
stronami, podział = „ten sam” w grupie i „różne” między grupami, „na pewno różne” = „różne” między stronami.
Odrzucenie nie jest dowodem o parach („to nie jest obiekt kosztowy” ≠ „te rekordy są różne”). Dla każdego poziomu
`m = (liczba wśród par „ten sam” + pseudo × m_prior) / (pary z tą cechą + pseudo)`, `u` analogicznie na parach
„różne” (prior Dirichleta o sile `pseudo_count` = 20 par; jeden krok zliczania = „EM-lite”). Nowa wersja jest
dopisywana tylko, gdy coś się zmieniło.

### Grupowanie (`grouping.py`, `discover.py`)

1. **Kotwice**: pozycja faktury sprzedaży z nazwą własną (bez korekt i bez pozycji „na cały miesiąc”), wiersz
   z importu listy wskazujący obiekt (arkusz PM, lista czynszów), sprawa z maila (gdy archiwum zostanie
   podpięte). Rozszerzenie może zdecydować inaczej (`is_anchor_eligible` w `hooks.py`; Rentease: tylko
   wiersz listy czynszów). **Faktura zbiorcza nigdy nie jest kotwicą ani członkiem**: pozycja, której tekst
   wymienia unikalne tokeny dwóch lub więcej grup („Gdańsk, Wrocław, Łódź”), albo bez nazwy z oknem na cały
   miesiąc, które zawiera kilka grup tego samego klienta, jest pomijana (raport: „pozycje zbiorcze pominięte”).
2. **Łączenie kotwic**: aglomeracyjnie (średnie powiązanie ≥ `merge`, żadna para poniżej `min_link`). Dwa
   istniejące obiekty nigdy się nie łączą; grupy z rekordami obiektów oznaczonych „na pewno różne” nigdy się
   nie łączą (cannot-link); rekordy odrzuconego kandydata (albo pominięte przy podziale) nigdy nie zostają
   kotwicą nowego.
3. **Członkowie**: rekord dołącza do grupy, gdy średnia trzech najlepszych par z kotwicami ≥ `member` i ma
   przewagę ≥ `margin` nad drugą grupą.
4. **Kandydat** = kotwica + co najmniej jeden rekord innego typu z powiązaniem ≥ `high`. Atrybuty liczy
   rozszerzenie (`derive_cost_object_candidates`): ODM - event = klient + zwarte okno dat + miasto + nazwa
   (nazwa z pozycji sprzedaży, potem zakupu, potem arkusza; okno z dat kotwic i pozycji, najwyżej 14 dni; klient
   z faktury sprzedaży albo podpowiedzi rozpoznanej jako znany nabywca); Rentease - pokój × miesiąc.
5. Zapis: `l3_cost_object` (status `candidate`, `method = 'code'`), `l2_cost_object_member` (kotwica + członkowie
   z siłą), karta `l2_suggestion` (`kind = cost_object_candidate`, podmiot = obiekt kosztowy):

```json
{"name": "Gala ACME 2026",
 "attrs": {"name": "Gala ACME 2026", "client": "ACME Polska sp. z o.o.", "date_start": "2026-10-08",
           "date_end": "2026-10-10", "city": "Kraków"},
 "anchor": {"record_type": "document_line", "record_id": "…"},
 "members": [{"record_type": "internal_record", "record_id": "…", "link_strength": 0.9964}, …],
 "weakest_link": 0.7782, "revenue_minor": 9000000, "cost_minor": 6015640}
```

   `evidence.facts`: kotwica, liczba powiązanych rekordów wg typu, najsłabsze powiązanie, okno dat i miasto.
   Przychód i koszt karty to kwoty memoriałowe członków; wiersz arkusza będący dowodem dokumentu nie liczy się
   drugi raz.

**Idempotencja**: drugie uruchomienie na tych samych danych nic nie zapisuje; zmieniona propozycja wygasza starą
kartę (`expired`) i zapisuje nową. Zatwierdzonego obiektu odkrywanie nie zmienia (może tylko dopisać otwartych
członków).

### Decyzje o kandydatach (`decide.py`) - zawsze człowiek

| Funkcja | Co robi |
| --- | --- |
| `approve_candidate(conn, id, *, decided_by, attrs_override=None)` | status `approved`, `method = 'human'`; aliasy (nazwy członków) do `evidence.aliases`; członkowie i karta → `accepted`; `method != 'human'` → `PermissionError` (a baza i tak odmawia: `l3_cost_object_method_check`) |
| `split_candidate(conn, id, groups: list[list[member_id]], *, decided_by)` | każda grupa = nowy kandydat (`method = 'human'`, status `candidate`, własna karta); stary → `rejected`; nowe są parami „na pewno różne” |
| `merge_candidates(conn, ids, *, decided_by, into=None)` | cel = `into`, inaczej zatwierdzony, inaczej pierwszy; pozostałe → `merged` (`merged_into`), ich członkowie przechodzą do celu; werdykt „ten sam” |
| `reject_candidate(conn, id, *, decided_by, reason=None)` | status `rejected`; rekordy nie zakotwiczą nowego kandydata |
| `mark_distinct(conn, a, b, *, decided_by)` | werdykt „na pewno różne” w `cost_object_pair_decision`; odkrywanie nigdy ich nie skleja |

Każda decyzja to wiersz `l3_decision_log` z `kind = 'cost_object'` i `payload.action` =
`approve|split|merge|reject|distinct` (enum `decision_kind` z fazy 1 ma jedną wartość `cost_object`; przycisk
zapisujemy w `payload`, jak `kasa/core/cost_objects.approve`), plus `retrain`.

## 2. Alokacja

### Podmioty i „koszt jest jeden, dowodów wiele” (`subjects.py`)

Podmiot alokacji: pozycja dokumentu, dokument bez pozycji, wiersz pliku wewnętrznego, przelew bez dokumentu.
Ten sam koszt widziany trzy razy (rachunek PDF, wiersz arkusza, przelew BLIK) alokujemy i liczymy raz, wg
pierwszeństwa **dokument > rekord wewnętrzny > przelew**:

* przelew spięty `l3_match` (4A) z dokumentem albo wierszem nie jest podmiotem - pieniądze idą za alokacją
  dokumentu (wiersza);
* wiersz arkusza nie jest podmiotem ani kosztem, gdy jest dowodem dokumentu: decyzja dopasowania zapisała go w
  grupie dokumentu jako `evidence_only` (0013, powód `evidence_only`, z `group_id`), ten sam przelew jest
  dopasowany do obu (`l3_match`), albo - zanim cokolwiek dopasowano - wiersz wskazuje tę samą osobę (pseudonim)
  co rachunek osoby, na tę samą kwotę, z datą w obrębie 31 dni (konferansjer: PDF EM-05 + wiersz R08 + przelew
  = 1 800 zł raz); `evidence_of_documents(..., person_heuristic=False)` wyłącza tę ostatnią heurystykę;
* przelew jest podmiotem tylko, gdy nie ma dokumentu: status końcowy `no_document` / `to_clarify` / `matched`
  wyłącznie z innym przelewem (zwrot ↔ duplikat), albo sugestia dopasowania 4A mówi „bez dokumentu”, albo - przed
  dopasowaniem - tytuł nie niesie numeru faktury i intencją nie jest zapłata faktury. Przelew własny nigdy.

### Sygnały (`signals.py`) - tabela Kroku 5

| Siła | Sygnał | Wynik na danych testowych | Automat? |
| --- | --- | --- | --- |
| A | jawne przypisanie w pliku wewnętrznym: `cost_object_ref` = nazwa/alias zatwierdzonego obiektu z unikalnym tokenem | 19 wierszy arkusza PM | **nie** (D2): odznaka wysoka, `hard: false`; flaga `allocation.internal_record_is_hard` w `extension.yaml` włącza automat |
| A | jawny identyfikator: unikalny token nazwy obiektu (+ co najmniej dwa tokeny nazwy/miasta) w pozycji, tytule lub mailu, bez sprzeczności (daty z tekstu poza oknem > 3 dni, inny nabywca, inne miasto); unikalność liczona wśród obiektów w oknie **±90 dni** wokół dat podmiotu (zeszłoroczna „Gala ACME” nie odbiera unikalności tegorocznej; przegląd 4B pkt 8) | 21 pozycji faktur (np. „Catering - Gala ACME 2026”), tytuły BLIK „Nordis Gdansk hostessa 03.10” | **tak** (reguła `builtin:allocation_explicit_identifier`); podmiot bez żadnej daty - sugestia z odznaką wysoką i tagiem `no_date`, nigdy automat; przelew bezpośrednio - dopiero gdy dopasowanie (4A) dało mu status końcowy (bez dokumentu / do wyjaśnienia), wcześniej sugestia z tagiem `awaiting_matching` |
| A | reguła kontrahenta w etapie `auto` (4C, `kasa.core.rules.apply.applicable_rules`) | - | tak, podpisane tą regułą |
| B | oferta ↔ faktura: kwota = oferta (proforma) jednego obiektu | - | sugestia |
| B | kontrahent + okres usługi w oknie **jednego** obiektu (bez sprzecznego miasta; dla sprzedaży zgodny klient); dwa obiekty = remis w kolejce | hotel Kraków 08-10.10 → Gala, sala Wrocław 17.10 → Wrocław, paragon 24.10 → Łódź | sugestia |
| B | kaskada pozycji obejmującej kilka obiektów (niżej), korekta dziedzicząca podział | FV 10/10/2026, FV 12/09/2026 | sugestia |
| C | wspólni uczestnicy (osoba z pliku wewnętrznego obiektu); data z tytułu przelewu bez miasta | pensja PM-ki, która kupiła kwiaty na Galę | tylko podpowiedź (rank ≥ 2 obok UNASSIGNED) |
| D | sam klient (sprzedaż), sama bliskość w czasie (≤ 3 dni) - najwyżej 3 obiekty | wrześniowe faktury sprzedaży do klientów eventów | tylko zawężenie: nigdy rank 1, nigdy odznaka wysoka |
| - | brak sygnału | czynsz biura, SaaS, Orange, leasing, ZUS/US (D9) | UNASSIGNED rank 1 (decyzja-propozycja „koszt ogólny”) |

Data księgowania przelewu mówi, kiedy ruszyły pieniądze, nie kiedy była usługa - nigdy nie daje sygnału okna.

**Kaskada pozycji obejmującej kilka obiektów** (Krok 5, „faktura zbiorcza”):

1. `line_description` - opis wymienia obiekty: propozycja równa (rank 1, tylko Twoją decyzją), dalej kroki
   jako alternatywy. *FV 12/09/2026 „Roadshow Nordis 2026 (Gdańsk, Wrocław, Łódź)” → 1/3 każdy (jak w GT).*
2. Bez nazw w opisie: klient faktury + okres (np. „październik 2026”) zawiera ≥ 2 obiekty klienta →
3. `offer_match` - kwota pozycji = oferta jednego obiektu (100% tam);
4. `offer_value` - proporcjonalnie do wartości ofert; **gdy brakuje oferty dla części obiektów: propozycja
   częściowa** (udziały = oferta / kwota pozycji, np. 0,2 + 0,3, `evidence.partial = true`, fakt „reszta do
   uzupełnienia”); takiej sugestii nie da się przyjąć bez uzupełnienia (`shares`);
5. `person_days` - proporcjonalnie do osobodni z plików wewnętrznych (wiersz z typem rozliczenia z
   `allocation.person_day_settlement_types`, ODM: umowa zlecenie = 1 osobodzień). *FV 10/10/2026 „Obsługa
   eventów - październik 2026”: Gdańsk 2, Wrocław 3, Łódź 5 → 20/30/50 (jak w GT).*
6. `equal` - równo, tylko z regułą zaakceptowaną dla klienta (akcja reguły `{"kind": "allocation",
   "split": "equal"}`);
7. UNASSIGNED.

Oferty = dokumenty sprzedaży rodzaju `proforma` wskazujące jeden obiekt (w danych testowych ich nie ma; test
podaje dwie). **Korekta** dziedziczy podział faktury pierwotnej (`cascade_step = inherited`), chyba że jej opis
wskazuje obiekt (KOR 1/10/2026 „…Roadshow Nordis Gdańsk” → A Gdańsk).

### Sugestia alokacji (format kontraktu)

Maks. 3 otwarte na podmiot (rank 1-3), `score` w bazie, na karcie odznaka:

```json
proposal = {"shares": [{"cost_object_id": "…", "cost_object_name": "Roadshow Nordis Gdańsk", "share": 0.2,
                        "share_exact": "1/5", "amount_minor": 1000000}, …],
            "signal_strength": "B", "cascade_step": "person_days"}
evidence = {"facts": ["pozycja zbiorcza: 3 obiekty klienta w okresie …; osobodni z plików wewnętrznych: …"],
            "badge": "medium", "precedents": 0, "hard": false, "margin": 0.52, "tags": ["person_days"],
            "partial": false, "auto_eligible": false, "contradiction": false, "signal_strength": "B", "rule": null}
```

Udziały mają 6 miejsc po przecinku i sumują się dokładnie do 1,000000 (największa reszta: 1/3 = 0,333334 +
0,333333 + 0,333333), kwoty co do grosza (`share_exact` trzyma ułamek). Odznaka: high = score ≥ 0,95 i twardy
dowód (wyjątek: jawne przypisanie w arkuszu - high bez twardego dowodu, D2), medium ≥ 0,7, low reszta. Remis
(dwie propozycje w granicach 0,02) nigdy nie jest automatem. Drugie uruchomienie bez zmian danych nic nie
zapisuje; podmiot z alokacją w mocy nie dostaje sugestii.

### Decyzja (`decide.py`) i automat (`auto.py`)

* `apply_allocation(conn, suggestion_id, *, decided_by, method='human'|'rule', rule_id=None, rule_version=None,
  reason=None, shares=None) -> decision_id` - wiersze `l3_allocation` (jeden na obiekt, `signal_strength`,
  `cascade_step`, `amount_minor`), sugestia → `accepted`, pozostałe otwarte sugestie alokacji podmiotu →
  `rejected` (`resolved_by_decision`), wpis `l3_decision_log` `kind = 'allocation'` z udziałami w `payload`.
  Człowiek może zastąpić wcześniejszą alokację (stare wiersze → `revoked` przez nową decyzję); automat nigdy
  niczego nie zastępuje. Cel połączony (`merged`) jest rozwiązywany do obiektu w mocy; cel musi być `approved`
  (inaczej błąd, a baza i tak odmawia - KS007). Propozycja częściowa wymaga `shares` (pełnego podziału).
* `allocate_manually(conn, company_id, subject_type, subject_id, shares, *, decided_by)` - „Inne…” na karcie.
* Automat (`run_auto`): otwarta sugestia rank 1 z `evidence.auto_eligible` (sygnał A z tekstu albo reguła
  `auto`, bez sprzeczności, nie częściowa, przewaga ≥ 0,2; przelew - tylko z decyzją o statusie końcowym, żeby
  automat nie alokował bezpośrednio zapłaty, którą 4A za chwilę dopasuje do faktury) → `apply_allocation(method='rule',
  rule_id=builtin:allocation_explicit_identifier)` (reguła zasiana w 0010: `method = 'human'`,
  `created_by = 'briefing-2026-11-03'`, etap `auto`; zawieszona = automat stoi).

### Kontrola prawdy 3 (`kasa/core/checks.py`)

`check_allocation_shares(conn, company_id)` - udziały aktywnych alokacji każdego podmiotu sumują się do
1,000000 (albo podmiot nie ma alokacji); wynik do `truth_check_result` (`check_name = allocation_shares`);
pozycja „Kontrola prawdy 3: alokacje = 100%” w `python -m kasa check`.

## 3. Raport firmy (`kasa/core/reports/company.py`)

`company_report(conn, company_id, "2026-10")` - dwa ujęcia, nigdy jeden wskaźnik:

* **memoriałowo** (wynik netto z dokumentów): przychód z faktur sprzedaży po dacie sprzedaży (korekta = różnica,
  zaliczka = 0, proforma = 0, ROZ = całe zamówienie, okres usługi rozkładany po dniach - FV 4/10/2026:
  6 400 wrzesień, 5 600 październik; waluta po kursie dokumentu) minus koszt (faktury zakupu netto, rachunek osoby,
  paragon i faktura zagraniczna brutto, wiersze arkuszy), rozdzielone wg alokacji w mocy;
* **kasowo**: każdy zaksięgowany przelew wszystkich rachunków w miesiącu: przelew → dopasowanie (`l3_match`) →
  dokument → jego alokacja (wg wagi pozycji brutto); wiersz arkusza dopasowany do przelewu → jego alokacja;
  przelew bez dokumentu → alokacja bezpośrednia; przelew bez niczego = „niesparowane (jeszcze nie patrzyłeś)”.
  Gdy przelew ma dopasowania do dokumentu i do wiersza, liczy się dokument (wiersz to dowód). Nogi MPP to dwa
  przelewy dopasowane do jednej faktury (każdy liczony raz); transfery własne (i przeksięgowanie z rachunku VAT)
  mają własną linię, netto 0 w miesiącu.

| Wiersz | memoriałowo | kasowo |
| --- | --- | --- |
| suma obiektów kosztowych | wynik przypisany obiektom | wpływy - wypływy przypisane obiektom |
| UNASSIGNED | koszt ogólny (decyzja) | j.w. |
| nieprzypisane / niesparowane | bez alokacji | bez dopasowania i bez alokacji + dopasowane do dokumentu bez alokacji |
| kontrola | wszystkie dokumenty miesiąca | zmiana sald wszystkich rachunków (salda z banku, inaczej suma operacji) |
| **różnica kontrolna** | **0,00** | **0,00** |

Różnica jest zerem z konstrukcji: każda kwota jest dzielona co do grosza (największa reszta) na dokładnie jeden
wiersz. Gdy salda z banku nie zgadzają się z sumą operacji, raport mówi o tym w uwagach (i różnica nie jest
zerem - to błąd danych, kontrola prawdy 1).

Per obiekt: przychód, koszt i marża memoriałowo (**cały obiekt**, także koszty sprzed miesiąca, np. wrześniowy
projekt graficzny roadshow), cash contribution miesiąca i udział kwot jeszcze w otwartych sugestiach rank 1
(„30% kosztów w sugestiach”). ZUS i PIT od umów cywilnoprawnych → UNASSIGNED (D9).

`kasa/core/reports/export.py` - CSV dla biura (średnik, przecinek dziesiętny, UTF-8 z BOM): dokumenty miesiąca
i wiersze wewnętrzne będące kosztem, z kwotami, kwotą memoriałową, zapłatą (aktywne dopasowania), statusem
płatności, alokacją i jej statusem (człowiek / reguła / w sugestiach / brak). Osoby tylko jako pseudonimy.

## 4. Komendy

```bash
python -m kasa cost-objects discover --company odm [--dry-run]     # kandydaci z danych (idempotentnie)
python -m kasa cost-objects list --company odm [--status candidate|approved|rejected|merged|all]
python -m kasa cost-objects show <id>                               # karta: fakty, rekordy, id członków
python -m kasa cost-objects approve <id> [--set name='Gala ACME 2026'] [--by artur]
python -m kasa cost-objects reject <id> [--reason …]
python -m kasa cost-objects merge <id> <id> [--into <id>]
python -m kasa cost-objects split <id> --group <członek>,<członek> --group <członek>
python -m kasa cost-objects distinct <id> <id>                      # na pewno różne
python -m kasa cost-objects retrain --company odm                   # przelicz wagi modelu par
python -m kasa allocate run --company odm [--dry-run] [--no-auto]   # sugestie + automat (tylko A)
python -m kasa allocate status --company odm
python -m kasa report company --company odm --month 2026-10 [--json]   # kod wyjścia 1 = różnica kontrolna ≠ 0
python -m kasa report export --company odm --month 2026-10 [--out biuro_2026-10.csv]
```

Identyfikator można podać skrótem (min. 6 znaków). `--by` = kto decyduje (domyślnie użytkownik systemu).

## 5. Wyniki na danych testowych (`tests/allocation/`)

**Kandydaci** (`cost-objects discover`): 156 rekordów, 27 kotwic → 5 kandydatów = 5 eventów z
`ground_truth.json`, nazwa, klient, daty i miasto identyczne (nazwa z pozycji: „Gali ACME 2026” i „Gala ACME
2026” to ten sam token, wygrywa częstsza forma). Łódź nie ma pozycji sprzedaży z nazwą (jedyny przychód to
faktura zbiorcza) - kotwicą są wiersze arkusza „Nordis Łódź”, nazwa „Roadshow Nordis Łódź” z pozycji zakupu.
Faktura zbiorcza FV 10/10/2026 i projekt FV 12/09/2026 (trzy miasta) - pominięte jako zbiorcze; trzy roadshow
tego samego klienta z różnymi nazwami i datami - trzy osobne kandydaty.

| Event (GT) | Kandydat | Rekordy | Najsłabsze powiązanie |
| --- | --- | --- | --- |
| EV-01 Gala ACME 2026 | Gala ACME 2026, ACME Polska, 08-10.10, Kraków | 29 | 0,78 (przelew za catering) |
| EV-02 Roadshow Nordis Gdańsk | identyczny, 03.10, Gdańsk | 8 | 0,90 |
| EV-03 Roadshow Nordis Wrocław | identyczny, 17.10, Wrocław | 9 | 0,98 |
| EV-04 Roadshow Nordis Łódź | identyczny, 24.10, Łódź | 11 | 0,97 |
| EV-05 Helios Partner Summit 2026 | identyczny, 15-16.10, Warszawa | 13 | 0,72 (umowa o dzieło) |

**Alokacja** (po zatwierdzeniu pięciu kandydatów i dopasowaniach z GT): 107 podmiotów, automat 21 (wszystkie
zgodne z GT, 0 fałszywych; żaden wiersz arkusza), pozostałe 65 kliknięć = sugestia rank 1 (0 alternatyw, 0
„Inne…”); 19 dokumentów historycznych z JPK (wrzesień) i 2 przelewy do wyjaśnienia zostają w kolejce.

**Marże memoriałowe = ground truth co do grosza** (przychód / koszt / marża / cash contribution):

| Event | Przychód | Koszt | Marża | Cash |
| --- | --- | --- | --- | --- |
| Gala ACME 2026 | 90 000,00 | 73 036,40 | 16 963,60 | 27 859,20 |
| Roadshow Nordis Gdańsk | 16 504,06 | 10 458,54 | 6 045,52 | -180,00 |
| Roadshow Nordis Wrocław | 21 000,00 | 16 358,54 | 4 641,46 | -7 965,00 |
| Roadshow Nordis Łódź | 25 000,00 | 18 577,20 | 6 422,80 | -10 833,20 |
| Helios Partner Summit 2026 | 53 085,00 | 39 908,00 | 13 177,00 | 29 779,50 |

**Raport kasowy października**: obiekty 38 660,50 + UNASSIGNED -61 207,95 + niesparowane -4 800,00 = zmiana
sald -27 347,45; różnica kontrolna 0,00 - zarówno z dopasowaniami przez `kasa.core.matching.decide.apply_match`
(4A), jak i wstawionymi bezpośrednio, i bez żadnych dopasowań. Przychód memoriałowy października 219 189,06.

## 6. Decyzje własne [4B]

* Kotwicą może być wiersz importu listy (arkusz PM) - inaczej EV-04 nie miałby kotwicy; lista eventów dalej
  nie przychodzi z arkusza (D1): każdy kandydat czeka na kartę.
* Pozycja wymieniająca kilka obiektów (projekt dla trzech miast) → rank 1 podział równy (`line_description`,
  tylko Twoją decyzją), rank 2 osobodni; faktura zbiorcza bez nazw → rank 1 osobodni. Zgodnie z
  `suggested_allocation` w GT.
* Osobodzień = wiersz arkusza z typem rozliczenia z konfiguracji rozszerzenia (ODM: umowa zlecenie), bez
  czytania „2 dni” z uwag (zgodnie z `person_days_pm_sheet` w GT).
* Odrzucenie kandydata nie uczy modelu „różne”; blokuje rekordy jako kotwice.
* Dowód „ten sam koszt” bez dopasowań 4A: ta sama osoba (pseudonim) + kwota + data ±31 dni, tylko rachunek osoby.
* Przelew z datą w tytule bez miasta - tylko podpowiedź C; data księgowania nigdy nie jest sygnałem.
* Marża per obiekt w raporcie miesięcznym liczona za cały obiekt (jak w GT: wrześniowe koszty roadshow), wiersze
  memoriałowe - tylko miesiąc.
* `kind = 'cost_object'` z `payload.action` zamiast nowych wartości enumu `decision_kind`.
* Ograniczenia słowa „event” w rdzeniu: `kasa/core/providers/bank.py` (domyślny katalog danych testowych
  wybierany bez nazwy firmy) i `kasa/core/documents/fa3.py` (klucz `start_trigger` zamiast `from_event` dla
  `ZdarzeniePoczatkowe`) - poprawione, żeby test bramki 1 objął cały `kasa/core/**`.

## 7. Ograniczenia (świadome)

* Grupowanie jest kwadratowe (pary rekordów); przy roku danych trzeba będzie blokować pary po kliencie/miesiącu.
* Sprawy z maili (oferty z datą i miejscem) nie są jeszcze importowane - typ `email_case` czeka.
* Oferty w kaskadzie = proformy; prawdziwy import ofert z maili poda je bez zmian w kaskadzie.
* Kurs przelewu walutowego na rachunku walutowym nie jest przeliczany na PLN w raporcie kasowym (wszystkie
  rachunki testowe są w PLN).
* Precedensy na karcie liczą decyzje człowieka dla tego samego kontrahenta i obiektu; zmiana precedensów
  odświeża otwarte sugestie (wygaszenie + nowe) przy następnym `allocate run`.
