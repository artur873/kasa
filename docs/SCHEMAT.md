# Schemat bazy (fazy 1-5)

Migracje: `migrations/0001_core.sql` … `0011_phase4_queue_rules.sql`, `0012_phase5_metrics.sql` (faza 5) i `0013_matching_fixes.sql`,
stosowane przez `python -m kasa migrate` w kolejności numerów; brakujący wcześniejszy numer zastosowany później
nie przeszkadza (0013 nie zależy od 0012) (każdy plik w osobnej transakcji, zapis w `schema_migrations` z sumą
SHA-256; zmiana zastosowanego pliku = błąd).
Rozszerzenia PostgreSQL: `pgcrypto` (od 0006, szyfrowanie kluczy pseudonimów; `gen_random_uuid()` jest wbudowane).
Sekcje do „Zasad triggerów” opisują fazę 1; tabele faz 2 i 3 są opisane na końcu.

## Wspólne

| Obiekt | Co to jest |
| --- | --- |
| `company` | Firma (najemca systemu): `slug`, nazwa i `extension_slug` - rozszerzenie, które mówi, czym jest jej obiekt kosztowy. |
| `provenance_method` | Metoda zapisu: `code` (parser), `rule` (automat z reguły), `model` (AI), `human` (człowiek). |
| `subject_type` | Rodzaj podmiotu w odwołaniach polimorficznych: `transaction`, `document`, `line`, `internal_record`, `cost_object`, `rule`. |

**Kolumny pochodzenia** - każdy wiersz L1-L3 ma: `company_id`, `source` (skąd: `ksef`, `enablebanking`, `queue`…),
`method`, `confidence` (0-1, skalibrowana pewność, może być NULL), `evidence` (jsonb z dowodem), `schema_version`,
`prompt_version` (obowiązkowe, gdy `method = 'model'`), `created_at`, `created_by`. Tabele L3 mają dodatkowo
`updated_at` (ustawia trigger) i `updated_by`. `company_id` jest też w tabelach podrzędnych - złożone klucze obce
`(id, company_id)` pilnują, że wiersz nigdy nie wskazuje na dane innej firmy.

## L0 - surowe

| Tabela | Opis |
| --- | --- |
| `l0_raw` | Plik dokładnie taki, jak przyszedł (XML KSeF, JSON z banku, PDF, arkusz), z kanałem (`bank`/`ksef`/`file`/`pdf`/`email`), `external_ref` i `sha256`, które baza sama przelicza z `payload`; `UNIQUE (company_id, sha256)` sprawia, że ponowne pobranie nie tworzy duplikatu. |

## L1 - fakty

| Tabela | Opis |
| --- | --- |
| `l1_document` | Faktura lub inny dokument (wersja rekordu): tożsamość (numer KSeF, numer surowy i znormalizowany, rodzaj, skrót pliku, program wystawcy), daty, strony, kwoty w groszach waluty dokumentu z kursem i PLN jako pochodną, powiązania (korekta, zaliczka, zamówienie, umowa), flagi MPP / metoda kasowa / odwrotne obciążenie oraz `superseded_by`. |
| `l1_document_line` | Pozycja dokumentu (nazwa, ilość, jednostka, cena, netto, stawka, VAT, brutto; dla korekty stan przed/po w `before_json`/`after_json`). |
| `l1_document_observation` | Jedna obserwacja dokumentu w jednym surowym pliku (`raw_id` + `locator` w środku pliku) z informacją, po czym ją dopięto (`linked_by`: utworzyła dokument, numer KSeF, skrót pliku, NIP+rodzaj+numer, człowiek) i polami tak, jak je widział ten kanał - to jest deduplikacja: trzy obserwacje, jeden dokument. |
| `l1_bank_account` | Rachunek firmy (IBAN, waluta, bank, rodzaj `current`/`vat`/`savings`, rachunek VAT wskazuje swój bieżący przez `parent_account_id`). |
| `l1_bank_transaction` | Operacja bankowa: `entry_reference` banku i własny `fingerprint`, daty, kwota ze znakiem, waluta i kurs, saldo po, nadawca i tytuł w wersji surowej, `extracted` (to, co kod wyłuskał z tytułu), typ operacji i status `booked`/`pending`; zaksięgowana operacja zastępuje oczekującą przez `superseded_by`. |
| `l1_fx_rate` | Kurs waluty z dnia (PLN za 1 jednostkę); `company_id = NULL` oznacza dane wspólne, np. tabelę A NBP. |
| `l1_title_semantics` | Odczyt tytułu przelewu przez model (intencja, encje), jeden wiersz na transakcję i wersję promptu; tylko `method = 'model'`. |
| `l1_internal_record` | Wiersz pliku wewnętrznego (arkusz PM, lista czynszów) odczytany mapowaniem z rozszerzenia: wszystkie pola w `fields`, a w kolumnach rdzenia osoba (pseudonim), data, kwota, waluta, odwołanie do obiektu kosztowego tak, jak je zapisano, i typ rozliczenia. |

