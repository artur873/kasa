# Reguły uczone z decyzji (faza 4, część C)

Krok 6 briefingu w kodzie: reguła to **wzorzec z dowodem** („nadawca z tym IBAN-em → ZUS, 14 z 14 przypadków”).
Powstaje z Twoich decyzji i twardych identyfikatorów - **nigdy z wyników samego automatu** - i dojrzewa etapami,
zanim zacznie działać sama. Kod: `kasa/core/rules/` (`apply.py` trafienia, `mine.py` kandydaci, `observe.py`
obserwacje, `lifecycle.py` Twoje decyzje, `audit.py` audyt, `auto.py` kategorie z reguł w etapie auto,
`simulate.py` symulacja, `facts.py` fakty podmiotu i kanoniczny kontrahent), CLI `python -m kasa rules|audit`,
schemat `migrations/0011_phase4_queue_rules.sql`, testy `tests/rules/`. Kolejka: `docs/KOLEJKA.md`.

## 1. Reguła w bazie (`l3_rule`)

| Pole | Znaczenie |
| --- | --- |
| `rule_key`, `version` | `R-17` wersja 3; **wersja nigdy nie jest nadpisywana** - zmiana wzorca, akcji albo zakresu = nowy wiersz |
| `stage` | etap zapisany przez człowieka: `candidate` (kod), `hint`, `auto`, `suspended`; `shadow` i `dormant` są pochodne (widok) |
| `pattern` | `{"all": [{"field", "op", "value"}]}`, `op` ∈ `eq` / `regex` / `between` / `in` (kontrakt fazy 4) |
| `action` | `{"kind": "category", "category": …}`, `{"kind": "final_status", "final_status": "no_document", "subtype": …}`, `{"kind": "allocation", "cost_object_id": …, "share": 1}` albo `"split": "equal"`, `{"kind": "match_strategy", "strategy": "monthly_bulk" \| "payer_alias", …}` |
| `scope` | `{"counterparties": [...], "subject_types": [...]}` - znani kontrahenci z dowodów: **nowy kontrahent = brak trafienia** („nie obejmuje nowych kontrahentów”) |
| `question` | pytanie biznesowe karty |
| `evidence_count`, `exceptions` | X przypadków, wszystkie wyjątki |
| `last_hit_at`, `suspended_reason`, `post_mortem` | ostatnie trafienie (księgowość, nie decyzja), powód zawieszenia, szkic post-mortem |

Reguły wbudowane (`builtin:*`, briefing 3.11) mają pusty zakres i są stosowane przez 4A/4B w ich kodzie.

## 2. `applicable_rules` - kontrakt dla 4A i 4B

```python
from kasa.core.rules.apply import applicable_rules
hits = applicable_rules(conn, company_id, "transaction", {
    "counterparty_id": …, "counterparty_kind": …, "op_type": …, "intent": …, "title": …,
    "amount_minor": …, "direction": …, "counterparty_iban": …, "transaction_id": …})
# [RuleHit(rule_id, rule_key, version, stage, action, evidence_count, pattern, scope, question, builtin,
#          conflict, specificity, observations, agreed, wilson_lower, ready_for_auto)]
```

* tylko **bieżąca wersja** każdej reguły, etap efektywny `hint` / `shadow` / `auto` (widok `rule_stage_effective`);
  reguła uśpiona, która trafia, wraca jako `hint` („wraca jako podpowiedź przy pierwszym trafieniu”);
* `title_regex` czyta `title`; `counterparty_id` porównuje także **kanonicznego kontrahenta**: kontrahent z banku
  bez NIP-u, którego IBAN albo nazwa należy do kontrahenta z NIP-em, to ten sam kontrahent (Orange: 12 rachunków
  wirtualnych = 1 Orange) - dla wzorców, zakresu, kopania i paczek;
* zakres: reguła nauczona trafia tylko u kontrahentów ze swoich dowodów i w rodzajach podmiotów, na których się
  uczyła (przegląd 4A/4B, pkt 3: `match_strategy` w auto nie uczyni twardym kandydata obcego kontrahenta);
