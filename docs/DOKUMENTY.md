# Dokumenty: KSeF, pliki z historią, PDF z maili, deduplikacja

Kanał dokumentów fazy 2. Każde źródło idzie tą samą drogą:

1. **L0** - plik dokładnie taki, jak przyszedł (`store_raw`, SHA-256, tylko INSERT);
2. **odczyt** - parser (kod) albo model (PDF) daje fakty `InvoiceFacts` (`kasa/core/documents/facts.py`);
3. **L1** - `upsert_document` łączy obserwacje tej samej faktury pod jednym dokumentem
   (`l1_document`, pozycje `l1_document_line`, obserwacje `l1_document_observation`).

Ponowny import czegokolwiek nie dodaje żadnego wiersza. Kod: `kasa/core/documents/`, dostawcy
`kasa/core/providers/ksef.py` i `llm.py`, komendy `python -m kasa documents ...` (`kasa/cli/documents.py`).

## Komendy

```bash
python -m kasa documents ksef-sync --company odm --from 2026-09-01 --to 2026-10-31   # + kontrola kompletności
python -m kasa documents ksef-check --company odm --from 2026-09-01 --to 2026-10-31  # kontrola prawdy nr 4
python -m kasa documents import-jpk fixtures/odm/history/JPK_V7M_2026-09.xml          # firma po NIP z Podmiot1
python -m kasa documents import --company odm --mapping document fixtures/odm/history/biuro_export_2025-10.csv
python -m kasa documents import --company odm --mapping pm_sheet fixtures/odm/internal/arkusz_pm_2026-10.xlsx
python -m kasa documents import-pdf --company odm --meta fixtures/odm/email_pdfs/index.json fixtures/odm/email_pdfs
python -m kasa documents list --company odm --direction purchase --month 2026-10
python -m kasa documents show FV/2026/10/0412        # id (lub jego początek), numer KSeF albo numer faktury
```

`show` pokazuje dokument, wszystkie jego wersje, obserwacje z każdego kanału (plik, miejsce w pliku,
po czym dopięto), pozycje, powiązania (korekta, zaliczka), kontrahenta, odczyt PDF i otwarte sugestie.

## KSeF - co musisz zrobić

Kasa tylko **czyta** z KSeF (nigdy nie wystawia faktur). Potrzebny jest dostęp tylko do odczytu w kontekście NIP firmy:

1. Zaloguj się do **Aplikacji Podatnika KSeF** (KSeF 2.0) jako osoba uprawniona w firmie.
2. Wygeneruj **token KSeF** z jednym uprawnieniem: *przeglądanie faktur* (bez wystawiania, bez zarządzania
   uprawnieniami) - albo wystaw **certyfikat KSeF** do uwierzytelnienia dla osoby/podmiotu, który ma
   wyłącznie to uprawnienie. Token jest prostszy (kasa obsługuje go w całości); logowanie certyfikatem
   wymaga jeszcze podpisu XAdES (szkielet w `Ksef._signed_auth_request`, do uzupełnienia, gdy użyjemy certyfikatu).
3. Ustaw zmienne środowiskowe (np. w sekretach środowiska):

   | Zmienna | Znaczenie |
   | --- | --- |
   | `KASA_KSEF_PROVIDER=ksef` | włącza prawdziwy KSeF (domyślnie `mock`) |
   | `KASA_KSEF_NIP` | NIP firmy (kontekst sesji) |
   | `KASA_KSEF_TOKEN` | token KSeF tylko do odczytu |
   | `KASA_KSEF_CERT_PATH`, `KASA_KSEF_KEY_PATH`, `KASA_KSEF_KEY_PASSWORD` | zamiast tokenu: certyfikat i klucz |
   | `KASA_KSEF_ENV=prod\|test\|demo` albo `KASA_KSEF_BASE_URL` | środowisko (domyślnie `https://ksef.mf.gov.pl/api/v2`) |

   Szyfrowanie tokenu kluczem publicznym MF wymaga pakietu `cryptography` (`uv pip install cryptography`).
4. Uruchamiaj pobieranie **co 15 minut** (np. zadanie cykliczne / cron):
   `*/15 * * * * python -m kasa documents ksef-sync --company odm`. Bez `--from` kasa zaczyna od daty
   ostatniej przyjętej faktury, którą już ma, i kończy dziś - kolejne przebiegi niczego nie dublują.

Brak zmiennych nie zatrzymuje programu: fabryka `get_ksef_provider()` zapisuje ostrzeżenie i używa danych
testowych (`MockKsef`, katalog `fixtures/<firma>/ksef/`, zmienna `KASA_FIXTURES_DIR`).