Wersjonowanie: nowa wersja parsera lub promptu dopisuje wiersz i ustawia `superseded_by` w starym. Ograniczenia
wykluczające (`EXCLUDE … DEFERRABLE INITIALLY DEFERRED`, sprawdzane przy commit) pilnują, że wśród wierszy bieżących
(`superseded_by IS NULL`) jest najwyżej jeden dokument o danym numerze KSeF w firmie, jedna operacja o danym
`entry_reference` i jedna o danym `fingerprint` na rachunku, i jeden wiersz pliku wewnętrznego na (plik, arkusz, wiersz).
`fingerprint` musi więc zawierać numer kolejny identycznych operacji z tego samego dnia. Reguły nie piszą faktów
(`method <> 'rule'`).

## L2 - sugestie

| Tabela | Opis |
| --- | --- |
| `l2_suggestion` | Propozycja kodu, reguły lub modelu (`method <> 'human'`): rodzaj (`match`, `allocation`, `category`, `cost_object_candidate`, `rule_candidate`, `counterparty`), podmiot, `proposal`, surowy `score`, `rank` i status `open`/`accepted`/`rejected`/`expired` z decyzją, która ją rozstrzygnęła. |

## L3 - decyzje

| Tabela | Opis |
| --- | --- |
| `l3_rule` | Reguła nauczona z decyzji, wersjonowana (`UNIQUE (company_id, rule_key, version)`), z etapem `candidate`/`hint`/`shadow`/`auto`/`suspended`/`dormant`, wzorcem, akcją, liczbą dowodów i wyjątkami; kod może zaproponować tylko kandydata, każdy dalszy etap ustawia człowiek. |
| `l3_decision_log` | Dziennik wszystkich decyzji (człowiek albo reguła `auto`): rodzaj, podmiot, `payload`, reguła z wersją, kto i kiedy, cofana decyzja (`kind = 'revoke'`), odpowiadana sugestia i opcjonalny powód; tylko dopisywanie. |
| `l3_cost_object` | Obiekt kosztowy firmy; `kind` i schemat `attrs` nadaje rozszerzenie (ODM `event`, Rentease `room_month`), status `candidate`/`approved`/`rejected`/`merged`; każda firma ma systemowy wiersz `UNASSIGNED` (rodzaj `unassigned`), tworzony triggerem przy wstawieniu firmy. |
| `l3_match` | Dopasowanie przelewu do dokumentu albo wiersza pliku wewnętrznego, z kwotą po stronie przelewu i dokumentu, nazwaną różnicą (`fee`/`fx`/`underpayment`/`overpayment`/`tolerance`) i `group_id` spinającym jedno rozliczenie (trzy faktury w jednym przelewie, dwie nogi MPP). |
| `l3_allocation` | Przypisanie przelewu, dokumentu, pozycji albo wiersza wewnętrznego do zatwierdzonego obiektu kosztowego w udziale `share` (0-1]. |
| `l3_category_decision` | Kategoria podmiotu (nazwy kategorii z rozszerzenia); najwyżej jedna aktywna na podmiot. |
| `l3_transaction_status` | Status końcowy przelewu: `matched` / `no_document` (z podtypem) / `own_transfer` / `to_clarify`; najwyżej jeden aktywny na przelew. |

Decyzje `l3_match`, `l3_allocation`, `l3_category_decision`, `l3_transaction_status` mają `decision_id` (wpis w
dzienniku), `status` `active`/`revoked` i `revoked_by_decision`. Cofnięcie = nowy wpis `revoke` w dzienniku plus
`UPDATE … SET status = 'revoked', revoked_by_decision = …, method = 'human'`.

## Zasady triggerów (pilnuje baza, nie aplikacja)