* **konflikt** dwóch reguł nauczonych, które mówią co innego o tym samym (kategoria ≠ kategoria, status ≠ status,
  udział ≠ udział, „bez dokumentu” ≠ strategia dopasowania): wygrywa bardziej szczegółowa (jej warunki zawierają
  warunki drugiej) z dowodem; inaczej **oba warianty** wracają jako `hint` z `conflict = True` - idą do kolejki,
  nigdy „pierwsza wygrywa”;
* reguły wbudowane też wracają (`builtin = True`), 4A/4B je pomijają;
* odczyt w punkcie zapisu (savepoint): awaria warstwy reguł nie psuje transakcji wołającego.

4A dodaje sygnał `rule:KLUCZ@wersja` (podpowiedź +1,5, cień 0, auto +4 i twardy dowód; test
`tests/rules/test_apply.py`: reguła `final_status` w auto zamyka wynagrodzenie jak twardy dowód), 4B - sygnał A
w auto / B w podpowiedzi i podział równy z reguły `split: equal`.

## 3. Etapy - stan pochodny (przegląd fazy 1, pkt 6; K9)

| Etap | Skąd | Kiedy dalej |
| --- | --- | --- |
| Kandydat | `mine.py`: ≥ 5 przypadków w ≥ 2 miesiącach | Ty: akceptuj (→ podpowiedź, **nowa wersja**) / popraw (nowa wersja) / odrzuć |
| Podpowiedź | zapisana Twoją decyzją | **≥ 30 obserwacji** tej wersji → cień (liczy widok) |
| Cień | pochodny | dolna granica 95% przedziału Wilsona zgodności **≥ 0,95** (n zgodnych z n: n / (n + 1,96²) ≥ 0,95 → **73 zgodne przy 0 błędów**) → „gotowa do auto”: karta `rule_ready` |
| Auto | „Włącz auto” jednym kliknięciem (`enable_auto`) | **jedna Twoja poprawka** → zawieszona z post-mortem |
| Zawieszona | poprawka w auto, potwierdzony błąd audytu, cofnięcie decyzji automatu, Twoja decyzja | `resume` → podpowiedź (albo auto, gdy próg wciąż spełniony) |
| Uśpiona | pochodny: 12 mies. bez trafienia (obserwacji ani decyzji automatu) | pierwsze trafienie → znów podpowiedź (do Twojej decyzji) |

Widok `rule_stage_effective` (0011) zwraca dla bieżącej wersji: etap zapisany i efektywny, obserwacje, zgodne,
niezgodne, decyzje automatu, dolną granicę Wilsona, `ready_for_auto`, ostatnie trafienie. Kod niczego z tego nie
zapisuje w `l3_rule` - przejścia automatyczne są liczone, przejścia ludzkie są decyzjami (`l3_decision_log`,
`kind = 'rule'`, payload `action`, `from_stage`, `to_stage`). Baza dalej pilnuje: automat pisze tylko z regułą w
etapie `auto` (KS004), etap zmienia tylko człowiek z nową decyzją (KS003).

## 4. Kandydaci (`mine.py`)

Dowody: decyzje człowieka w mocy (nie cofnięte) oraz - dla strategii dopasowania - dopasowania oparte na twardym
identyfikatorze w tytule (numer KSeF, blok MPP, numer faktury: dowodem jest identyfikator, nie decyzja automatu).
Decyzja automatu bez twardego identyfikatora **nigdy** nie jest dowodem (system nie potwierdza sam siebie).

| Rodzina | Dowód | Akcja | Pytanie |
| --- | --- | --- | --- |
| `category` | kategorie nadane przez Ciebie (nadawca/IBAN → kategoria: ZUS, US, Orange) | `category` | „Czy Orange Polska S.A. to zawsze kategoria „IT/SaaS”?” |
| `no_document` | status „bez dokumentu” z podtypem | `final_status` | „Czy przelewy … to zawsze „bez dokumentu: ZUS”?” |
| `allocation` | alokacje 100% do jednego obiektu | `allocation` | „Czy koszty od … zawsze idą w 100% do „…”?” |
| `monthly_bulk` | jeden przelew za kilka faktur | `match_strategy: monthly_bulk` | „Czy … płaci zbiorczo?” |
| `payer_alias` | płatnik ≠ kontrahent faktur, słowo nazwy klienta w tytule (Revolut) | `match_strategy: payer_alias` | „Czy wpłata z rachunku LT… ze słowem „kwadrat” w tytule to zawsze klient Kwadrat Development?” |