Jak działa klient (`Ksef`, szkielet bez testów sieciowych): `POST /auth/challenge` → `POST /auth/ksef-token`
(token zaszyfrowany RSA-OAEP) → `GET /auth/{referenceNumber}` → `POST /auth/token/redeem` (token dostępu);
lista faktur `POST /invoices/query/metadata` (osobno sprzedaż `Subject1` i zakup `Subject2`, po dacie
trwałego zapisu w KSeF, stronicowane), treść `GET /invoices/ksef/{numer}`. Dla dużych okien historii KSeF
udostępnia paczki (`POST /invoices/exports`, zaszyfrowane części ZIP) - do dopisania przy pierwszym imporcie
historii. Kierunek faktury ustala NIP firmy: Podmiot1 = my → sprzedaż, Podmiot2 = my → zakup. NIP firmy
kasa zapamiętuje w `company.nip` przy pierwszym pobraniu z KSeF albo imporcie JPK.

**Parser FA(3)** (`fa3.py`) zapisuje wszystkie grupy pól z kroku 2 briefingu: tożsamość (numer KSeF,
numer surowy i znormalizowany, rodzaj VAT/KOR/ZAL/ROZ, skrót pliku, program wystawcy), daty (P_1, P_6
albo `OkresFa`, brak P_6 = data wystawienia, termin płatności opcjonalny), strony z NIP-em lub numerem
VAT-UE, rachunkiem do zapłaty (rachunek faktora ma pierwszeństwo) i stronami trzecimi (Podmiot3 z rolą),
kwoty per stawka (P_13_x/P_14_x) i razem, walutę z kursem i kwotą PLN, pozycje (P_7-P_12; korekta: stan
przed i po, kwoty pozycji = różnica; zaliczkowa: pozycje zamówienia oznaczone `line_kind = 'order'`),
powiązania (faktura korygowana, zaliczki, zamówienie, umowa) i flagi (P_18A MPP, P_16 metoda kasowa,
P_18 odwrotne obciążenie). Każdy element, którego parser nie mapuje na kolumnę, trafia do `extra`
(dokument) albo `extra` pozycji - pod swoją ścieżką, nic nie ginie. VAT pozycji to udział w P_14_x stawki
(rozdzielony tak, by suma się zgadzała co do grosza).

## JPK i eksport biura - historia sprzed KSeF

- **JPK_V7M / V7K** (`jpk.py`): jeden wiersz `SprzedazWiersz` / `ZakupWiersz` = jedna obserwacja
  (`locator` `sales:4`, `purchase:17`), bez pozycji. Rejestr nie odróżnia faktury zaliczkowej od zwykłej,
  więc rodzaj jest „niepewny” (`kind_certain = false`) - deduplikacja odróżnia wtedy tylko korekty.
  Wiersze samego samonaliczenia VAT (WNT, import usług, K_23-K_32) nie są naszą sprzedażą - są pomijane
  z adnotacją. Plik JPK innej firmy (inny NIP w Podmiot1) jest odrzucany. Numer KSeF z JPK_V7(3) (`NrKSeF`)
  jest używany do deduplikacji.
- **JPK_FA**: faktury z pozycjami (`FakturaWiersz` po `P_2B`).
- **Eksport biura (CSV/XLSX)**: przez kreator importu z mapowaniem `document`.

## Kreator importu (CSV, XLSX)

1. Plik jest czytany taki, jaki jest: kodowanie (UTF-8 z BOM lub bez, inaczej Windows-1250), separator
   (`;`, tabulator, `,`, `|` - po nagłówku), wiersz nagłówka z mapowania.
2. Kolumny mapuje rozszerzenie firmy (`kasa/extensions/<firma>/extension.yaml`, sekcja `import_mappings`).
   Mapowanie, które człowiek potwierdził dla plików o danych nagłówkach, zapisuje się w
   `import_mapping_profile`; następny plik o tych samych nagłówkach importuje się bez pytań.
   Brakującą kolumnę wskazujesz interaktywnie albo opcją `--map pole=Nagłówek` (np.
   `--map number_raw="Nr dok."`); `--no-input` zamienia pytanie w błąd z listą nagłówków.
   Zmiana mapowania = nowy profil, stary dostaje `superseded_by`.
3. `target: document` → dokumenty z deduplikacją; `target: internal_record` → `l1_internal_record`
   (cały wiersz w `fields`, osoba jako pseudonim, kwota, data, `cost_object_ref` tak, jak zapisano - rdzeń
   go nie interpretuje - i typ rozliczenia).

**Jak dodać mapowanie w rozszerzeniu** (przykład ODM, eksport biura):