| Kod | Zasada | Gdzie |
| --- | --- | --- |
| `KS001` | Tylko INSERT: UPDATE i DELETE zgłaszają wyjątek. | `l0_raw`, `l3_decision_log` |
| `KS002` | Nic nie kasujemy: DELETE i TRUNCATE zgłaszają wyjątek. | wszystkie tabele L1, L2, L3 (TRUNCATE także L0) |
| `KS003` | Wiersz z `method = 'human'` może zmienić tylko zapis z `method = 'human'` (UPDATE dozwolony, gdy `NEW.method = 'human'` albo `OLD.method <> 'human'`). Od 0011: zapis `human` musi nieść nową decyzję (`decision_id`) albo cofnięcie (`revoked_by_decision`); same kolumny księgowe (`updated_at`, `updated_by`, `last_hit_at`) wolno zmienić bez niej. | wszystkie tabele L3 |
| `KS004` | Zapis z `method = 'rule'` musi wskazać `rule_id` + `rule_version` reguły tej samej firmy w etapie `auto` (wiersz reguły blokowany `FOR SHARE`, więc zawieszenie reguły czeka na zapis automatu). Gdy reguła przestanie być `auto`, automat nie zmieni już nawet swoich wierszy; człowiek może je cofnąć. | wszystkie tabele L3 |
| `KS005` | Podmiot polimorficzny (`subject_type`, `subject_id`) musi istnieć w tej samej firmie. | `l2_suggestion`, `l3_decision_log`, `l3_allocation`, `l3_category_decision` |
| `KS006` | `company_id` istniejącego wiersza nie może się zmienić. | wszystkie tabele L3 |
| `KS007` | Aktywna alokacja może wskazywać tylko obiekt kosztowy `approved`. | `l3_allocation` |

Ograniczenia CHECK uzupełniają triggery: L3 przyjmuje tylko `human` i `rule`, z dwoma wyjątkami - kandydat na obiekt
kosztowy (`code`/`model`, status `candidate`) i kandydat na regułę (`code`, etap `candidate`); obiektu kosztowego nie
zatwierdza, nie łączy ani nie odrzuca automat. Kontrola `python -m kasa check` sprawdza, że wszystkie triggery istnieją
i są włączone oraz że każda tabela L1-L3 ma komplet kolumn pochodzenia.

## Faza 2 - bank i kontrahenci (`0006_phase2_bank.sql`)

| Obiekt | Co to jest |
| --- | --- |
| `l0_raw.purged_at`, `l0_purge_log` | Retencja L0: jedyna dozwolona zmiana surowego rekordu to opróżnienie treści przez `kasa_purge_raw(id, powód)` (skrót SHA-256 i metadane zostają), każde opróżnienie ma wpis w dzienniku (tylko dopisywanie). |
| `pseudonym_key` | Klucz pseudonimów: firma, rodzaj (`person`/`iban`/`phone`/`email`/`pesel`/`address`), kod (`P-7f3a`), HMAC wartości i wartość zaszyfrowana pgcrypto; wolno tylko zniszczyć wartość (`shredded_at`, KS008), nic nie kasujemy. |
| `l1_counterparty` | Kontrahent jako fakt (NIP, numer zagraniczny, nazwa, IBAN-y, rodzaj `company`/`person`/`institution`/`bank`/`own`); jedyna droga zapisu to `kasa_upsert_counterparty()` (NIP → numer zagraniczny → IBAN → nazwa), firma sama dla siebie to `kasa_own_counterparty()`. |
| `l1_bank_account` (+ kolumny) | Rachunek: dostawca, identyfikator u dostawcy, BIC, właściciel. |
| `l1_bank_transaction` (+ kolumny) | Szeroki rekord przelewu: data operacji, kod i opis operacji banku, MCC, referencja, BIC nadawcy, saldo liczone przez nas, `counterparty_id`, tytuł/nadawca/IBAN po pseudonimizacji, flaga `sensitive`, split payment (`mpp_group_id`, `mpp_role`), surowy plik i miejsce w nim. |
| `l1_bank_balance` | Saldo zgłoszone przez bank (`OPBD`/`CLBD`/`PRCD`/`ITAV`/`CLAV`) na dzień; nowa wartość zastępuje starą przez `superseded_by`. |
| `l1_document.counterparty_id`, format numeru KSeF | Druga strona dokumentu jako kontrahent; CHECK formatu numeru KSeF. |
| `truth_check_result` | Wynik każdej kontroli prawdy (ciągłość sald, MPP, kompletność KSeF) per firma, zakres i okres; tylko dopisywanie. |

## Faza 2 - dokumenty (`0007_phase2_documents.sql`)

