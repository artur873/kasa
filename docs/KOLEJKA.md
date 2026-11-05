# Kolejka przeglądu (faza 4, część C)

Krok 7 briefingu w kodzie: kolejka to Twój główny ekran. **Jedna karta = jedna decyzja**: kwota, kontrahent,
data, tytuł, 1-3 propozycje jako przyciski, pod każdą dowód w faktach i odznaka wysoka / średnia / niska z liczbą
precedensów. Procent pewności jest w bazie (`l2_suggestion.score`), **nigdy na karcie** (D8).

Kod: `kasa/core/queue/` (`build.py` budowa i kolejność, `cards.py` model karty, `actions.py` przyciski, historia,
KPI, `state.py` czas pokazania i decyzji), decyzje `kasa/core/decisions.py` (dyspozytor), panel
`kasa/web/routers/queue.py` + `kasa/web/templates/queue/`, CLI `kasa/cli/queue.py`, schemat
`migrations/0011_phase4_queue_rules.sql`, testy `tests/queue/`, bramka `tests/gates/test_gate4.py`. Reguły:
`docs/REGULY.md`.

```
 l2_suggestion (open)   kandydaci na obiekty   kandydaci na reguły   reguły „gotowe”   próbki audytu
  match / allocation /        (4B)                 (mine.py)        (rule_stage_effective) (audit.py)
  category / duplicate          │                     │                    │                 │
        └───────────────────────┴──────────┬──────────┴────────────────────┴─────────────────┘
                                           ▼ build.py: karty, priorytet, powód rangi, wartownicy co ~50
                                     queue_state (shown_at = pierwsze pokazanie, odłożenie, czas decyzji)
                                           ▼ actions.answer (panel / API / CLI)
     decisions.accept_suggestion / reject_suggestion / defer_suggestion / decide_other / bulk_accept
        │ match → apply_match (4A)   allocation → apply_allocation / allocate_manually (4B)
        │ category → decide_category  cost_object_candidate → approve/split/merge/reject/distinct (4B)
        │ rule_candidate → rules.lifecycle   duplicate → werdykt
        ▼ po każdej decyzji człowieka: rules.observe (obserwacje) + rules.mine (przyrostowo) + queue_state
                                 l3_decision_log (undo_until = +10 s, batch_id) ── revoke_decision (cofnij)
```

## 1. Karty

| Karta (`card_kind`) | Skąd | Co pokazuje | Przyciski |
| --- | --- | --- | --- |
| `match` | otwarte sugestie dopasowania przelewu (rank 1-3, 4A) | przelew; propozycje: faktury / wiersz arkusza / przelew, „bez dokumentu: podtyp”, „transfer własny”, „niejednoznaczne (remis)” | Akceptuj, alternatywa (rank 2-3), Inne…, Odrzuć, Odłóż, paczka |
| `allocation` | otwarte sugestie alokacji (4B) | pozycja / dokument / wiersz / przelew; podział na obiekty z udziałami | jw.; propozycja częściowa → „Uzupełnij podział” |
| `sensitive` | `category` z `needs_human` (słowo wrażliwe, faza 3) | podmiot bez odczytu modelu | kategoria z listy rozszerzenia, **„To nie jest wrażliwe”**, Odłóż |
| `category`, `duplicate` | sugestie kategorii / duplikatu dokumentu | propozycja | Akceptuj, Odrzuć (duplikat: werdykt „to nie duplikat” jest decyzją) |
| `cost_object_candidate` | karta kandydata 4B | nazwa, klient, daty, miasto, lista rekordów z typem i siłą (słowem), najsłabsze powiązanie, suma przychodu i kosztów | Zatwierdź, Podziel (zaznacz pierwszą grupę), Połącz z, Na pewno różne, Odrzuć |
| `rule_candidate` | `rules.mine` | pytanie biznesowe, „X z Y u N kontrahentów”, 3 przykłady, wszystkie wyjątki | Akceptuj (jako podpowiedź), Popraw (nowa wersja), Odrzuć |
| `rule_ready` | widok `rule_stage_effective` (`ready_for_auto`) | reguła, zgodne / niezgodne, dolna granica Wilsona | **Włącz auto** (jedno kliknięcie), Nie teraz |
| `audit` | `l2_audit_sample` (2% decyzji automatu, min. 20) | oznaczenie „audyt”, decyzja automatu z faktami | Potwierdź, Błąd |
| `sentinel` | wartownik (znana odpowiedź) | wygląda dokładnie jak karta `match` | jak `match`; wynik ukryty |