```yaml
import_mappings:
  document:
    target: document
    file_types: [csv, xlsx]
    columns:
      number_raw:        {source: numer, aliases: [numer dokumentu, nr dokumentu], required: true}
      issue_date:        {source: data, type: date, required: true}   # DD.MM.RRRR, RRRR-MM-DD, ...
      counterparty_name: {source: kontrahent}
      counterparty_nip:  {source: nip}
      net:               {source: netto, type: amount}                # przecinek dziesiętny, spacje
      vat:               {source: vat, type: amount}
      gross:             {source: brutto, type: amount, required: true}
      kind:              {source: typ}
```

Pola rdzenia dla dokumentu: `number_raw`, `ksef_number`, `kind`, `direction`, `issue_date`, `sale_date`,
`due_date`, `seller_nip`, `seller_name`, `buyer_nip`, `buyer_name`, `currency`, `net`, `vat`, `gross`
(wymagane: numer, data wystawienia, brutto). Umowa kanału dokumentów: pola `counterparty_nip` i
`counterparty_name` trafiają po stronie drugiej strony (nabywca sprzedaży, sprzedawca zakupu), a tekst w
`kind`/`direction` rozstrzyga kierunek i rodzaj („sprzedaż”, „zakup”, „import usług” → zagraniczna,
„korekta…” → korekta, „zaliczk…” → zaliczkowa). Nagłówki porównujemy luźno (wielkość liter, ogonki, spacje).
Mapowanie arkusza PM (`pm_sheet`) jest w tym samym pliku; nowa firma dodaje swoje bez zmiany kodu.

Osoba z arkusza dostaje pseudonim z bramki pseudonimizacji (`kasa.core.pseudonym.register_person`,
`pseudonym_key.basis = 'registered'` - osoba znana także dla tytułów przelewów);
jeśli moduł lub tabela kluczy nie działa, stabilny kod HMAC nazwiska (`KASA_PSEUDONYM_SECRET`) - tymczasowo,
z adnotacją `pseudonym_method` w dowodzie.

## PDF z maili - koszt i pseudonimizacja

Najdroższy i najmniej pewny kanał, dlatego:

- **Model czyta plik raz.** Odczyt (`l1_pdf_extraction`) jest zapisywany z kluczem (plik, model, wersja
  promptu) i ponowny import go używa - drugi przebieg nie kosztuje nic. Nowa wersja promptu = nowy odczyt.
- **Koszt** pierwszego importu ≈ liczba PDF-ów × (tokeny wejścia + wyjścia) × cena modelu z cennika Vertex AI.
  Jedna strona faktury to rząd 1-2 tys. tokenów wejścia i kilkuset wyjścia (tekst, nie obraz). Przed
  importem archiwum policz pliki (`import-pdf` na mocku nic nie kosztuje) i zatwierdź szacunek.
- **Bramka przed wywołaniem (od fazy 3).** Tekst PDF idzie do modelu wyłącznie przez
  `kasa.core.semantics.gate.guarded_complete` (cel `pdf_invoice`, `docs/SEMANTYKA.md`): słowo z listy
  wrażliwej omija model w ogóle, potem pseudonimizacja (nazwiska, adresy, telefony, IBAN-y, e-maile → kody),
  druga linia obrony (czy nie został IBAN/telefon/PESEL/e-mail) i wpis w `llm_call_log`. Gdy bramka nie
  istnieje albo zgłasza błąd, **wywołania nie ma - także dla modelu lokalnego (mock)**; plik zostaje w L0,
  status `blocked`. Model nigdy nie dostaje surowego PDF-u (bajtów nie da się spseudonimizować). Kody w
  odpowiedzi modelu wracają do wartości po naszej stronie (`depseudonymize`, rola wewnętrzna) - **tylko te,
  które były w tekście wysłanym** (model mógłby odpowiedzieć cudzym kodem). Odczyt regexem modelu testowego
  (nieznany plik) ma `method = 'code'`, nie `model`.
- **Kontrola kodu:** pozycje muszą sumować się do kwot razem (netto, VAT, brutto). Niezgodność: obserwacja
  i dokument są zapisane, ale z flagą `needs_review` (kolejka przeglądu).
- **Kurs:** faktura walutowa trzyma walutę; PLN liczymy z kursu NBP z ostatniego dnia przed datą
  sprzedaży/wystawienia (`l1_fx_rate`, źródło `nbp_*`), gdy dokument nie podaje kursu.

Vertex AI (decyzja D5): `KASA_LLM_PROVIDER=vertex`, `KASA_VERTEX_PROJECT`, `KASA_VERTEX_LOCATION`
(tylko region `europe-*`, domyślnie `europe-west1`), `GOOGLE_APPLICATION_CREDENTIALS` (konto usługi),
`KASA_VERTEX_MODEL` (Gemini, np. `gemini-2.5-flash`, albo Claude: identyfikator zaczynający się od `claude`).
Potrzebny pakiet `google-auth`. Brak konfiguracji = mock z ostrzeżeniem.

## Deduplikacja