| Obiekt | Co to jest |
| --- | --- |
| `company.nip` | NIP firmy - rozstrzyga kierunek faktury (Podmiot1 = my → sprzedaż). |
| `l1_document.extra`, `l1_document_line.extra`, `line_kind` | Pola źródła bez własnej kolumny („zapisujemy wszystko”); rodzaj pozycji `invoice`/`order`/`correction`. |
| `l1_document_observation.parser_version` | Wersja parsera, która czytała plik (idempotentny import, nowa wersja = nowa wersja dokumentu). |
| `document_lineage` (widok) | Każda wersja dokumentu → bieżąca głowa łańcucha (`head_id`). |
| `import_mapping_profile` | Pamięć kreatora importu: mapowanie kolumn potwierdzone raz dla plików o danych nagłówkach; zmiana = nowy profil z `superseded_by`. |
| `l1_pdf_extraction` | Odczyt PDF raz na (plik, model, wersję promptu): skrót tekstu wysłanego do modelu, wynik pseudonimizacji, kontrola sumy pozycji, `needs_review`. |
| `document_kind.bill`, `suggestion_kind.duplicate` | Rachunek osoby; sugestia „to może być duplikat dokumentu X”. |

## Faza 3 - semantyka i zbiory kontrolne (`0008_phase3_semantics.sql`)

| Obiekt | Co to jest |
| --- | --- |
| `l1_semantics` | Odczyt tekstu podmiotu (`transaction`, `line`, `internal_record`, `document`) przez model albo kod: cel (`title_intent`, `line_entities`), intencja, encje (teksty jako `{"untrusted_text": …}`), podsumowanie, model, wersja promptu, **dokładnie to, co widział model** (`input_pseudonymized`) i id wywołania; jeden bieżący wiersz na (podmiot, cel, wersję promptu, model), nowsza wersja zastępuje starszą przez `superseded_by` (przegląd fazy 1, pkt 12). |
| `title_semantics_current` (widok) | Najnowszy bieżący odczyt tytułu każdego przelewu (z `l1_semantics`, a gdzie go nie ma - z `l1_title_semantics` fazy 1, która zostaje). |
| `llm_call_log` | Każde wywołanie modelu, także zablokowane: dostawca, model, wersja promptu, cel, podmiot, SHA-256 i długość promptu (nigdy tekst), skrót odpowiedzi, czas, koszt, status `ok`/`blocked`/`error` i powód; tylko dopisywanie. |
| `llm_call_payload` | Pełny prompt po pseudonimizacji i odpowiedź, 90 dni (`expires_at`); jedyna zmiana to wygaszenie przez `kasa_expire_llm_payloads()` (KS010). |
| `control_set` | Zbiór kontrolny firmy (`transaction`/`cost`), docelowy rozmiar, ziarno losowania, zamrożenie i zakończenie (tylko raz każde, KS009). |
| `control_set_item` | Pozycja zbioru: podmiot, kolejność, etykieta (prawda referencyjna, nie decyzja), kto i kiedy, czas na odpowiedź (`duration_ms` od `shown_at`), wartownik ze znaną odpowiedzią i jego wynik; podmiot, kolejność i wartownik niezmienne, do zamrożonego zbioru nic nie dochodzi (KS009). |
| `control_set_label_history` | Każda zmiana nadanej już etykiety (stara i nowa, kto, kiedy) - zapisuje ją trigger; tylko dopisywanie. |
| `sensitive_term` | Słowo wrażliwe jako rdzeń z odmianą (`stem`), kategoria, źródło `core`/`extension`/`human`, firma (`NULL` = wszystkie); zasilane przez `init-db`, wycofanie przez `retired_at` (KS011; przegląd fazy 1, pkt 15). |
| `l1_pdf_extraction.method` | Dopuszcza `code` - odczyt regexem modelu testowego to kod, nie model (przegląd fazy 2). |
| `company.nip_evidence` | Skąd i kiedy ustalono NIP firmy przy pierwszym ustawieniu (źródło, `raw_id`). |
| `pseudonym_key.basis` | Dlaczego wartość uznano za osobę (`registered`, `first_name`, `context`, `known_person`); klucze sprzed 0008 (`NULL`) są używane jako osoby tylko z dowodem (imię, arkusz, kontrahent-osoba). |

Nowe kody strażników: `KS008` klucz pseudonimu tylko do zniszczenia (0006), `KS009` zbiór kontrolny (zamrożenie,
niezmienna tożsamość pozycji), `KS010` wygaszanie payloadów modelu tylko funkcją, `KS011` słowo wrażliwe tylko
do wycofania.

## Faza 4 - dopasowanie (`0009_phase4_matching.sql`)

Opis silnika: `docs/DOPASOWANIE.md`.

