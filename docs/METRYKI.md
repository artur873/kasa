# Warstwa metryk (faza 5)

Krok 8 briefingu w kodzie: „przychód”, „należności”, „marża” są zdefiniowane **raz**, z wersją, i tę samą
definicję czytają agent (MCP), dashboard i eksport. Dwie marże są niemożliwe technicznie - obie ścieżki wołają ten
sam rejestr (`kasa.core.metrics.registry.compute`), a test bramki 5 porównuje ich liczby.

Kod: `kasa/core/metrics/` (`base.py` - klasa metryki i wynik, `registry.py` - rejestr, wersje i skróty kodu,
`state.py` - stan danych, `definitions/*.py` - definicje), lista skrótów `kasa/core/metrics/definitions.lock.json`,
tabela `metrics.metric_definition` (migracja `0012_phase5_metrics.sql`), CLI `kasa/cli/metrics.py`, testy
`tests/metrics/`.

```
definitions/*.py (klasa: key, version, compute) ──► registry.compute(conn, firma, klucz, okres|dzień, wymiary, filtry)
        │ SHA-256 kodu definicji                        │ 1. verify: (klucz, wersja, skrót) = metrics.metric_definition
        ▼                                               │ 2. compute: fakty L1 + decyzje L3 (+ sugestie L2 osobno)
metrics.metric_definition  ◄── metrics sync (właściciel) │ 3. metadane: wersja, stan danych, udział niepotwierdzonych,
definitions.lock.json      ◄── metrics lock             │    pochodzenie, ostrzeżenia kontroli prawdy, pułapki
                                                        ▼
                     dashboard (rola właściciela, tabele public)  =  agent MCP (rola kasa_agent_ro, lustra evidence)
```

## 1. Metryka i wynik

Metryka to klasa (`kasa.core.metrics.base.Metric`): `key`, `version`, `name_pl`, `description_pl`, ziarno
(`company` / `cost_object` / `counterparty` / `document` / `day`), jednostka (`pln_minor` / `days` / `ratio` /
`count`), tryb okresu (`month` - okres wymagany, domyślnie miesiąc stanu danych; `as_of` - dzień, domyślnie dzień
stanu danych; `optional`; `none`), dozwolone wymiary i filtry, **pułapki rozstrzygane definicją** (`traps_pl`) i
`compute(ctx, period, as_of, dimensions, filters)`. Nieznany wymiar albo filtr = błąd (nie ma „formuły”).

Każdy wynik (`MetricResult.as_dict()`) niesie to, czego briefing wymaga od każdej odpowiedzi:

| Pole | Co znaczy |
| --- | --- |
| `metric` | `key`, `version`, `ref` (`receivables_open@1`), jednostka, ziarno, `code_sha256` |
| `value`, `value_pl`, `values`, `detail` | liczba główna, wiersze (pozycje, obiekty, kubełki), szczegóły; każda kwota `*_minor` ma obok `*_pl` („12 345,67 zł”, waluta dokumentu dla `*_doc_minor`) - agent cytuje, nie formatuje |
| `as_of`, `period` | dzień albo okres odpowiedzi |
| `data_state` | dzień danych (ostatni zaksięgowany dzień banku - nie „dziś”), początek historii banku, ostatni import każdego kanału, czy kontrole prawdy w okresie są zielone, liczba czerwonych |
| `unconfirmed_share`, `unconfirmed_minor` | udział kwot, które są tylko w otwartych sugestiach (L2), wobec kwot z decyzji i faktów - „30% kosztów jest jeszcze w sugestiach” |
| `provenance` | liczba decyzji (L3) i sugestii (L2) za liczbą + do 25 identyfikatorów każdego rodzaju (`explain_fact` pokazuje łańcuch) |
| `warnings` | czerwona kontrola prawdy w okresie, okres bez policzonych kontroli, okres dłuższy niż dane, uwagi definicji |
| `traps_pl`, `metrics_used` | pułapki definicji; użyte metryki z wersjami (np. DSO używa `receivables_open@1`) |

