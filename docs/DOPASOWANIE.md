# Dopasowanie przelew ↔ faktura (faza 4, część A)

Krok 4 briefingu w kodzie: kasa łączy każdy zaksięgowany przelew z fakturą (albo wierszem arkusza PM, albo innym
przelewem), warstwami od najtańszej i najpewniejszej do człowieka. Żadna warstwa nie zamyka sprawy sama: zamyka
**automat** (gdy naraz spełni trzy warunki) albo **Ty** w kolejce.

Kod: `kasa/core/matching/` (`data.py` przygotowanie, `engine.py` warstwy i ranking, `weights.py` wagi i progi,
`fx.py` kurs, `suggestions.py` zapis sugestii, `auto.py` automat, `decide.py` decyzja, `calibrate.py` kalibracja,
`explain.py` wyjaśnienie, `run.py` jeden przebieg), CLI `kasa/cli/matching.py`, schemat
`migrations/0009_phase4_matching.sql`, kontrola prawdy 2 na końcu `kasa/core/checks.py`, testy `tests/matching/`.

```
przelewy (booked, bez decyzji)          faktury (bieżące wersje, saldo z aktywnych dopasowań i korekt)
        │                                         │
        ▼  engine.evaluate  (nic nie zapisuje; chronologicznie, twardy kandydat rezerwuje fakturę)
  1. przygotowanie: grosze, numery, kontrahent po NIP/IBAN/nazwie, transfer własny, nogi MPP = 1 zdarzenie
  2. twarde identyfikatory: numer KSeF, blok MPP (numer + NIP), numer faktury w tytule (kierunek!)
  3. kontrahent + kwota + czas: okno -30..+90 dni, dokładna kwota, 2-6 faktur co do grosza, nadpłata,
     duplikat, zwrot, korekta (wiersz ujemny), zaliczka, waluta (różnica „kurs”), tolerancja (1 faktura)
  4. semantyka tytułu (model, zapisana raz; model nie widzi faktur): intencja, okres, osoby
  5. arkusz PM: osoba / telefon BLIK / kwota / data → wiersz wewnętrzny (D2: nigdy twardy)
  6. reguły nauczone (4C, importlib) + reguły wbudowane builtin:* (w kodzie)
        │   każdy kandydat: pozycje + sygnały (waga, fakt po polsku) + twardy? + reguła
        ▼  ranking: surowy wynik → prawdopodobieństwo → maks. 3, remis → „do wyjaśnienia” pierwsze
  l2_suggestion (kind = match, format kontraktu)   ──►   auto.py: trzy warunki + reguła w etapie auto
                                                            │
                                                            ▼  decide.apply_match (method = rule | human)
                                      l3_decision_log + l3_match (group_id) + l3_transaction_status
```

## 1. Warstwy