| Obiekt | Co to jest |
| --- | --- |
| `l3_transaction_status.awaiting_until` | Do kiedy przelew bez dokumentu czeka na dokument: zaliczka = data księgowania + 180 dni, karta = + 30 dni (przegląd fazy 1, pkt 5). |
| `l3_match.related_transaction_id` | Trzeci cel dopasowania obok dokumentu i wiersza wewnętrznego: zwrot ↔ pierwotna wpłata, duplikat wpłaty ↔ jego zwrot (przegląd fazy 1, pkt 11). CHECK: dokładnie jeden z trzech celów; przelew nie wskazuje sam siebie; najwyżej jedno aktywne powiązanie danej pary. |
| `match_difference_kind.correction` | Wiersz korekty w grupie dopasowań ma **ujemne** `amount_minor` i `document_amount_minor` (faktura 10 000 + korekta −2 000 = przelew 8 000; przegląd fazy 1, pkt 8). Saldo dokumentu = brutto łańcucha korekt − aktywne dopasowania; `status = 'revoked'` zdejmuje kwotę z salda. |
| `l1_bank_transaction.resolved_by_transaction_id` | Blokada kartowa (`pending`) rozliczona zaksięgowaną operacją - stan, nie nowa wersja faktu (`superseded_by` zostaje tylko dla wersji); CHECK: zaksięgowana operacja nigdy nie jest „rozliczona”. Stare blokady z `superseded_by` przeniesione migracją (przegląd fazy 2, pkt 5). |
| `bank_tx_status.expired` | Blokada, która wygasła bez księgowania. |
| `calibration_result` | Wynik kalibracji progów automatu na zbiorze kontrolnym: `run_id`, firma, zbiór, `kind` (`match`), `params` (temperatura, przesunięcie, próg, minimalna przewaga), `metrics` (automaty, fałszywe automaty, zgodność kolejki, log-loss), `chosen` (najwyżej jeden na przebieg), `run_at`; tylko dopisywanie (KS001). Silnik czyta wybrane parametry ostatniego przebiegu. |
| `kasa_seed_builtin_rules(company_id)`, trigger `company_builtin_rules` | Reguły wbudowane z briefingu (krok 4) w `l3_rule` dla każdej firmy, także nowej: `builtin:match_ksef_number`, `builtin:match_mpp_block`, `builtin:match_invoice_number_same_counterparty`, `builtin:match_single_open_invoice_exact_amount`, `builtin:own_transfer`, `builtin:no_document_zus_tax_fee`; etap `auto`, `method = 'human'`, `created_by = 'briefing-2026-11-03'`, w `evidence` cytat z Kroku 4. Strażnicy bez zmian: regułę zawiesza tylko człowiek (KS003), automat pisze tylko na regułę w etapie `auto` (KS004). |

## Faza 4 - kandydaci na obiekty kosztowe i alokacja (`0010_phase4_allocation.sql`)

Opis: `docs/ALOKACJA.md`.

| Obiekt | Co to jest |
| --- | --- |
| `l2_cost_object_member` | Członek kandydata na obiekt kosztowy (przegląd fazy 1, pkt 10): kandydat (`candidate_id` → `l3_cost_object`), rodzaj rekordu (`cost_object_member_type`: `document_line`/`document`/`transaction`/`internal_record`/`email_case`), id rekordu, siła powiązania (`link_strength` 0-1, z modelu par), `is_anchor` (najwyżej jedna żywa kotwica na kandydata), status `open`/`accepted`/`rejected`/`expired` z decyzją, która go rozstrzygnęła, i komplet kolumn pochodzenia (`method` `code`/`model`, `human` = człowiek przy podziale lub połączeniu). Rekord musi istnieć w tej samej firmie (trigger, KS005); nic nie kasujemy (KS002). |
| `l3_allocation.signal_strength`, `cascade_step` | Siła sygnału `A`-`D` (`NULL` = brak sygnału, UNASSIGNED albo podział człowieka) i krok kaskady faktury zbiorczej (`line_description`, `offer_match`, `offer_value`, `person_days`, `equal`, `inherited` - korekta dziedziczy podział faktury pierwotnej) (przegląd fazy 1, pkt 9). |
| `pair_model_weights` | Wagi modelu par Fellegi-Sunter: firma, cecha, poziom, `m_prob` = P(poziom \| ten sam obiekt), `u_prob` = P(poziom \| różne), `pseudo_count` (siła priora Dirichleta), wersja, liczba decyzji i par, na których uczono. Wersja 0 (`company_id IS NULL`) = priory z migracji; każde przeuczenie po decyzji człowieka dopisuje pełną nową wersję firmy. Także `__prior__` (udział par prawdziwych), `__threshold__` (progi grupowania: `merge`, `min_link`, `member`, `high`, `margin`) i `__param__` (`rare_df_ratio`). Tylko dopisywanie (KS001). |
| `cost_object_pair_verdict`, `cost_object_pair_decision` | Werdykt człowieka o parze obiektów (`a_id < b_id`): `different` (przycisk „na pewno różne” i podział - odkrywanie nigdy ich nie skleja) albo `same` (połączenie); z decyzją w dzienniku; obowiązuje najnowszy werdykt pary. Tylko dopisywanie. |
| `l2_suggestion_subject_type_check` | Podmiotem sugestii może być też obiekt kosztowy (karta kandydata, `kind = cost_object_candidate`) i reguła (karta reguły, `kind = rule_candidate`). |
| `kasa_seed_builtin_allocation_rule(company_id)`, trigger `company_builtin_allocation_rule` | Reguła wbudowana `builtin:allocation_explicit_identifier` (etap `auto`, `method = 'human'`, `created_by = 'briefing-2026-11-03'`) dla każdej firmy, także nowej: automat alokacji tylko dla sygnału A bez sprzeczności (decyzja D3); zawieszenie reguły wyłącza automat. |