Ta sama faktura przychodzi z KSeF, z PDF-u w mailu, z JPK i z eksportu biura. `upsert_document` szuka
dokumentu w kolejności pewności:

1. **numer KSeF**;
2. **skrót pliku** (ten sam plik dokumentu z innego miejsca);
3. **(NIP drugiej strony, rodzaj, numer znormalizowany)** przy **zgodnej dacie wystawienia i kwocie brutto
   w tej samej walucie**. Ten sam numer u innego NIP-u to nigdy duplikat; dokument bez NIP-u nie jest
   łączony po numerze.
4. Ten sam NIP i numer, ale inna data, kwota albo rodzaj → **nowy dokument** z `possible_duplicate` w
   `evidence` i sugestią L2 `duplicate` (kolejka przeglądu). Człowiek decyduje.

Każda obserwacja to wiersz `l1_document_observation` z polami tak, jak widział je kanał (`observed`),
miejscem w pliku (`locator`) i tym, po czym ją dopięto (`linked_by`). Dokument ma pola z najpewniejszego
kanału: **KSeF > PDF > JPK > CSV/XLSX**. Gdy pewniejszy kanał przychodzi później (PDF, potem KSeF), powstaje
nowa wersja dokumentu, stara dostaje `superseded_by`. Nowa wersja parsera lub promptu czytająca ten sam plik
też tworzy nową wersję; ta sama wersja - nic. Widok `document_lineage` mapuje każdą wersję na bieżącą
(`head_id`) - przez niego obserwacje, powiązania i dopasowania wskazujące starszą wersję trafiają do
bieżącego dokumentu.

Korekta wskazuje dokument korygowany (`corrects_document_id`, po numerze KSeF albo numerze + NIP),
rozliczeniowa - zaliczkę (`settles_advance_document_id`). Gdy dokument powiązany przyjdzie później,
powiązanie uzupełnia `relink_pending` (nowa wersja). Kontrahent (`counterparty_id`) pochodzi z
`kasa_upsert_counterparty` (migracja 0006) - po NIP-ie, numerze zagranicznym, IBAN-ie albo nazwie;
rachunek osoby bez NIP-u daje kontrahenta rodzaju `person`.

## Kontrola prawdy nr 4: kompletność KSeF

`check_ksef_completeness` (`kasa/core/checks.py`, komenda `ksef-check`, automatycznie po `ksef-sync`)
porównuje bieżące dokumenty z numerem KSeF przyjęte w oknie dat (czas polski; dla dokumentu bez daty
przyjęcia - data zapisana w numerze KSeF) z listą `KsefProvider.list_invoice_numbers`:

- liczba i suma brutto per kierunek i waluta muszą być równe,
- w obie strony: brak u nas (`missing_in_kasa`) i brak w KSeF (`missing_in_ksef`),
- ten sam numer z inną kwotą, walutą lub kierunkiem (`mismatches`).

Wynik trafia do `truth_check_result` (`check_name = 'ksef_completeness'`), gdy tabela istnieje (0006);
bez niej zwracany jest tylko w pamięci.

## Tabele i kolumny (migracja 0007)

| Obiekt | Po co |
| --- | --- |
| `import_mapping_profile` | Pamięć kreatora: mapowanie kolumn potwierdzone raz dla plików o danych nagłówkach. |
| `l1_pdf_extraction` | Odczyt PDF przez model: raz na (plik, model, prompt), także gdy nie powstał dokument; skrót tekstu wysłanego do modelu, wynik pseudonimizacji, `lines_sum_ok`, `needs_review`. |
| `document_lineage` (widok) | Wersja dokumentu → bieżąca wersja jej łańcucha. |
| `company.nip` | NIP firmy: rozstrzyga kierunek faktury. |
| `l1_document.extra`, `l1_document_line.extra` | Pola źródła bez własnej kolumny („zapisujemy wszystko”). |
| `l1_document_line.line_kind` | `invoice` / `order` (pozycja zamówienia z faktury zaliczkowej) / `correction` (różnica po-przed). |
| `l1_document_observation.parser_version` | Wersja parsera, która czytała plik (idempotencja i nowe wersje). |
| `document_kind` + `bill` | Rachunek osoby lub sprzedawcy bez VAT. |
| `suggestion_kind` + `duplicate` | „To może być duplikat dokumentu X” do kolejki. |

## Ograniczenia

- Prawdziwy klient KSeF i Vertex AI to szkielety bez testów sieciowych; podpis XAdES (certyfikat) i paczki
  eksportu KSeF są do uzupełnienia przy pierwszym podłączeniu.
- PDF bez warstwy tekstowej (skan) daje pusty tekst - OCR nie jest częścią fazy 2.
- Wiersze arkusza z CSV i XLSX tego samego arkusza to osobne pliki - importuj jeden z nich.