Zasady karty:

* **Brak kandydata** → pierwsza propozycja to „bez dokumentu: zaliczka / ZUS / prowizja / …” (robi to 4A; karta
  tylko nazywa podtyp po polsku), nigdy przypadkowa faktura.
* **Remis** → „niejednoznaczne (remis) - do wyjaśnienia” i lista **wszystkich** równorzędnych faktur (przegląd 4A/4B,
  pkt 10): karta sama liczy otwarte faktury kontrahenta (kanoniczny kontrahent, ten sam kierunek) z otwartym saldem
  równym kwocie przelewu - nie tylko dwie, które silnik zostawił w rangach 2-3. Wybór faktury: „Inne…”.
* **Narracja modelu** (podsumowanie z `l1_semantics`) jest tylko pod „Szczegóły”; dla tytułu wrażliwego jej nie ma.
* Siła powiązań kandydata na obiekt to słowo (mocne ≥ 0,9 / średnie ≥ 0,7 / słabe) - liczba zostaje w bazie.

## 2. Kolejność i powód rangi

`build.py` liczy `priority` = klasa + `10 · log10(1 + kwota w zł)` (najwyżej +99) i zapisuje `rank_reason` po polsku.
Najpierw to, co blokuje metryki i kosztuje najwięcej przy pomyłce:

| Klasa | Karta | Przykładowy powód rangi |
| --- | --- | --- |
| 900 | kandydat na obiekt kosztowy | „kandydat na obiekt kosztowy blokuje alokacje: Gala ACME 2026, 29 rekordów, przychód 90 000,00 zł, koszt 60 156,40 zł” |
| 800 | wypływ bez dokumentu / do wyjaśnienia | „duży wypływ bez dokumentu: 7 240,18 zł (Anna Mazur) - blokuje koszty bez dokumentu” |
| 780 | wpływ, którego kandydatem jest przeterminowana należność | „przeterminowana należność bez dopasowania: FV 4/09/2026 (38 dni po terminie), wpływ 5 000,00 zł” |
| 700 | audyt automatu | „audyt automatu (2026-10): decyzja reguły builtin:…, 31 104,00 zł - potwierdź albo oznacz błąd” |
| 650 | pozostałe dopasowania (remis +5) | „remis - niejednoznaczne: 4 500,00 zł, Kadr Studio…” |
| 640 | przelew, któremu odrzuciłeś wszystkie propozycje | „odrzucone wszystkie propozycje: … - „Inne…”” |
| 600 / 560 | alokacja (UNASSIGNED rank 1 niżej) | „alokacja 18 450,00 zł: Helios Partner Summit 2026” |
| 550 | słowo wrażliwe | „słowo wrażliwe - kategorię nadaje człowiek (model tego nie czytał)” |
| 500 | możliwy duplikat dokumentu | |
| 400 | reguła gotowa do auto | „reguła R-3 gotowa do auto: 73 zgodnych z 73, dolna granica 0,950” |
| 300 | kandydat na regułę | „kandydat na regułę: 12 z 12 - Czy koszty od Orange Polska S.A. zawsze idą w 100% do „UNASSIGNED”?” |

Karta alokacji przelewu z tagiem `awaiting_matching` nie pojawia się, dopóki dopasowanie nie da mu statusu
końcowego (najpierw rozstrzygamy, czy przelew ma dokument). Odłożone karty idą na koniec do swojej daty. Budowa jest
idempotentna: istniejąca karta zachowuje pierwszy czas pokazania i odłożenie; karta, której sprawę rozstrzygnięto
gdzie indziej (CLI, automat), wychodzi z kolejki (`outcome = resolved_elsewhere`). Zatwierdzenie (albo odrzucenie)
ostatniego kandydata na obiekt od razu odświeża alokacje (`run_allocation` 4B) i kolejkę - propozycje alokacji
czekały na ten obiekt.

## 3. Przyciski → decyzje

`kasa.core.queue.actions.answer(conn, item_id, action, *, decided_by, suggestion_id=None, extra=None,
reason=None, until=None)`; pole „dlaczego” (`reason`) jest zawsze opcjonalne.