## Faza 4 - kolejka przeglądu i reguły (`0011_phase4_queue_rules.sql`)

Opis: `docs/KOLEJKA.md`, `docs/REGULY.md`.

| Obiekt | Co to jest |
| --- | --- |
| `l3_rule.question`, `scope`, `last_hit_at`, `suspended_reason`, `post_mortem` | Pytanie biznesowe karty; zakres `{"counterparties": [...], "subject_types": [...]}` (znani kontrahenci - nowy kontrahent = brak trafienia; `{}` = bez ograniczeń, reguły wbudowane); ostatnie trafienie (księgowość); powód zawieszenia i szkic post-mortem. Indeks `l3_rule_signature_key` (`evidence.signature` + wersja) - jeden kandydat na wzorzec. |
| `l2_rule_observation` | Obserwacja reguły (przegląd fazy 1, pkt 6): reguła z wersją, etap w chwili obserwacji, podmiot, decyzja człowieka, `predicted`, `actual`, `agreed`, `observed_at`; jedna na regułę i decyzję; jedyna zmiana to wycofanie po cofnięciu decyzji (`withdrawn_at`, `withdrawn_by_decision`, KS012); komplet kolumn pochodzenia (`method = code`). |
| `rule_stage_effective` (widok) | Etap bieżącej wersji każdej reguły liczony jako stan pochodny (K9): podpowiedź → cień po 30 obserwacjach, `ready_for_auto` przy dolnej granicy Wilsona ≥ 0,95 (73 zgodne przy 0 błędów), uśpiona po 12 mies. bez trafienia (obserwacja albo decyzja automatu), po uśpieniu znów podpowiedź; plus obserwacje, zgodne, niezgodne, decyzje automatu, ostatnie trafienie. |
| `l2_audit_sample` | Próbka audytu automatu (przegląd fazy 1, pkt 7): decyzja automatu, miesiąc (data podmiotu), reguła, podmiot, kwota, populacja, wielkość próbki, ziarno, werdykt `confirmed`/`error` (raz, KS012), kto i kiedy, decyzja przeglądu (cofnięcie przy błędzie). |
| `queue_state` | Karta kolejki: rodzaj (`match`, `allocation`, `category`, `sensitive`, `duplicate`, `cost_object_candidate`, `rule_candidate`, `rule_ready`, `audit`, `sentinel`), podmiot, propozycje (`suggestion_ids` wg rangi), `rank_reason` (po polsku), `priority`, `position`, odznaka, kwota, klucz paczki, `deferred_until`, `shown_at` (pierwsze pokazanie - `COALESCE`, przegląd fazy 3, pkt 8), `skipped_at`, `decided_at`, `decided_by`, `decision_id`, `outcome`, `duration_ms`, `fast_alarm` (< 0,8 s na karcie „niska”), `closed_at`. Najwyżej jedna otwarta karta na (firma, rodzaj, podmiot); nic nie kasujemy. |
| `queue_sentinel` | Wartownik kolejki: podmiot, pokazane propozycje, znana odpowiedź (`expected` z decyzji człowieka albo etykiety zbioru kontrolnego), odpowiedź, czas, `passed`; znana odpowiedź niezmienna, odpowiedź raz (KS012). |
| `l3_decision_log.undo_until`, `batch_id` | Do kiedy karta pokazuje toast „Cofnij” (trigger `l3_decision_log_undo_window`: decyzja człowieka + 10 s); paczka zbiorcza (każda pozycja osobną decyzją, wspólny `batch_id`). |
| `sensitive_exception` | „To nie jest wrażliwe” dla słowa z rdzenia/rozszerzenia: wyjątek dla jednego podmiotu (słowa, powód, kto); tylko dopisywanie. Słowo dodane przez człowieka jest zamiast tego wycofywane (`sensitive_term.retired_at`, przegląd fazy 3, pkt 3). |
| `kasa_l3_guard` (nowa treść) | KS003 zaostrzony (przegląd fazy 1, pkt 13) - patrz tabela zasad triggerów. |
| `decision_kind.duplicate` | Werdykt człowieka o sugestii duplikatu dokumentu. |