| Warstwa | Co robi | Sygnały (tagi) |
| --- | --- | --- |
| 1. Przygotowanie | Kwoty w groszach ze znakiem; numery faktur znormalizowane (`extracted.invoice_numbers` z wariantami `alt`); kontrahent przelewu ↔ strona faktury: ten sam `counterparty_id`, **NIP** (także z bloku MPP), **IBAN** (rachunek z faktury albo z kontrahenta), ta sama nazwa bez form prawnych („Notion Labs, Inc.” = „NOTION LABS INC”), nazwa podobna albo nazwa z tytułu (Revolut); inny NIP = konflikt. Transfer własny (`op_type = own`) to osobna hipoteza. Nogi split payment (`mpp_group_id`) to **jedno zdarzenie**: podmiotem jest noga główna, kwota zdarzenia = suma nóg (56 000 + 12 880 = 68 880 = brutto faktury), noga VAT nie dostaje własnej sugestii. Blokady kartowe (`pending`) są pomijane. | `counterparty_strong`, `counterparty_name`, `counterparty_similar`, `counterparty_title_name`, `counterparty_conflict`, `own_pair` |
| 2. Twarde identyfikatory | Numer KSeF w tytule (z sumą kontrolną); blok MPP: numer INV i NIP IDC = NIP wystawcy faktury; numer faktury w tytule - **wpływ szuka faktur sprzedaży, wypływ zakupu** („FV 7/10/2026” ACME vs czynsz Biura Centrum). Kwota zgodna → twardy dowód. Kwota niezgodna → sygnał zostaje, propozycja częściowej zapłaty (`underpayment`, kwota dopasowania = kwota przelewu) albo nadpłaty, bez twardego dowodu. Kilka numerów w tytule = jedna grupa, suma co do grosza. | `ksef_number`, `mpp_block`, `invoice_number`, `amount_exact`, `underpayment`, `overpayment` |
| 3. Kontrahent, kwota, czas | Otwarte faktury kontrahenta (saldo = brutto − aktywne dopasowania; korekty z łańcucha `corrects_document_id` doliczane do faktury pierwotnej) w oknie **-30..+90 dni wokół terminu** (bez terminu: od daty wystawienia, z adnotacją). Dokładna kwota; „jedyna otwarta faktura kontrahenta na tę kwotę” (twardy dowód tylko przy kontrahencie po NIP/IBAN); 2-6 faktur sumujących się co do grosza (DFS z limitem); tolerancja **tylko przy jednej fakturze**; płatnik nierozpoznany (operator, karta): jedyny otwarty dokument na tę kwotę. Duplikat: identyczna wcześniejsza wpłata (kontrahent, kwota, tytuł) → `possible_duplicate`, nigdy automat, `related_transaction_id` do pierwotnej wpłaty albo do późniejszego zwrotu. Zwrot (tytuł „zwrot”, intencja `refund`) → pierwotna wpłata tego kontrahenta tej kwoty (data z tytułu rozstrzyga). Zaliczka: przed fakturą zaliczkową → `no_document/advance` (czeka 180 dni), gdy faktura zaliczkowa jest → dopasowanie. Waluta: kwota w walucie faktury (bank podaje ją przy karcie i przelewie zagranicznym) co do grosza + nazwana różnica „kurs”; bez kwoty w walucie: PLN wobec faktury × kurs, tolerancja 1,5% albo 50 zł. | `window`, `single_open_invoice`, `subset_sum`, `correction_chain`, `fx`, `amount_tolerance`, `amount_fx_tolerance`, `possible_duplicate`, `refunded_later`, `refund_date_in_title`, `advance_invoice`, `title_names_other_invoice` |
| 4. Semantyka tytułu | `title_semantics_current` (odczyt modelu z fazy 3; model nigdy nie widział faktur): intencja zgodna z dokumentem (zapłata, czynsz, abonament, zaliczka) +; sprzeczna (ZUS, podatek) −; `zus`/`tax`/`fee`/`rent`/`salary`/`advance`/`deposit` → hipotezy „bez dokumentu” z podtypem; `refund` → szukanie pierwotnej wpłaty. Tytuł wrażliwy: semantyki nie ma, kandydat nigdy nie jest twardy. | `intent_agrees`, `intent_conflicts`, `no_document_intent`, `period` |
| 5. Arkusz PM | Wiersz `l1_internal_record` na dokładnie tę kwotę, data pracy od 14 dni przed do 3 dni po przelewie: ta sama osoba (pseudonim kontrahenta = pseudonim z arkusza), telefon BLIK = telefon z kolumny uwag (przez `pseudonym_key`, bez jawnego numeru), data z arkusza w tytule, nazwa eventu w tytule. Inna osoba = nigdy kandydat. **D2: nie twardy, dopóki nie dojrzeje reguła.** Ten sam koszt z fakturą (Zawadzki: rachunek PDF + wiersz R08) to dowód przy fakturze (`linked_evidence`), nie rywal. | `person`, `phone`, `record_date_in_title`, `record_date_close`, `record_label_in_title` |
| 6. Reguły | Nauczone (4C): `kasa.core.rules.apply.applicable_rules(conn, company_id, 'transaction', facts)` przez `importlib`; brak modułu = warstwa pominięta. Trafienie dodaje sygnał `rule:KLUCZ@wersja` (podpowiedź +1,5, cień 0, auto +4 i twardy dowód). Reguły `builtin:*` są stosowane w kodzie (warstwa 6 ich nie dubluje). | `rule:R-17@3` |