| Przycisk | Funkcja | Efekt |
| --- | --- | --- |
| Akceptuj / alternatywa | `decisions.accept_suggestion(conn, suggestion_id, *, decided_by, reason, extra)` | po `kind`: `apply_match` (4A), `apply_allocation` (4B; `extra.shares` uzupełnia propozycję częściową), `decide_category`, karta kandydata (`extra.action` = approve / split / merge / reject / distinct), karta reguły (accept / correct / reject), duplikat |
| Inne… | `decisions.decide_other(conn, company_id, subject_type, subject_id, *, decided_by, kind, targets, subtype, shares, category, reason)` | własna decyzja: `matched` (dokumenty / wiersze arkusza / przelew wskazane przez Ciebie; kwota jednego = kwota przelewu), `no_document` (**podtyp obowiązkowy**; zaliczka czeka 180 dni, karta 30), `own_transfer`, `to_clarify`, `allocation` (udziały = 100%), `category` |
| Odrzuć | `decisions.reject_suggestion` | propozycja `rejected` (4A jej nie powtórzy); karta zostaje z pozostałymi propozycjami albo z „Inne…”; na karcie kandydata / reguły = decyzja „odrzuć” |
| Odłóż | `decisions.defer_suggestion(until)` | `deferred_until` (domyślnie jutro), karta na koniec |
| Paczka | `decisions.bulk_accept(conn, suggestion_ids, decided_by)` | tylko jednorodna paczka (niżej) |
| To nie jest wrażliwe | `decisions.not_sensitive` | słowo dodane przez człowieka → `retired_at`; słowo z rdzenia/rozszerzenia → wyjątek dla podmiotu (`sensitive_exception`) |
| Potwierdź / Błąd | `rules.audit.verdict` | błąd = cofnięcie decyzji automatu + zawieszenie reguły z post-mortem |
| Włącz auto | `rules.lifecycle.enable_auto` | tylko gdy reguła gotowa |

Decyzja „bez dokumentu” zawsze ma podtyp - na karcie, w „Inne…”, w API (422) i w etykietach zbiorów kontrolnych
(`control_sets.TransactionLabel` odrzuca `no_document` bez podtypu) (przegląd 4A/4B, pkt 11).

„Inne… → dopasuj dokument” zapisuje Twój wybór jako sugestię kolejki (`source = queue.other`, `method = code`) i
od razu przyjmuje ją przez `apply_match` - jedna ścieżka zapisu dopasowań (grupy MPP, kwoty po stronie dokumentu,
odrzucenie pozostałych propozycji). Takiej sugestii cofnięcie nie przywraca.

## 4. Paczka zbiorcza

Klucz paczki (`decisions.batch_key`): rodzaj | reguła albo twardy tag (`detail.rule_key`, `rule:*`, `ksef_number`,
`mpp_block`, `invoice_number`, …) | **kanoniczny kontrahent** (12 przelewów Orange na 12 różnych rachunków wirtualnych
to jeden kontrahent z NIP-em) | rozstrzygnięcie. Tylko otwarte propozycje rank 1 z odznaką „wysoka”. Karta pokazuje
„Paczka: 12 przelewów Orange Polska S.A., 4 380,00 zł, builtin:match_invoice_number_same_counterparty - wszystkie
„wysoka””. Każda pozycja to osobna decyzja (osobny wpis w dzienniku, wspólny `batch_id`); paczka niejednorodna
(inny kontrahent, reguła, odznaka) jest odrzucana. Cofnięcie paczki (`revoke_batch`) cofa dokładnie te decyzje.

## 5. Cofnięcie

* **Toast 10 s**: po decyzji karta przechodzi do następnej z paskiem „Cofnij” (`undo_until = decided_at + 10 s`
  zapisuje baza triggerem dla każdej decyzji człowieka); kilka linii JS chowa pasek po czasie.
* **Historia** (`/queue/history`, `queue history`): każda decyzja z „Cofnij” (i „Cofnij paczkę”), bez limitu czasu.
* `revoke_decision(conn, decision_id, decided_by, reason)` = **nowa decyzja `revoke`** (nic nie kasujemy):

| Rodzaj | Co się dzieje |
| --- | --- |
| `match` | wiersze `l3_match` i `l3_transaction_status` tej decyzji (wszystkie nogi MPP) → `revoked`; saldo faktury wraca (liczymy tylko `active`); propozycje rozstrzygnięte tą decyzją wracają jako nowe otwarte wiersze (`restored_from`) - przelew jest znów w kolejce |
| `allocation` | wiersze `l3_allocation` → `revoked`; propozycje wracają |
| `category` | `l3_category_decision` → `revoked`; karta wraca |
| `cost_object` | zatwierdzenie / odrzucenie → znów kandydat (tylko gdy obiekt nie ma aktywnych alokacji); podziału, połączenia i „na pewno różne” nie cofamy - decydujesz od nowa |
| `rule` | etap reguły wraca (albo wersja utworzona decyzją → zawieszona, karta kandydata wraca) |