Nowy kod strażnika: `KS012` - obserwacja reguły, próbka audytu i wartownik kolejki zapisują wynik tylko raz.
`python -m kasa check` sprawdza także triggery 0011 (`kasa/core/checks.py`, dopisane na końcu).

## Poprawki przeglądu faz 4A i 4B (`0013_matching_fixes.sql`)

Opis: `docs/DOPASOWANIE.md` (rozdz. 5, 7). Migracja używa tylko obiektów 0001-0011 - jest niezależna od 0012
(faza 5), runner może ją zastosować przed nią.

| Obiekt | Co to jest |
| --- | --- |
| `l3_match.evidence_only` | „Koszt jeden, dowodów wiele” (Krok 5; przegląd 4A/4B pkt 6): wiersz arkusza PM, który dowodzi tego samego kosztu co dokument dopasowania, w **tej samej** `group_id` co wiersz dokumentu, z `evidence_only = true` i kwotą 0 (`evidence.evidence_of` = dokumenty grupy). Pisze go `kasa.core.matching.decide.apply_match` z `linked_evidence` sugestii. Kontrola prawdy 2 go pomija; saldo wiersza arkusza jest nim zamknięte (koszt rozliczony przez dokument); cofnięcie decyzji cofa go razem z grupą. |
| `l3_match_evidence_only_check` | `evidence_only` tylko z `internal_record_id`, `amount_minor = 0`, `document_amount_minor` 0/NULL i bez nazwanej różnicy. |
| `l3_match_evidence_group_idx` | Indeks częściowy `group_id WHERE evidence_only` (raport: dowody grupy). |

Bez zmian schematu (kod): `l1_counterparty.ibans` uzupełniany przez `kasa_upsert_counterparty` także po pierwszej
decyzji człowieka, która dopasowała dokument kontrahenta znanego bankowi tylko z nazwy (pochodzenie:
`l3_decision_log.evidence.iban_linked`); `calibration_result.metrics` z walidacją krzyżową (`labels`,
`false_auto`, `upper_bound_error_wilson95`, `trustworthy`, `folds`, `nested`); `l1_bank_transaction` w wersji
parsera 3 (`extracted.payer_hint`, `sensitive` z tytułu i nazwy kontrahenta listą bramki).

## Faza 5 - metryki, agent, dashboard, eksport, retencja (`0012_phase5_metrics.sql`)

Opis: `docs/METRYKI.md`, `docs/AGENT.md`, `docs/DASHBOARD.md`.