**Brak kandydata** → pierwsza propozycja to `no_document` z podtypem (`zus`, `tax`, `fee`, `interest`, `salary`,
`advance`, `deposit`, `card_awaiting` - karta czeka 30 dni na fakturę), nigdy przypadkowa faktura. Przelew BLIK /
natychmiastowy do osoby bez wiersza w arkuszu i bez dokumentu → `to_clarify`. **Remis** (dwie propozycje
dopasowania w granicach 0,02) → pierwsza propozycja to `to_clarify` („niejednoznaczne”), za nią remisujące faktury.

## 2. Wynik, wagi, prawdopodobieństwo

Każdy kandydat ma `surowy = BASE + Σ wag sygnałów` (jednostki log-szans, `weights.py`, `BASE = -3`) i
`p = 1 / (1 + exp(-(surowy - przesunięcie) / temperatura))`. Najważniejsze wagi:

| Sygnał | Waga | | Sygnał | Waga |
| --- | --- | --- | --- | --- |
| `ksef_number`, `mpp_block` | +7,0 | | `single_open_invoice` (NIP/IBAN) | +4,0 |
| `invoice_number` | +5,5 | | `single_open_invoice_weak`, `single_amount_any` | +1,0 |
| `amount_exact` | +2,0 | | `title_names_other_invoice` | -4,0 |
| `counterparty_strong` (NIP/IBAN/id) | +1,5 | | `possible_duplicate` | -3,0 |
| `counterparty_name` | +1,0 | | `counterparty_conflict` | -3,0 |
| `person` / `phone` (arkusz) | +2,5 / +1,5 | | `overpayment` / `underpayment` | -2,0 / -1,0 |
| `builtin_no_document`, `builtin_own_transfer` | +9,0 | | `window`, `intent_agrees` | +0,3 |

Przykład: numer faktury w tytule + ten sam kontrahent po IBAN + kwota co do grosza + termin w oknie + intencja
zapłaty = −3 + 5,5 + 1,5 + 2 + 0,3 + 0,3 = 6,6 → p = 0,9986. Dwie faktury Kadr Studio po 4 500 zł: obie
−3 + 1,5 + 2 + 0,3 + 0,3 = 1,1 → p = 0,75, remis.

**Twardy dowód** to osobna flaga (nie waga): numer KSeF / blok MPP / numer faktury przy zgodnym kontrahencie
(NIP, IBAN albo ta sama nazwa) **i** kwota co do grosza (w walucie faktury, z łańcuchem korekt, suma kilku
faktur), jedyna otwarta faktura kontrahenta po NIP/IBAN na dokładnie tę kwotę, ZUS/US/prowizja po nadawcy i typie
operacji, transfer własny, reguła nauczona w etapie `auto`. Flagę zdejmuje każda „blokada”: możliwy duplikat,
zwrot, arkusz PM (D2), słowo wrażliwe, tytuł wskazujący inną fakturę, nie wszystkie numery z tytułu znalezione.

**Odznaka** (kontrakt): wysoka = p ≥ 0,95 i twardy dowód; średnia = p ≥ 0,7; niska = reszta. Procent jest w
bazie (`l2_suggestion.score`), na karcie tylko odznaka (D8).

## 3. Automat - trzy warunki naraz

`auto.eligibility`: (1) p pierwszej propozycji ≥ próg (domyślnie **0,995**; kalibracja może podnieść, nigdy
obniżyć), (2) twardy dowód, (3) przewaga nad drugą ≥ **0,2** (remis < 0,02 nigdy), oraz (4) reguła w etapie
`auto`, która na to pozwala - reguła wbudowana odpowiadająca dowodowi albo reguła nauczona, z której kandydat
wynika. Decyzja jest podpisana tą regułą (`method = 'rule'`, `rule_id`, `rule_version`, `decided_by =
rule:builtin:…@1`); baza odrzuca ją, gdy reguła nie jest w etapie `auto` (KS004), a `apply_match` - gdy sama
sugestia nie spełnia trzech warunków albo faktura jest już rozliczona (sugestia nieaktualna → kolejka).

