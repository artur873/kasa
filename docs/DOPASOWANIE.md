# Dopasowanie przelew ↔ faktura (faza 4, część A)

Krok 4 briefingu w kodzie: kasa łączy każdy zaksięgowany przelew z fakturą (albo wierszem arkusza PM, albo innym
przelewem), warstwami od najtańszej i najpewniejszej do człowieka. Żadna warstwa nie zamyka sprawy sama: zamyka
**automat** (gdy naraz spełni trzy warunki) albo **Ty** w kolejce.

Kod: `kasa/core/matching/` (`data.py` przygotowanie, `engine.py` warstwy i ranking, `weights.py` wagi i progi,
`fx.py` kurs, `suggestions.py` zapis sugestii, `auto.py` automat, `decide.py` decyzja, `calibrate.py` kalibracja,
`explain.py` wyjaśnienie, `run.py` jeden przebieg), CLI `kasa/cli/matching.py`, schemat
`migrations/0009_phase4_matching.sql` i `0013_matching_fixes.sql` (`evidence_only`), kontrola prawdy 2 w
`kasa/core/checks.py`, testy `tests/matching/` (poprawki przeglądu 4A/4B: `test_review_fixes.py`).

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
  (rezerwacja salda: tylko pierwsza propozycja, którą automat zamknie; odmowa automatu → przeliczenie)
                                                            │
                                                            ▼  decide.apply_match (method = rule | human)
                                      l3_decision_log + l3_match (group_id; wiersz arkusza-dowód evidence_only)
                                      + l3_transaction_status (+ człowiek: IBAN płatnika → kontrahent z NIP)