Stan danych: `truth_check_result` - najnowszy wynik każdej kontroli (i zakresu) przecinającej okres; czerwona =
ostrzeżenie każdej metryki z tego okresu; brak wyników = ostrzeżenie „kontrole prawdy nie liczone” (kontrole zapisuje
`python -m kasa check` i przebiegi potoku).

## 2. Metryki startowe (wersja 1; `margin_accrual` w wersji 3)

| Klucz | Co liczy | Pułapka rozstrzygnięta definicją | Na danych testowych (31.10.2026, po kliknięciu kolejki) |
| --- | --- | --- | --- |
| `revenue_accrual_net` | faktury sprzedaży minus korekty, po dacie sprzedaży, netto PLN; wymiar obiekt | zaliczka = 0, proforma = 0, okres usługi po dniach, ROZ = całe zamówienie | 219 189,06 zł (= GT) |
| `revenue_cash_gross` | wpływy dopasowane decyzją do faktur sprzedaży, po dacie księgowania | w tym zaliczki i obie nogi MPP; duplikat i zwrot to nie przychód; sugestia osobno | 201 652,50 zł (= GT), zaliczki 29 520,00 |
| `receivables_open` | brutto łańcucha (faktura + korekty) minus wpłaty z decyzji, na dzień; kubełki niewymagalne / 1-30 / 31-60 / 61-90 / >90, sortowane po kwocie | brak terminu → wiek od wystawienia, `due_date_missing`; dokument tylko z JPK sprzed wyciągów nie jest należnością (lista osobno) | 75 565,00 zł, 4 faktury, wiek i kubełki = GT |
| `payables_open` | to samo dla zakupów, rachunków osób, paragonów, faktur zagranicznych | remis: obie faktury otwarte, przelew w `unresolved_payments` | 42 102,00 zł (= GT) |
| `dso_90` | należności / sprzedaż brutto z 90 dni × 90 | jedna definicja: sprzedaż = brutto faktur sprzedaży (z korektami i zaliczkowymi, bez proform), należności = `receivables_open@1` | 19,7 dni |
| `margin_accrual` (@3) | przychód minus koszt przypisany, cały obiekt; UNASSIGNED osobno; kandydat bez karty - bez liczby | UNASSIGNED to decyzja, nie część marży; „w pełni obciążona” to inna metryka; faktura zbiorcza wg zatwierdzonego podziału; filtr bez obiektu = odmowa, nie „0,00 zł” | 5 eventów co do grosza (= GT) |
| `margin_fully_loaded` | wynik obiektu w okresie minus koszt UNASSIGNED okresu rozdzielony wg przychodu obiektów | konwencja zarządcza, nigdy jeden KPI z marżą memoriałową; przychód UNASSIGNED nie jest kosztem ogólnym | Gala 14 238,90 zł |
| `cash_contribution` | wpływy - wypływy okresu przypisane obiektom; UNASSIGNED, niesparowane (z detalem), transfery własne | suma = zmiana sald z konstrukcji, różnica kontrolna 0,00; transfer własny bez decyzji = niesparowane (`unmatched_own`) | obiekty 38 660,50, UNASSIGNED -61 207,95, niesparowane -4 800,00 = -27 347,45 |
| `company_result` | raport firmy: wiersze memoriałowe i kasowe, różnice kontrolne | dwa ujęcia, nigdy jeden KPI | wynik memoriałowy 57 543,91; kasowo -27 347,45; różnice 0,00 |
| `costs_without_document` | wypływy > 100 zł bez faktury i dokumentu wewnętrznego (decyzja albo sugestia „bez dokumentu”) | sygnał „brakuje papieru”, nie P&L; podtyp obowiązkowy; „do wyjaśnienia” osobno; wynagrodzenia osób i tytuły wrażliwe tylko zbiorczo | 63 012,15 zł (= GT; 4 wynagrodzenia zbiorczo 23 838,68, komornik zbiorczo 1 250,00) |
| `allocation_coverage` | udział kwot memoriałowych (przychód + koszt) przypisanych obiektom; UNASSIGNED; „jeszcze nie patrzyłem” i w tym w sugestiach | UNASSIGNED (decyzja) ≠ brak decyzji; sugestia ≠ przypisanie | 93,3% obiekty, 6,7% UNASSIGNED, 0% bez decyzji |
| `account_balances` | saldo każdego rachunku (CLBD banku, inaczej saldo po ostatniej operacji), VAT osobno, dostępne z blokadami | rachunek VAT osobno; blokady kartowe poza bilansem | 160 401,26 zł (w tym VAT 4 465,00) |
| `cash_flow` | wpływy i wypływy okresu z transferami własnymi i bez | zmiana sald = wpływy + wypływy; transfery własne netto 0 | per rachunek = GT |
| `data_state` | dzień danych, importy, kontrole prawdy; wartość = liczba czerwonych | dzień danych ≠ dziś | 0 czerwonych |
| `queue_state` | otwarte karty kolejki (bez wartowników) wg klasy, co blokuje liczby | kolejka to sugestie, liczby nie ruszają się przed kliknięciem | 21 kart (alokacje dokumentów z JPK) |