| Obiekt | Co to jest |
| --- | --- |
| schematy `metrics`, `views`, `evidence` | Jedyne schematy roli agenta: definicje metryk; widoki pytań; lustra faktów. |
| `metrics.metric_definition` | Definicja metryki: `key` + `version` (PK), `name_pl`, `description_pl`, `grain` (`company`/`cost_object`/`counterparty`/`document`/`day`), `unit` (`pln_minor`/`days`/`ratio`/`count`), `implementation` (`python:moduł:Klasa`), `dimensions`, `filters`, `traps_pl` (pułapki rozstrzygane definicją), `code_sha256` (skrót kodu definicji), `status` `active`/`deprecated`. Wiersz niezmienny - zmiana definicji = nowa wersja (KS013; jedyna zmiana `active → deprecated`). Zapisuje `python -m kasa metrics sync`. |
| `agent_call_log` | Każde wywołanie narzędzia MCP: firma, narzędzie, argumenty po pseudonimizacji, model, wersja promptu, klient, czas, `status` `ok`/`refused`/`error`, `result_summary` (bez tekstów), `created_by` (rola). Tylko dopisywanie (KS001). |
| `metric_proposal` | Pytanie spoza warstwy metryk (np. CIT): pytanie po pseudonimizacji, żądany klucz, powód, `proposed_by` `agent`/`human`, `status` `open`/`accepted`/`rejected` (decyzja raz, KS012). |
| `export_log` | Każde udostępnienie danych biuru: kto, komu, kiedy, zakres (`scope`: miesiąc, format, tabele, liczba wierszy), pliki (`files`: nazwa, SHA-256, bajty, wiersze). Tylko dopisywanie. |
| `suggestion_kind.payment_reminder` | Szkic przypomnienia o płatności (sugestia agenta; wysyła człowiek). |
| `views.transactions`, `documents`, `receivables`, `cost_objects`, `allocations`, `queue` | Widoki pytań jednej firmy (`kasa_agent_company()`); tytuł i powód rangi jako `{"untrusted_text": …}`; osoby kodem; bez przelewów wrażliwych i wynagrodzeń osób. `transactions.related_transactions` - zwrot ↔ pierwotna wpłata. |
| `evidence.<tabela>` | Lustra (`security_barrier`) `company`, `l0_raw` (tylko metadane), L1-L3, `truth_check_result`, `queue_state`, `document_lineage`, `title_semantics_current`, `rule_stage_effective` jednej firmy, z maskami danych osób (tytuł po pseudonimizacji, IBAN/nazwa osoby jako kod, bez telefonów, pola arkusza bez osoby i kontaktów, wynagrodzenie bez osoby, bez wejścia modelu, bez faktów sugestii). Buduje je `kasa_refresh_agent_views()` z bieżących kolumn tabel (także przy `metrics sync`). |
| role `kasa_agent_ro`, `kasa_agent_suggest` | RO: `USAGE` + `SELECT` tylko na `metrics`/`views`/`evidence`, `statement_timeout = 15s`, `default_transaction_read_only`, `search_path = evidence, views, metrics, public`. Suggest: tylko `INSERT` do `l2_suggestion`, `metric_proposal`, `agent_call_log`, `statement_timeout = 15s`. `EXECUTE` funkcji zmieniających dane (`kasa_purge_raw`, `kasa_expire_*`, `kasa_anonymize_persons`, `kasa_refresh_agent_views`) odebrane `PUBLIC`. |
| `kasa_agent_company()`, `kasa_tx_is_payroll(id)`, `kasa_doc_is_person(…)`, `kasa_party_display(id)`, `kasa_mask_internal_fields(…)`, `kasa_strip_phone_numbers(…)`, `kasa_rank_reason_masked(…)` | Funkcje widoków agenta: firma sesji, „ten przelew to wynagrodzenie osoby”, dokument osoby, nazwa kontrahenta po bramce, maski pól. `kasa_subject_exists` od 0012 `SECURITY DEFINER` (strażnik KS005 działa też dla roli suggest). |
| `l1_semantics.input_sha256`, `input_chars`, `input_expires_at`, `input_expired_at` | Wejście modelu: skrót i długość na zawsze (trigger przy INSERT), tekst `input_pseudonymized` 90 dni; zeruje go tylko `kasa_expire_semantics_inputs()` (KS014; przegląd fazy 3, pkt 4 i 10). |
| `anonymization_log`, `kasa_anonymize_persons(firma, przed, …)` | Jedyna ścieżka UPDATE surowych kolumn osób (`l1_bank_transaction.title_raw`, `counterparty_name_raw`, `counterparty_iban_raw` osoby, `extracted.phones`; `l1_internal_record.fields`; `l1_pdf_extraction.extracted` dokumentów osób; `l1_document.seller_name`/`buyer_name`/`payment_iban` rachunków osób; `l1_counterparty` osób bez późniejszych rekordów) - nadpisuje je formą pseudonimową, kwoty, daty, obiekty i firmy zostają; wpis w dzienniku (liczby wierszy per tabela, kto, kiedy, `xact_id`). Strażnik `kasa_raw_person_guard` (KS014) odrzuca każdą inną zmianę tych kolumn (przegląd fazy 2, pkt 3). |

Nowe kody strażników: `KS013` definicja metryki niezmienna, `KS014` surowa kolumna osoby albo wejście modelu zmienione
poza funkcją retencji. `python -m kasa check` sprawdza triggery 0012, role agenta i ostrzega o tekstach modelu po
90 dniach (`kasa/core/checks.py`, dopisane na końcu); `python -m kasa retention run` je wygasza.