Próg: ≥ `min_cases` (5) przypadków, ≥ `min_months` (2) miesiące, większość decyzji kontrahenta. W CLI
`--min-months 1` pozwala pokazać kandydatów na jednym miesiącu danych testowych (domyślnie zawsze 2).
Kandydat = wiersz `l3_rule` (etap `candidate`, `method = code`, `evidence.signature` = rodzina | kontrahent |
rozstrzygnięcie) + karta `l2_suggestion` (`kind = rule_candidate`, kontrakt: `rule_key`, `question`, `hits`,
`total`, `counterparties`, 3 `examples`, **wszystkie** `exceptions`, `pattern`, `action`, `months`). Kopanie jest
idempotentne (zmienione liczby odświeżają kandydata i wymieniają kartę) i przyrostowe: po każdej Twojej decyzji
tylko dla jej kontrahenta. Kandydat odrzucony („jawne nie”: nowa wersja w etapie `suspended`, powód „odrzucona”)
nie wraca; brak wystąpienia nigdy nie tworzy reguły negatywnej.

## 5. Obserwacje (`observe.py`, `l2_rule_observation`)

Po każdej **Twojej** decyzji (dopasowanie, alokacja, kategoria) dla każdej reguły nauczonej w etapie podpowiedź /
cień / auto, która trafia w podmiot (bez rozstrzygania konfliktów - każda obserwowana osobno): co przewidziała
(`predicted`), co zdecydowałeś (`actual`), zgodność (`agreed`), etap w chwili obserwacji. Porównanie po rodzaju akcji:
status i podtyp (odsetki = prowizja), strategia (zbiorczo = ≥ 2 faktury; płatnik = faktury tego klienta), kategoria,
udziały. Niezgodność z regułą w etapie **auto** = poprawka → zawieszenie od razu z post-mortem. Cofnięcie decyzji
wycofuje jej obserwacje (`withdrawn_at`): reguła traci punkt, a w etapie auto, gdy spadnie poniżej progu - jest
zawieszona. Obserwacja jest zapisywana raz; jedyna zmiana to wycofanie (KS012).

## 6. Twoje decyzje o regule (`lifecycle.py`)

`accept`, `correct(pattern | action | scope | question)`, `reject` → nowa wersja; `enable_auto` (tylko gdy gotowa;
`--force` = świadome obejście), `suspend(reason)` (szkic post-mortem: co się stało, decyzja, przewidywanie vs Twoja
decyzja, obserwacje, wzorzec, co sprawdzić), `resume(to_stage=hint|auto)` → ten sam wiersz, nowa decyzja w dzienniku.
Każdą można cofnąć (`queue undo`): etap wraca, a wersja utworzona decyzją zostaje zawieszona.

## 7. Automat kategorii (`auto.py`)

Dopasowania i alokacje z reguł stosują 4A i 4B. Kategorie - `apply_category_rules` (woła ją `queue build`): dla
przelewu albo dokumentu bez kategorii, gdy trafia reguła `category` w etapie auto bez konfliktu, zapis
`l3_category_decision` z `method = 'rule'`, podpisany regułą i wersją. Podmiot ze słowem wrażliwym nigdy.

## 8. Audyt (`audit.py`, `l2_audit_sample`)

Co miesiąc 2% decyzji automatu w mocy, **min. 20** (mniej niż 20 = wszystkie), losowo z zapisanym ziarnem; miesiąc =
data podmiotu (księgowania przelewu, sprzedaży dokumentu, wiersza). Losowanie raz na miesiąc (idempotentne).
Próbka to zwykła karta kolejki z oznaczeniem „audyt”: Potwierdź / Błąd. Błąd = cofnięcie decyzji automatu (przelew
wraca do kolejki) i zawieszenie reguły ze szkicem post-mortem; kwota błędu wchodzi do KPI „błędy ważone kwotą”.