Jedna implementacja: sześć metryk (`company_result`, `margin_accrual`, `margin_fully_loaded`, `cash_contribution`,
`revenue_accrual_net`, `allocation_coverage`) to widoki **jednego** raportu `kasa.core.reports.company.company_report`
- raport firmy (`report company`) jest złożeniem tych metryk, nic nie liczy drugi raz.

Zmiany raportu w fazie 5 (`kasa/core/reports/company.py`):

* **Przegląd 4A/4B, pkt 9**: transfer własny liczy się jako „własny” tylko z decyzją w mocy (człowiek albo reguła
  wbudowana `builtin:own_transfer`); wyglądający na własny bez decyzji → „niesparowane”, detal `unmatched_own`.
* `memorial_pending`: kwoty miesiąca bez alokacji, które czekają w otwartych sugestiach rank 1 (część
  „nieprzypisanych”) - dla `allocation_coverage`.
* „Koszt jeden, dowodów wiele” po 0013: wiersz arkusza `evidence_only` w grupie dopasowania i wiersz dopasowany do
  tego samego przelewu co dokument są dowodem (`evidence_of_documents(..., person_heuristic=False)`, w
  `evidence_links` z `group_id`); heurystyka „ta sama osoba, kwota, data” tylko dla wierszy bez żadnego aktywnego
  `l3_match`, oznaczona `presumption`; kasowo wiersz `evidence_only` nie jest celem dopasowania.

## 3. Wersjonowanie: zmiana definicji = nowa wersja z testem

* Skrót: `registry.code_hash(cls)` = SHA-256 źródła klasy (i jej klas bazowych z pakietu definicji), funkcji z
  `uses` i opisu (ziarno, jednostka, wymiary, filtry, pułapki, tryb okresu).
* `python -m kasa metrics sync` (właściciel; robi to też start serwera MCP i każde żądanie dashboardu) dopisuje do
  `metrics.metric_definition` brakujące (klucz, wersja). Ten sam klucz i wersja z innym skrótem → `DefinitionChanged`:
  „definicja zmieniona bez nowej wersji”. Baza nie pozwala zmienić wiersza definicji (KS013; jedyna zmiana:
  `active → deprecated`).
* `registry.verify` przed każdą odpowiedzią: (klucz, wersja, skrót kodu, który właśnie liczy) musi zgadzać się z
  bazą - inaczej agent i dashboard odmawiają odpowiedzi tą definicją.
* `definitions.lock.json` w repozytorium: test `tests/metrics/test_registry.py` porównuje skróty bieżącego kodu z
  plikiem - zmiana kodu starej wersji czerwieni test w CI, zanim trafi do bazy. Procedura zmiany: nowa klasa z
  `version = 2` (stara zostaje do odtwarzania starych odpowiedzi), test nowej wersji, `python -m kasa metrics lock`
  (odmawia, gdy zmieniłeś kod istniejącej wersji), `metrics sync`; starą wersję można oznaczyć `deprecated`
  (odpowiedzi nią niosą ostrzeżenie).