```

## 1. Warstwy

| Warstwa | Co robi | Sygnały (tagi) |
| --- | --- | --- |
| 1. Przygotowanie | Kwoty w groszach ze znakiem; numery faktur znormalizowane (`extracted.invoice_numbers` z wariantami `alt`); kontrahent przelewu ↔ strona faktury: **silnie** (`STRONG_LINKS`) tylko przez identyfikator - ten sam `counterparty_id`, gdy przelew doszedł do niego po IBAN (albo NIP bloku MPP), a dokument po NIP / numerze podatkowym albo tym samym IBAN; **NIP** (także z bloku MPP); **IBAN** (rachunek z faktury albo z kontrahenta, także dopięty decyzją człowieka); **słabo** - ta sama nazwa bez form prawnych („Notion Labs, Inc.” = „NOTION LABS INC”; także ten sam `counterparty_id` połączony przez `kasa_upsert_counterparty` tylko po nazwie), nazwa podobna albo nazwa z tytułu (Revolut; z formą prawną spółki - `extracted.payer_hint` - waży tyle co ta sama nazwa); inny NIP = konflikt. Transfer własny (`op_type = own`) to osobna hipoteza. Nogi split payment (`mpp_group_id`) to **jedno zdarzenie**: podmiotem jest noga główna, kwota zdarzenia = suma nóg (56 000 + 12 880 = 68 880 = brutto faktury), noga VAT nie dostaje własnej sugestii. Blokady kartowe (`pending`) są pomijane. | `counterparty_strong`, `counterparty_name`, `counterparty_similar`, `counterparty_title_name`, `counterparty_conflict`, `own_pair` |
| 2. Twarde identyfikatory | Numer KSeF w tytule (z sumą kontrolną); blok MPP: numer INV i NIP IDC = NIP wystawcy faktury; numer faktury w tytule - **wpływ szuka faktur sprzedaży, wypływ zakupu** („FV 7/10/2026” ACME vs czynsz Biura Centrum). Kwota zgodna → twardy dowód (numer faktury: tylko przy kontrahencie zgodnym po NIP/IBAN; po samej nazwie - sygnał `counterparty_name_only`, kolejka). Numer o poprawnym wzorcu, którego nie ma jeszcze w danych (faktura nie dotarła z KSeF) → propozycja `to_clarify` z `awaiting_until` = dzień po księgowaniu i tagiem `awaiting_ksef`: kolejka jej nie pokazuje do tego dnia, następny przebieg z fakturą dopasowuje normalnie. Kwota niezgodna → sygnał zostaje, propozycja częściowej zapłaty (`underpayment`, kwota dopasowania = kwota przelewu) albo nadpłaty, bez twardego dowodu. Kilka numerów w tytule = jedna grupa, suma co do grosza. | `ksef_number`, `mpp_block`, `invoice_number`, `amount_exact`, `underpayment`, `overpayment`, `counterparty_name_only`, `awaiting_ksef` |
| 3. Kontrahent, kwota, czas | Otwarte faktury kontrahenta (saldo = brutto − aktywne dopasowania; korekty z łańcucha `corrects_document_id` doliczane do faktury pierwotnej) w oknie **-30..+90 dni wokół terminu** (bez terminu: od daty wystawienia, z adnotacją). Dokładna kwota; „jedyna otwarta faktura kontrahenta na tę kwotę” (twardy dowód tylko przy kontrahencie po NIP/IBAN); 2-6 faktur sumujących się co do grosza (DFS z limitem); tolerancja **tylko przy jednej fakturze**; płatnik nierozpoznany (operator, karta): jedyny otwarty dokument na tę kwotę. Duplikat: identyczna wcześniejsza wpłata (kontrahent, kwota, tytuł) → `possible_duplicate`, nigdy automat, `related_transaction_id` do pierwotnej wpłaty albo do późniejszego zwrotu. Zwrot (tytuł „zwrot”, intencja `refund`) → pierwotna wpłata tego kontrahenta tej kwoty (data z tytułu rozstrzyga). Zaliczka: przed fakturą zaliczkową → `no_document/advance` (czeka 180 dni), gdy faktura zaliczkowa jest → dopasowanie. Waluta: kwota w walucie faktury (bank podaje ją przy karcie i przelewie zagranicznym) co do grosza + nazwana różnica „kurs”; bez kwoty w walucie: PLN wobec faktury × kurs, tolerancja 1,5% albo 50 zł. Raty faktury walutowej: rata niższa od salda = `underpayment` z częścią faktury w jej walucie (bez kwoty w walucie: PLN ÷ kurs faktury); ostatnia rata, która zamyka resztę w tolerancji kursu, dostaje `difference_kind = 'fx'` na resztę salda - suma rat = faktura co do centa. | `window`, `single_open_invoice`, `subset_sum`, `correction_chain`, `fx`, `amount_tolerance`, `amount_fx_tolerance`, `possible_duplicate`, `refunded_later`, `refund_date_in_title`, `advance_invoice`, `title_names_other_invoice` |
| 4. Semantyka tytułu | `title_semantics_current` (odczyt modelu z fazy 3; model nigdy nie widział faktur): intencja zgodna z dokumentem (zapłata, czynsz, abonament, zaliczka) +; sprzeczna (ZUS, podatek) −; `zus`/`tax`/`fee`/`rent`/`salary`/`advance`/`deposit` → hipotezy „bez dokumentu” z podtypem; `refund` → szukanie pierwotnej wpłaty. Tytuł wrażliwy: semantyki nie ma, kandydat nigdy nie jest twardy. | `intent_agrees`, `intent_conflicts`, `no_document_intent`, `period` |
| 5. Arkusz PM | Wiersz `l1_internal_record` na dokładnie tę kwotę, data pracy od 14 dni przed do 3 dni po przelewie: ta sama osoba (pseudonim kontrahenta = pseudonim z arkusza), telefon BLIK = telefon z kolumny uwag (przez `pseudonym_key`, bez jawnego numeru), data z arkusza w tytule, nazwa eventu w tytule. Inna osoba = nigdy kandydat. **D2: nie twardy, dopóki nie dojrzeje reguła.** Ten sam koszt z fakturą (Zawadzki: rachunek PDF + wiersz R08) to dowód przy fakturze (`linked_evidence`), nie rywal. | `person`, `phone`, `record_date_in_title`, `record_date_close`, `record_label_in_title` |
| 6. Reguły | Nauczone (4C): `kasa.core.rules.apply.applicable_rules(conn, company_id, 'transaction', facts)` przez `importlib`; brak modułu = warstwa pominięta. Trafienie dodaje sygnał `rule:KLUCZ@wersja` (podpowiedź +1,5, cień 0, auto +4 i twardy dowód). Reguły `builtin:*` są stosowane w kodzie (warstwa 6 ich nie dubluje). | `rule:R-17@3` |

**Wrażliwe**: `sensitive` z faktu banku (tytuł i nazwa kontrahenta) albo trwała flaga bramki semantyki (karta
„kategorię nadaje człowiek” - słowo dodane po imporcie); „to nie jest wrażliwe” człowieka (`sensitive_exception`)
zdejmuje oba. Wrażliwy przelew nie ma semantyki ani twardego dowodu.

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
| `counterparty_title_name` (z formą prawną) | +0,5 (+1,0) | | `awaiting_ksef` | +2,0 |
| `person` / `phone` (arkusz) | +2,5 / +1,5 | | `overpayment` / `underpayment` | -2,0 / -1,0 |
| `builtin_no_document`, `builtin_own_transfer` | +9,0 | | `window`, `intent_agrees` | +0,3 |

Przykład: numer faktury w tytule + ten sam kontrahent po IBAN + kwota co do grosza + termin w oknie + intencja
zapłaty = −3 + 5,5 + 1,5 + 2 + 0,3 + 0,3 = 6,6 → p = 0,9986. Dwie faktury Kadr Studio po 4 500 zł: obie
−3 + 1,5 + 2 + 0,3 + 0,3 = 1,1 → p = 0,75, remis.

**Twardy dowód** to osobna flaga (nie waga): numer KSeF / blok MPP / numer faktury przy zgodnym kontrahencie
(**tylko NIP albo IBAN** - `STRONG_LINKS`; ta sama nazwa to sygnał punktowy, przegląd 4A pkt 2) **i** kwota co do grosza (w walucie faktury, z łańcuchem korekt, suma kilku
faktur), jedyna otwarta faktura kontrahenta po NIP/IBAN na dokładnie tę kwotę, ZUS/US/prowizja po nadawcy i typie
operacji, transfer własny, reguła nauczona w etapie `auto`. Flagę zdejmuje każda „blokada”: możliwy duplikat,
zwrot, arkusz PM (D2), słowo wrażliwe, tytuł wskazujący inną fakturę, nie wszystkie numery z tytułu znalezione,
czekanie na fakturę z KSeF.

**Nazwa → IBAN decyzją człowieka.** Faktura sprzedaży zna nabywcę po NIP, bank płatnika - po nazwie i IBAN: to
dwaj różni kontrahenci, zgodni tylko nazwą, więc numer faktury w tytule nie jest twardym dowodem. Pierwsze
dopasowanie człowieka (`apply_match`, `method = 'human'`) dopina IBAN płatnika do kontrahenta z NIP faktury
(`kasa_upsert_counterparty` z IBAN; w dzienniku decyzji `evidence.iban_linked`) - od tej chwili kolejny przelew z
tego rachunku jest zgodny po IBAN, a numer faktury w tytule daje twardy dowód i automat. Tylko gdy bezpiecznie:
jeden kontrahent z NIP (albo numerem podatkowym) na dokumentach, ta sama nazwa co w banku, IBAN nie jest
rachunkiem operatora (Revolut, PayU…: klient tylko w tytule), firmy ani innego NIP-u.

**Odznaka** (kontrakt): wysoka = p ≥ 0,95 i twardy dowód; średnia = p ≥ 0,7; niska = reszta. Procent jest w
bazie (`l2_suggestion.score`), na karcie tylko odznaka (D8).

## 3. Automat - trzy warunki naraz

**Rezerwacja w przebiegu** (przegląd 4A pkt 4): saldo faktury dla późniejszych przelewów tego samego przebiegu
zajmuje tylko pierwsza propozycja, którą `auto.eligibility` dopuszcza (twarda, ale bez reguły w etapie `auto`
albo poniżej progu - nie rezerwuje; `--no-auto` - nic). Gdy automat potem odmówi (sugestia nieaktualna,
reguła zawieszona w trakcie), rezerwacja nie stała się decyzją: przelewy nadal bez decyzji są przeliczane bez
niej (do 3 przebiegów, `RunReport.passes`) - ich sugestie nie widzą nadpłaty, której nie ma.

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
| `builtin:match_invoice_number_same_counterparty` | `invoice_number` + `amount_exact` + kontrahent zgodny po NIP/IBAN |
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

`calibrate.py` dobiera temperaturę × przesunięcie × próg (siatka 6 × 5 × 3 = 90 punktów; próg nigdy poniżej
0,995) **walidacją krzyżową** (przegląd 4A pkt 1 - wybór i raport „0 fałszywych” na tych samych etykietach
niczego nie dowodzi):

1. **Foldy** (deterministycznie, bez zależności od identyfikatorów bazy): etykiety z kilku miesięcy - po
   miesiącu (bloki kolejnych miesięcy, najwyżej 5); z jednego miesiąca - po kontrahencie (NIP, inaczej nazwa bez
   form prawnych, inaczej IBAN), `k = 5`, grupy od największej do najmniejszego foldu; nogi split payment w tym
   samym foldzie.
2. Każdy fold to **osobny przebieg** (jego przelewy jak nowy miesiąc; pozostałe zaetykietowane przelewy ani nie
   są decyzjami, ani nie rezerwują faktur), oceniany każdym punktem siatki; osobno przebieg całego zbioru (jak
   miesiąc).
3. Punkt jest **dopuszczalny tylko z 0 fałszywych automatów w KAŻDYM foldzie testowym** (i w przebiegu całości).
   Spośród dopuszczalnych: najwięcej automatów na foldach testowych → najlepszy log-loss → wyższy próg →
   najbliżej wartości domyślnych. Żaden dopuszczalny → automat **wyłączony** (próg > 1).
4. „Fałszywe” (`false_auto`) liczone są na foldach testowych; ocena zagnieżdżona (punkt wybrany na pozostałych
   foldach, sprawdzony na danym) idzie do `metrics.nested`.

Rezerwacja zależy od parametrów (automat zamknie tylko to, co przejdzie próg), więc przebieg zrobiony przy
jednym punkcie służy innemu tylko wtedy, gdy ten zarezerwowałby dokładnie te same przelewy; inaczej fold jest
liczony od nowa (na danych testowych: 1,4 s za całą kalibrację).

`calibration_result.metrics` punktu: `labels` (ocenione etykiety = przelewy-podmioty), `labels_total`,
`autos`, `false_auto`, `folds` (`labels`, `autos`, `false_auto` każdego), `whole`, `admissible`,
`upper_bound_error_wilson95` - górna granica 95% Wilsona błędu automatu (0 błędów na n etykietach:
z²/(n + z²)), `trustworthy` = ta granica ≤ 0,5%, `labels_needed` (765 etykiet bez błędu); wybrany ma też
`nested`, `basis` i `k`. Silnik czyta parametry ostatniego wybranego (`engine.load_params`).
`match calibrate` i `match status` drukują **ostrzeżenie**, dopóki `trustworthy = false` (albo kalibracji nie
było).

Na danych testowych: 5 foldów po kontrahencie, temperatura 0,75, przesunięcie 0,5, próg 0,999 → 41 automatów,
0 fałszywych w każdym foldzie (14/10/4/8/5), ocena zagnieżdżona 41 / 0, kolejka 39/39 zgodna z etykietą w
pierwszej propozycji; górna granica błędu 4,58% przy 80 etykietach → **ostrzeżenie** (oczekiwane: prawdziwa
kalibracja to 765+ przelewów zaetykietowanych w panelu bez podglądu propozycji).

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
  otwarte sugestie tych przelewów `rejected` (`resolved_by_decision`). **Koszt jeden, dowodów wiele**: wiersze
  arkusza z `linked_evidence` sugestii (ten sam koszt co dokument - Zawadzki: rachunek 4/2026 + wiersz R08)
  dostają wiersz `l3_match` w **tej samej grupie** z `evidence_only = true`, kwotą 0 i `evidence.evidence_of` =
  dokumenty grupy (0013) - dowód, nie drugie rozliczenie: kontrola prawdy 2 go pomija, wiersz arkusza jest
  rozliczony przez dokument (nie jest już kandydatem innego przelewu), `allocation.subjects.evidence_of_documents`
  zwraca go z powodem `evidence_only` i `group_id`. Decyzja człowieka dopasowująca dokument kontrahenta znanego
  tylko z nazwy dopina IBAN płatnika do kontrahenta z NIP (rozdz. 2, `evidence.iban_linked`). **Jedna decyzja zamyka wszystkie nogi
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

**Raty faktury walutowej** (przegląd 4A pkt 7): rata = `underpayment` z częścią faktury w EUR (kwota EUR z banku,
a bez niej PLN ÷ kurs faktury); rata, która zamyka resztę w tolerancji kursu (1,5% albo 50 zł wobec reszty × kurs
faktury), dostaje `document_amount_minor` = reszta salda w EUR i `difference_kind = 'fx'` (PLN raty − reszta ×
kurs), także gdy bank podał EUR o kilka centów inne niż reszta. Suma rat = faktura co do centa (test: dwie raty
po 5 000 EUR, z kwotą EUR i bez niej).

Kurs: **jedna funkcja** `kasa.core.matching.fx.rate_for(conn, waluta, data) -> (kurs, data_tabeli)` = ostatnia
tabela A NBP opublikowana **przed** datą (`l1_fx_rate`, źródło `nbp_a`, do 10 dni wstecz). Wartość PLN faktury
walutowej: kurs z faktury (KursWaluty), a gdy go nie ma - `rate_for(waluta, data sprzedaży albo wystawienia)`.
Zapisane fakty bankowe (kurs banku, `extracted.fx`) zostają bez zmian.

## 7. Kontrola prawdy 2: pokrycie dopasowań

`checks.check_match_coverage(conn, company_id, od, do)` (w `python -m kasa check` jako „Kontrola prawdy 2”, wynik
w `truth_check_result`, `check_name = match_coverage`): dla przelewów zaksięgowanych w okresie suma aktywnych
dopasowań ≤ |kwota przelewu| + nazwana tolerancja; dla każdej dotkniętej faktury suma (strona dokumentu, łańcuch
korekt) ≤ brutto łańcucha + tolerancja; dla wiersza arkusza ≤ jego kwota. Wiersze `evidence_only` (dowód, kwota 0)
są pomijane. Na danych testowych po pełnym przebiegu: zielona (29 przelewów, 28 faktur).

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
odznakami, przelewy bez decyzji i bez sugestii, kontrola prawdy 2 i **ostrzeżenie**, gdy ostatnia kalibracja nie
jest wiarygodna (`trustworthy = false`: górna granica Wilsona błędu automatu > 0,5%) albo jej nie było.

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

80 podmiotów (3 nogi VAT należą do zdarzeń MPP). Automat: **41 decyzji zamyka 44 przelewy** (23 numer faktury
przy kontrahencie zgodnym po NIP/IBAN, w tym 3 faktury jednym przelewem; 3 bloki MPP z nogami VAT; 9
ZUS/US/prowizje/odsetki; 6 transferów własnych), **0 fałszywych** wobec etykiet (dokumenty i kwoty z
`ground_truth.json`); wiersz arkusza R08 jest w grupie rachunku Zawadzkiego jako `evidence_only` (= jego
`linked_evidence`). Kolejka: 39, pierwsza propozycja zgodna z etykietą w 39/39.

Przed poprawkami przeglądu 4A/4B automat zamykał 45 decyzji; **4 przelewy z `expected_route = auto` przestały
być automatem** (zostają w kolejce z właściwą fakturą jako pierwszą propozycją, odznaka średnia/wysoka bez
twardego dowodu) - wszystkie to numer faktury sprzedaży przy kontrahencie zgodnym **tylko po nazwie** (bank zna
płatnika po nazwie i IBAN, faktura nabywcę po NIP / VAT-UE, IBAN nabywcy nie jest nigdzie zapisany):

| Przelew | Scenariusz | Faktura | Dlaczego nie automat |
| --- | --- | --- | --- |
| `202610136613771164` | MB-ORBITA-1 | FV 5/09/2026 | Orbita Media: kontrahent banku (nazwa + IBAN) ≠ kontrahent faktury (NIP 7795524621, bez IBAN) |
| `ML2610205610950` | ML-NORDIS-KOR | FV 2/10/2026 + KOR 1/10/2026 | Nordis Retail: jw. (NIP 5867422344) |
| `202610280050568790` | MB-HELIOS-EUR | FV 5/10/2026 (EUR) | Helios Software GmbH: jw. (VAT-UE DE686148513) |
| `ML2610301580065` | ML-NORDIS-WRO | FV 8/10/2026 | Nordis Retail: jw.; po decyzji człowieka przy ML-NORDIS-KOR (20.10) IBAN Nordis jest dopięty i ten przelew (30.10) jest już automatem |

`tests/matching/test_against_ground_truth.py` oczekuje dla `expected_route = auto`: automat **albo** kolejka z
pierwszą propozycją zgodną z etykietą (lista `NAME_ONLY`); `ground_truth.json` się nie zmienia.

## 10. Decyzje i ograniczenia (świadome)

* Tolerancja „1% albo 20 zł” = większa z dwóch wartości (inaczej różnica kursu karty 16,25 zł przy 420 zł nie
  mieściłaby się w 1,5%). Tolerancja nigdy nie daje twardego dowodu.
* Nazwa kontrahenta bez form prawnych **nie** jest „zgodnym kontrahentem” dla twardego dowodu z numeru faktury
  (przegląd 4A pkt 2; do fazy 4C była): wpływy sprzedaży mają w banku tylko nazwę i IBAN klienta, faktura - tylko
  NIP, więc pierwszy przelew klienta idzie do kolejki, a decyzja człowieka dopina IBAN. „Jedyna otwarta faktura”
  jest twarda tylko przy NIP/IBAN; na danych testowych nie zamyka żadnego przelewu.
* Ten sam `counterparty_id` jest silny tylko przez identyfikator po obu stronach (przelew po IBAN albo NIP bloku
  MPP, dokument po NIP / numerze podatkowym albo tym samym IBAN): `kasa_upsert_counterparty` łączy rachunek PDF
  bez NIP i IBAN z kontrahentem banku po nazwie.
* Dopięcie IBAN po decyzji człowieka zmienia fakt `l1_counterparty.ibans` funkcją `kasa_upsert_counterparty`
  (jak bank przy imporcie); pochodzenie jest w dzienniku decyzji (`evidence.iban_linked`). Cofnięcie decyzji nie
  odpina IBAN (nic nie kasujemy) - błędne powiązanie wychodzi jako konflikt NIP przy kolejnym przelewie.
* Czekanie na fakturę z KSeF: `awaiting_until` = dzień po dacie księgowania (nie dzień przebiegu) - przebieg
  historyczny nie czeka, przebieg dzienny czeka jeden przebieg.
* Odsetki banku mają podtyp `interest` (kontrakt); etykiety zbioru kontrolnego mają `fee` (K20) - porównanie
  traktuje je jako to samo.
* Duplikat: propozycja wiąże drugą wpłatę ze zwrotem (gdy jest) albo z pierwszą wpłatą; nigdy automat.
* Kurs: `rate_for` nie zmienia `l1_document.fx_rate` ani faktów bankowych; różnica „kurs” jest liczona przy
  dopasowaniu.
* Rezerwacja w przebiegu: pierwsza propozycja, którą automat zamknie (chronologicznie), zajmuje saldo faktury dla
  późniejszych przelewów tego samego przebiegu; odmowa automatu → przeliczenie bez tej rezerwacji. Druga
  identyczna wpłata Orbity widzi nadpłatę dopiero po decyzji o pierwszej (a i bez niej jest „możliwym duplikatem”).
* Blokady kartowe: `resolved_by_transaction_id` zamiast `superseded_by` (0009). Wygaszanie blokad
  (`expired`) po czasie i dopasowanie blokady o innej kwocie niż księgowanie - nie w tej fazie.
* Prawdziwa kalibracja wymaga 765 przelewów zaetykietowanych w panelu bez podglądu propozycji (Wilson 95%; reguła
  trzech dawała 600); na 80 podmiotach danych testowych „0 fałszywych” daje górną granicę 4,58% - stąd
  ostrzeżenie w `match status`.