## 9. Symulacja (`rules simulate`)

Co reguła zrobiłaby na historii: trafienia (wzorzec + zakres), zgodne / niezgodne z decyzją w mocy (człowieka albo
automatu), bez decyzji, wyjątki po nazwie. Nic nie zapisuje.

## 10. CLI

```bash
python -m kasa rules list [--all]                       # etap zapisany → efektywny, obserwacje, Wilson, GOTOWA
python -m kasa rules show R-3                            # wzorzec, akcja, zakres, wersje, post-mortem
python -m kasa rules mine [--min-months 1] [--min-cases 5] [--dry-run] [--verbose]
python -m kasa rules accept|reject|enable-auto|suspend|resume R-3 [--reason …] [--to hint|auto] [--force]
python -m kasa rules correct R-3 [--pattern JSON] [--action JSON] [--scope JSON] [--question …]
python -m kasa rules simulate R-3
python -m kasa audit sample [--month 2026-10] [--seed 7]  # domyślnie poprzedni miesiąc
python -m kasa audit list [--month 2026-10] [--open]
python -m kasa audit verdict <próbka> confirmed|error [--note …]
```

## 11. Wyniki na danych testowych

* Baza `kasa` (bez decyzji człowieka): `rules mine --min-months 1` - 0 kandydatów (dowodem byłyby tylko twarde
  identyfikatory: 2 wzorce „płaci zbiorczo” po 1 przypadku); audyt października: 45 decyzji automatu → 20 kart.
* Po symulowanym klikaniu kolejki (bramka 4): `rules mine --min-months 1` → „Czy koszty od Orange Polska S.A. zawsze
  idą w 100% do „UNASSIGNED”? (12 z 12)”; z progiem 2 miesięcy - nic (jeden miesiąc danych).
* Bramka 4 (e), dane syntetyczne z 2 miesięcy (osobny rachunek): 6 decyzji ZUS → kandydat „6 z 6 u 1 kontrahenta”, 6
  decyzji Orange → kandydat; akceptacja kartą → podpowiedź; 30 obserwacji → cień; 72 → nie gotowa, 73 → karta „reguła
  gotowa”, „Włącz auto” → automat nadaje kategorię nowym przelewom ZUS; jedno cofnięcie decyzji automatu → zawieszona
  z post-mortem, automat staje. Audyt: 66 decyzji automatu, 20 w próbce.

## 12. Decyzje własne [4C] i ograniczenia

* Akceptacja kandydata tworzy wersję 2 (podpowiedź), a wiersz kandydata (wersja 1, `method = code`) zostaje jak był -
  historia propozycji kodu. Zmiana etapu bieżącej wersji (auto, zawieszenie, wznowienie) to stan tego samego wiersza
  z nową decyzją; zmiana treści to zawsze nowa wersja.
* Odrzucenie = wersja w etapie `suspended` z powodem „odrzucona” (enum `rule_stage` nie ma `rejected`).
* `shadow` liczony z obserwacji całej wersji (także zebranych w podpowiedzi) - briefing mówi o trafieniach
  „obserwowanych w cieniu”; podpowiedź jest obserwowana pasywnie tak samo.
* Cofnięcie decyzji automatu zawiesza także regułę wbudowaną (briefing: jedna poprawka zawiesza regułę auto) - od
  tej chwili jej przypadki idą do kolejki, aż ją wznowisz (`rules resume builtin:… --to auto`).
* Kanoniczny kontrahent jest wyliczany przy odczycie (NIP/numer zagraniczny + wspólny IBAN albo nazwa), bez
  łączenia wierszy `l1_counterparty`.
* Reguła `payer_alias` dodaje w 4A wagę wszystkim kandydatom-dokumentom przelewu (tak działa warstwa 6 silnika dla
  `match_strategy`); zawężenie do faktur klienta z akcji wymaga zmiany `engine.py` (faza 5, przegląd 4A/4B pkt 3).
* Uśpienie po 12 miesiącach: dane testowe mają jeden miesiąc - test ustawia starą datę etapu.