* Przykład z tej fazy (procedura w praktyce): `margin_accrual@1` odpowiadał „0,00 zł” dla filtra, który nie pasował
  do żadnego obiektu, i dla kandydata bez karty; `@2` dodał kandydatów do listy i odmowę dla pustego filtra, `@3`
  nie podaje liczby, gdy każdy obiekt odpowiedzi czeka na kartę (`value = null`, „brak liczby”). Stare wersje są w
  kodzie i w bazie jako `deprecated` (`metrics sync` wycofuje starszą wersję, gdy jest nowsza) i odpowiadają tak jak
  wtedy (`get_metric(..., version=1)`), z ostrzeżeniem.
* Granica skrótu: obejmuje kod definicji i jej pomocników z `uses`, nie wspólne ładowanie faktów
  (`kasa/core/allocation/amounts.py`, `subjects.py`) - ich zmiany pilnują ich testy i bramki 4-5.

## 4. Dwie ścieżki, jedna liczba

Kod metryk czyta tabele nazwami bez schematu. Dashboard (rola właściciela) czyta `public`; agent (rola
`kasa_agent_ro`, `search_path = evidence, views, metrics, public`) czyta lustra `evidence.*` tych samych tabel
jednej firmy, bez surowych danych osób (szczegóły: `docs/AGENT.md`). Metryki pokazują osoby wyłącznie kodem
(`name_pseudonymized`) w obu ścieżkach, więc wynik jest identyczny - test `test_e_dashboard_and_agent_give_the_same_numbers`
(bramka 5) i `tests/metrics/test_dashboard.py`.

`metric_value_cache` (opcjonalny w zadaniu) świadomie nie powstał: każda liczba jest liczona z faktów i decyzji w
chwili pytania - pamięć podręczna byłaby drugim źródłem prawdy (z innym „na dzień”).

## 5. Komendy

```bash
python -m kasa metrics list                                   # metryki z wersjami
python -m kasa metrics show receivables_open                  # definicja, pułapki, wymiary, filtry, skrót kodu
python -m kasa metrics get receivables_open --as-of 2026-10-31
python -m kasa metrics get margin_accrual --filter cost_object="Gala ACME 2026" --period 2026
python -m kasa metrics get revenue_accrual_net --period 2026-10 --dim cost_object --json
python -m kasa metrics sync                                   # definicje do bazy (zmiana bez wersji = błąd)
python -m kasa metrics lock                                   # skróty do definitions.lock.json (tylko nowe wersje)
python -m kasa metrics proposals                              # propozycje metryk od agenta (pytania spoza warstwy)
```

Okres: `2026-10` (miesiąc), `2026` (od 1 stycznia do dnia danych), `2026-10-01..2026-10-31`. Dzień: `RRRR-MM-DD`.

## 6. Decyzje własne [5]

* Domyślny „na dzień” to dzień stanu danych (ostatni zaksięgowany dzień banku), nie data systemowa - inaczej wiek
  należności rośnie, choć nie przyszły nowe dane.
* Dokumenty widziane tylko w pliku historii (JPK, eksport biura) i wystawione przed początkiem historii banku nie są
  należnościami ani zobowiązaniami (zapłata, jeśli była, jest niewidoczna) - lista w `detail.excluded_history`
  (zgodnie z `history_only_documents` w GT). Do DSO sprzedaż z JPK wchodzi (to sprzedaż).
* „Bez dokumentu” = status końcowy `no_document` (decyzja albo sugestia rank 1); „do wyjaśnienia” liczone osobno.
* Wynagrodzenia osób (status/sugestia `no_document/salary`, tytuł „salary” do osoby bez dokumentu) i pozycje
  wrażliwe są w metrykach tylko zbiorczo - liczba się zgadza, osoby nie ma (Krok 9).
* `margin_accrual` z okresem zawęża listę obiektów do tych, których okno dat przecina okres; kwoty obiektu są zawsze
  za cały obiekt (jak raport fazy 4).
* `margin_fully_loaded` v1: koszt UNASSIGNED okresu dzielony proporcjonalnie do przychodu obiektów w okresie.