Dodatkowo: obserwacje reguł z tej decyzji są wycofywane (reguła traci punkt, w etapie auto poniżej progu → zawieszona),
a cofnięcie **decyzji automatu** to poprawka: reguła (także wbudowana) zostaje zawieszona ze szkicem post-mortem.

## 6. Wartownik i alarm 0,8 s

* Co ok. 50 kart (`round(n / 50)`, min. 1 przy ≥ 10 kartach nieodłożonych) `build` wstawia wartownika: przelew ze
  znaną odpowiedzią - Twoją wcześniejszą decyzją (`expected_source = decision`) albo etykietą zbioru kontrolnego
  (`control_set`), pokazany z jego własnymi propozycjami. Karta wygląda jak każda inna; odpowiedź nie jest decyzją,
  trafia do `queue_sentinel` (`passed`), odpowiedź API jest identyczna („Zapisano”), wynik widać tylko w KPI.
* `shown_at` = **pierwsze** pokazanie karty (`COALESCE`; przeładowanie nie zeruje zegara - przegląd fazy 3, pkt 8),
  `duration_ms` = decyzja − pierwsze pokazanie. Decyzja szybsza niż 0,8 s na karcie z odznaką „niska” →
  `fast_alarm` i ostrzeżenie w `queue stats`.

## 7. KPI (`queue stats`, `/queue/stats`)

Minuty Twojej pracy (suma `duration_ms`, dziennie), mediana sekund na kartę, **błędy ważone kwotą** (błędy audytu +
cofnięcia po oknie 10 s, ważone kwotą podmiotu; cofnięcia w 10 s liczone osobno jako pomyłki kliknięcia), audyt,
wartownicy, alarmy, liczba decyzji człowieka i automatu. Nigdy „procent obsłużony przez AI”.

## 8. Panel na telefonie

```bash
uv pip install --system fastapi uvicorn jinja2 python-multipart    # raz
python -m kasa queue build --company odm                           # kolejka (też po każdym match/allocate run)
python -m kasa web --host 0.0.0.0 --port 8787 --user artur         # wypisze: Panel: http://192.168.x.y:8787/?token=…
```

Otwórz wypisany adres na telefonie w tym samym Wi-Fi (token zapisze się w ciasteczku na 30 dni; stały:
`KASA_WEB_TOKEN`). Strona główna to kolejka (`/queue`), dalej „Historia” i „KPI”. Poza siecią domową tylko przez VPN
(np. Tailscale) albo odwrotne proxy z TLS (np. Caddy `reverse_proxy 127.0.0.1:8787`) - panel pokazuje dane jawne.

API JSON (dokumentacja `/api/docs`):

| Metoda i ścieżka | Co robi |
| --- | --- |
| `GET /api/queue?company=odm` | karty w kolejności (pozycja, rodzaj, powód rangi, odznaka, paczka) |
| `POST /api/queue/build` `{company?, min_months?}` | przebuduj kolejkę |
| `GET /api/queue/next` | następna karta (zapisuje pierwsze pokazanie) |
| `GET /api/queue/items/{id}` | karta |
| `GET /api/queue/items/{id}/documents?q=` | wyszukiwarka dokumentów do „Inne…” |
| `POST /api/queue/items/{id}/accept` `{suggestion_id?, reason?, extra?}` | Akceptuj / alternatywa / przyciski kandydata (`action`) |
| `POST /api/queue/items/{id}/reject` / `defer` `{until?}` / `bulk` | Odrzuć / Odłóż / paczka |
| `POST /api/queue/items/{id}/other` `{kind, targets?, subtype?, shares?, category?}` | „Inne…” (422 z opisem, np. brak podtypu) |
| `POST /api/queue/items/{id}/answer` `{action, …}` | dowolny przycisk (audyt: `confirmed` / `error`, `not_sensitive`, …) |
| `POST /api/queue/undo` `{decision_id | batch_id}` | cofnij decyzję / paczkę |
| `GET /api/queue/history`, `GET /api/queue/stats` | historia z cofaniem, KPI |

## 9. CLI