| Reguła wbudowana (`l3_rule`, etap `auto`, `created_by = briefing-2026-11-03`) | Tag kandydata |
| --- | --- |
| `builtin:match_ksef_number` | `ksef_number` + `amount_exact` |
| `builtin:match_mpp_block` | `mpp_block` + `amount_exact` (NIP wystawcy = IDC) |
| `builtin:match_invoice_number_same_counterparty` | `invoice_number` + `amount_exact` + kontrahent zgodny |
| `builtin:match_single_open_invoice_exact_amount` | `single_open_invoice` + `amount_exact` (kontrahent po NIP/IBAN) |
| `builtin:own_transfer` | `builtin_own_transfer` (`op_type = own`) |
| `builtin:no_document_zus_tax_fee` | `builtin_no_document` (ZUS / US / prowizja i odsetki banku) |

Reguły wbudowane są zasiane migracją dla każdej firmy (także nowej - trigger `company_builtin_rules`) z
`method = 'human'`: zgodą użytkownika jest briefing z 3.11. Zawieszasz je jak każdą inną regułę
(`UPDATE l3_rule SET stage = 'suspended', method = 'human'`); od tej chwili te przelewy idą do kolejki.

## 4. Kalibracja

```bash
python -m kasa control-set import-ground-truth --company odm          # zbiór z etykietami (dane testowe)
python -m kasa match calibrate --company odm --control-set ground-truth-transaction
```

`calibrate.py` ocenia każdy zaetykietowany przelew zbioru (jak bez decyzji), dla siatki temperatura ×
przesunięcie × próg (6 × 5 × 3 = 90 punktów) liczy automaty i **fałszywe automaty** (propozycja ≠ etykieta:
status, dokumenty, podtyp; „odsetki” = `fee` jak w K20). Wybór: tylko punkty z 0 fałszywych → najwięcej
automatów → najlepszy log-loss wszystkich propozycji wobec etykiet → wyższy próg → najbliżej wartości domyślnych.
Żaden punkt bez fałszywych → automat **wyłączony** (próg > 1). Każdy punkt trafia do `calibration_result`
(jeden `run_id`), wybrany z `chosen = true`; silnik czyta parametry ostatniego wybranego (`engine.load_params`).
Na danych testowych: temperatura 0,75, przesunięcie 0,5, próg 0,999 → 45 automatów, 0 fałszywych, kolejka
35/35 zgodna z etykietą w pierwszej propozycji.

## 5. Sugestie, decyzja, cofnięcie

* `l2_suggestion` (`kind = match`, `source = kasa.matching`): `proposal` dokładnie wg kontraktu
  (`items[]` z `document_id | internal_record_id | related_transaction_id`, `amount_minor` - po stronie przelewu,
  **ujemne dla korekty w grupie**, `difference_kind`, `difference_minor`; `final_status`, `subtype`,
  `awaiting_until`), `score` = p, `rank` 1-3, `evidence` = `facts` (po polsku), `badge`, `precedents`, `hard`,
  `margin`, `tags` + `detail` (surowy wynik, sygnały z wagami, kwoty po stronie dokumentu w jego walucie, nogi MPP,
  parametry, blokady).
* **Idempotencja**: przebieg, który proponuje to samo (propozycje, rangi, wyniki), nic nie zapisuje; inny wynik
  → stare otwarte sugestie przelewu `expired`, nowe wiersze. Propozycja odrzucona przez człowieka nie wraca
  (chyba że decyzja, która ją odrzuciła, została cofnięta).