```bash
python -m kasa queue build [--company odm] [--min-months 2] [--no-mine] [--no-auto]
python -m kasa queue list [--limit 20] [--all]              # z powodem rangi
python -m kasa queue show [next|<pozycja>|<id>] [--json]
python -m kasa queue accept <karta> [--suggestion 2] [--bulk] [--category X] [--action approve|split|…] [--extra JSON]
python -m kasa queue reject|defer <karta> [--until 2026-11-20]
python -m kasa queue other <karta> --kind matched|no_document|own_transfer|to_clarify|allocation|category \
       [--document document:<id>] [--subtype zus] [--share <obiekt>=0.5] [--category X]
python -m kasa queue undo <decyzja>|batch:<id>
python -m kasa queue history [--auto]
python -m kasa queue stats [--json]
```

Karta: `next`, pozycja z `queue list` (stała do następnego `build`) albo id (min. 6 znaków).

## 10. Strażnik KS003 (przegląd fazy 1, pkt 13)

`kasa_l3_guard` (0011): UPDATE na L3 zapisany jako `method = 'human'` musi nieść **nową decyzję** (`decision_id`)
albo cofnięcie (`revoked_by_decision`); bez nich - KS003. Zmienić same kolumny księgowe (`updated_at`, `updated_by`,
`last_hit_at`) wolno. Kod 4A/4B już tak pisał (cofnięcia, zastąpienie alokacji, decyzje o kandydatach); poprawione
zostały tylko testy, które zmieniały etap reguły bez decyzji (`tests/matching/test_engine_units.py`,
`test_schema_and_fx.py`, `tests/allocation/test_allocation.py`, `tests/gates/test_gate1.py` - teraz z wpisem
w dzienniku, a bramka 1 sprawdza też odmowę bez decyzji).

## 11. Wyniki na danych testowych (bramka 4)

Świeża baza, pełny potok: kolejka 141 kart (35 dopasowań, 5 kandydatów, 97 alokacji, 1 słowo wrażliwe, 3
wartowników). Symulacja użytkownika klika kartami zgodnie z `ground_truth.json`: 5 zatwierdzeń kandydatów, 107
„Akceptuj” (pierwsza albo inna propozycja), 1 „Inne…” (komornik: bez dokumentu / inne), 1 kategoria, 8 wartowników
(wszystkie dobrze); zostaje 21 kart, których plik prawdy nie rozstrzyga (19 dokumentów historycznych z JPK, 2 przelewy
do wyjaśnienia). Wynik: 0 fałszywych automatów (45 dopasowań, 21 alokacji), 5 obiektów zatwierdzonych kartą, raport
firmy z różnicą kontrolną 0,00 w obu ujęciach i marżami 5 eventów co do grosza, kontrole prawdy 1-5 zielone, paczka 12
przelewów Orange (4 380,00 zł) przyjęta i cofnięta jednym ruchem.

## 12. Decyzje własne [4C] i ograniczenia

* Kolejka trzyma stan w `queue_state` (nie widok): pierwsze pokazanie, odłożenie i czas decyzji muszą przetrwać
  przebudowę. Wiersze nigdy nie są kasowane (`closed_at`).
* Odrzucenie propozycji dopasowania/alokacji nie jest decyzją L3 (podmiot czeka dalej); kto odrzucił - w
  `evidence.rejected_by` sugestii i w `queue_state`.
* Cofnięcie przywraca propozycje jako **kopie** (nowe wiersze `open`, `restored_from`), bo sugestia nie zmienia
  statusu wstecz; następny przebieg 4A/4B porównuje je jak swoje.
* „Inne… → dokument” przechodzi przez sugestię kolejki (`queue.other`) i `apply_match` - zamiast drugiej ścieżki
  zapisu `l3_match`.
* Po ostatnim kandydacie na obiekt kolejka sama uruchamia `run_allocation` (z automatem sygnału A) - inaczej karty
  alokacji pokazywałyby propozycje sprzed zatwierdzenia.
* „To nie jest wrażliwe” dla słowa z rdzenia zapisuje wyjątek (`sensitive_exception`), ale semantyka fazy 3 go
  jeszcze nie czyta (ponowny odczyt modelu tego podmiotu - faza 5); słowo człowieka jest wycofywane od razu.
* Wartownicy ze zbioru kontrolnego pokazują propozycje przelewu rozstrzygniętego już automatem - na danych
  testowych innych przelewów ze znaną odpowiedzią nie ma.
* Remis z więcej niż trzema fakturami: karta liczy równorzędne faktury sama (silnik trzyma najwyżej 3 propozycje).
* Alarm 0,8 s liczy się tylko dla kart pokazanych (`shown_at`); decyzje z CLI bez `queue show` nie mają czasu.