* `decide.apply_match(conn, suggestion_id, *, decided_by, method='human'|'rule', rule_id=None, rule_version=None,
  reason=None)`: wpis `l3_decision_log` (`kind = match`, payload = propozycja, `suggestion_id`), wiersze
  `l3_match` z jednym `group_id` (trzy faktury, faktura + korekta, nogi MPP - noga VAT dostaje własny wiersz z
  kwotą nogi), `l3_transaction_status` (z `awaiting_until`) dla każdej nogi, sugestia `accepted`, pozostałe
  otwarte sugestie tych przelewów `rejected` (`resolved_by_decision`). **Jedna decyzja zamyka wszystkie nogi
  grupy MPP.** Przeksięgowanie z rachunku VAT to transfer własny - osobna decyzja (briefing: „przeksięgowanie =
  transfer własny”).
* **Cofnięcie** (4C): `revoke` w dzienniku + `status = 'revoked'` na wierszach tej decyzji. Saldo faktury liczymy
  tylko z wierszy `active`, więc cofnięte dopasowanie oddaje kwotę fakturze, a przelew wraca do dopasowania.

## 6. Różnice i kurs

Każda różnica ma nazwę, kwot nigdy nie „poprawiamy”: `difference_minor` = kwota przelewu − wartość dokumentu w
PLN (dla `underpayment`: brakująca reszta). `fx` - kurs (Helios: 52 737,50 zł wobec 12 500 EUR × 4,2468 =
53 085,00 → −347,50; Google kartą: +16,25), `fee` - prowizja potrącona z przelewu zagranicznego, `tolerance` -
różnica w tolerancji 1% albo 20 zł (waluta 1,5% albo 50 zł - zawsze większa z dwóch, tylko jedna faktura),
`underpayment`, `overpayment`, `correction` (wiersz korekty). `l3_match.document_amount_minor` trzyma część
dokumentu w jego walucie (EUR).

Kurs: **jedna funkcja** `kasa.core.matching.fx.rate_for(conn, waluta, data) -> (kurs, data_tabeli)` = ostatnia
tabela A NBP opublikowana **przed** datą (`l1_fx_rate`, źródło `nbp_a`, do 10 dni wstecz). Wartość PLN faktury
walutowej: kurs z faktury (KursWaluty), a gdy go nie ma - `rate_for(waluta, data sprzedaży albo wystawienia)`.
Zapisane fakty bankowe (kurs banku, `extracted.fx`) zostają bez zmian.

## 7. Kontrola prawdy 2: pokrycie dopasowań

`checks.check_match_coverage(conn, company_id, od, do)` (w `python -m kasa check` jako „Kontrola prawdy 2”, wynik
w `truth_check_result`, `check_name = match_coverage`): dla przelewów zaksięgowanych w okresie suma aktywnych
dopasowań ≤ |kwota przelewu| + nazwana tolerancja; dla każdej dotkniętej faktury suma (strona dokumentu, łańcuch
korekt) ≤ brutto łańcucha + tolerancja; dla wiersza arkusza ≤ jego kwota. Na danych testowych po pełnym przebiegu:
zielona (33 przelewy, 32 faktury).

## 8. CLI i jak czytać `match explain`

```bash
python -m kasa match run --company odm [--month 2026-10] [--dry-run] [--no-auto]
python -m kasa match calibrate --company odm --control-set <id|nazwa> [--no-record]
python -m kasa match explain <entry_reference|id przelewu> [--company odm]
python -m kasa match status --company odm [--month 2026-10]
```

`match run` wypisuje: ile przelewów do rozstrzygnięcia, ile zamknął automat (per reguła), ile do kolejki (w tym
„bez dokumentu” i remisy), rozkład pierwszych propozycji i ile sugestii powstało / zostało bez zmian / wygasło.
`--dry-run` nic nie zapisuje. `match status` - rozkład statusów końcowych (metoda `rule`/`human`), kolejka z
odznakami, przelewy bez decyzji i bez sugestii, kontrola prawdy 2.

`match explain` to łańcuch w pięciu częściach:

1. **Fakty** - data, kwota (i w walucie), kontrahent (rodzaj, NIP, IBAN), tytuł, typ operacji, co kod wyłuskał
   (numery, blok MPP, /TI/, okresy, telefony jako pseudonimy), intencja z semantyki.
2-3. **Sygnały i kandydaci** - przeliczone teraz (bez zapisu, jak bez decyzji). Każdy kandydat:
   `#ranga status/podtyp: pozycje [różnica]  surowy X -> p=Y, twardy=tak/nie, odznaka`, pod nim sygnały z wagą
   (`+5.5 invoice_number: FV 12/09/2026 w tytule`) i blokady (`! możliwy duplikat - zawsze kolejka`). Kandydaci
   bez rangi to ci, którzy nie weszli do trzech. Na końcu `automat: TAK/nie - powód`.
4. **Sugestie** - wszystkie wiersze `l2_suggestion` przelewu ze statusem (open / accepted / rejected / expired).
5. **Decyzja** - dziennik decyzji (kto: człowiek albo `rule:builtin:…@wersja`), status końcowy i dopasowania z
   grupą.

Noga VAT split payment pokazuje zdarzenie nogi głównej.

## 9. Wyniki na danych testowych (październik 2026, 83 przelewy)

80 podmiotów (3 nogi VAT należą do zdarzeń MPP). Automat: **45 decyzji zamyka 48 przelewów** (27 numer faktury
przy zgodnym kontrahencie, w tym 3 faktury jednym przelewem, korekta 10 000 − 2 000 i EUR z kursem; 3 bloki MPP z
nogami VAT; 9 ZUS/US/prowizje/odsetki; 6 transferów własnych), **0 fałszywych** wobec etykiet (dokumenty i kwoty z
`ground_truth.json`). Wszystkie 38 przelewów z `expected_route = auto` są automatem; żaden z `queue`. Kolejka: 35,
pierwsza propozycja zgodna z etykietą w 35/35 (Revolut, karty z różnicą kursu, 16 BLIK-ów i 2 przelewy z arkuszem,
zaliczka, komornik, duplikat i zwrot, remis Kadr Studio, częściowa zapłata Fali, wynagrodzenia, zaliczka ACME).

## 10. Decyzje i ograniczenia (świadome)

* Tolerancja „1% albo 20 zł” = większa z dwóch wartości (inaczej różnica kursu karty 16,25 zł przy 420 zł nie
  mieściłaby się w 1,5%). Tolerancja nigdy nie daje twardego dowodu.
* Nazwa kontrahenta bez form prawnych liczy się jako „zgodny kontrahent” dla numeru faktury w tytule (wpływy
  sprzedaży mają w banku tylko nazwę i IBAN klienta, faktura - tylko NIP). „Jedyna otwarta faktura” jest twarda
  tylko przy NIP/IBAN; na danych testowych nie zamyka żadnego przelewu.
* Odsetki banku mają podtyp `interest` (kontrakt); etykiety zbioru kontrolnego mają `fee` (K20) - porównanie
  traktuje je jako to samo.
* Duplikat: propozycja wiąże drugą wpłatę ze zwrotem (gdy jest) albo z pierwszą wpłatą; nigdy automat.
* Kurs: `rate_for` nie zmienia `l1_document.fx_rate` ani faktów bankowych; różnica „kurs” jest liczona przy
  dopasowaniu.
* Rezerwacja w przebiegu: twarda pierwsza propozycja (chronologicznie) zajmuje saldo faktury dla późniejszych
  przelewów tego samego przebiegu - dlatego druga identyczna wpłata Orbity widzi nadpłatę.
* Blokady kartowe: `resolved_by_transaction_id` zamiast `superseded_by` (0009). Wygaszanie blokad
  (`expired`) po czasie i dopasowanie blokady o innej kwocie niż księgowanie - nie w tej fazie.
* Prawdziwa kalibracja wymaga 600 przelewów zaetykietowanych w panelu bez podglądu propozycji; na 83 przelewach
  danych testowych „0 fałszywych” nie daje jeszcze górnej granicy błędu 0,5%.
